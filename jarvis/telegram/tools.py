"""Telegram-only action buttons with server-side, owner-bound callback records."""
from __future__ import annotations

import contextvars
import json
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import tool


@dataclass(frozen=True)
class TelegramContext:
    bot: Any
    chat_id: str
    user_id: str
    role: str


@dataclass(frozen=True)
class PendingButtonAction:
    action: str
    chat_id: str
    user_id: str
    expires_at: float


_CONTEXT: contextvars.ContextVar[TelegramContext | None] = contextvars.ContextVar(
    "nikhilai_telegram_tool_context", default=None,
)
_PENDING_ACTIONS: dict[str, PendingButtonAction] = {}
_PENDING_LOCK = threading.RLock()
_ACTION_TTL_S = 600.0
_MAX_PENDING_ACTIONS = 500


@contextmanager
def use_telegram_context(bot: Any, chat_id: int | str, user_id: int | str, role: str) -> Iterator[None]:
    token = _CONTEXT.set(TelegramContext(bot=bot, chat_id=str(chat_id), user_id=str(user_id), role=str(role)))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def _prune_actions(now: float) -> None:
    for action_id, record in list(_PENDING_ACTIONS.items()):
        if record.expires_at <= now:
            _PENDING_ACTIONS.pop(action_id, None)
    if len(_PENDING_ACTIONS) > _MAX_PENDING_ACTIONS:
        oldest = sorted(_PENDING_ACTIONS, key=lambda item: _PENDING_ACTIONS[item].expires_at)
        for action_id in oldest[:len(_PENDING_ACTIONS) - _MAX_PENDING_ACTIONS]:
            _PENDING_ACTIONS.pop(action_id, None)


def _store_action(action: str, context: TelegramContext) -> str:
    now = time.time()
    with _PENDING_LOCK:
        _prune_actions(now)
        action_id = secrets.token_urlsafe(9)
        _PENDING_ACTIONS[action_id] = PendingButtonAction(
            action=str(action), chat_id=context.chat_id, user_id=context.user_id,
            expires_at=now + _ACTION_TTL_S,
        )
    return action_id


def consume_button_action(action_id: str, *, user_id: int | str, chat_id: int | str) -> str | None:
    """Consume an action once and only for the original user+chat; return its prompt."""
    with _PENDING_LOCK:
        record = _PENDING_ACTIONS.get(str(action_id))
        if record is None:
            return None
        if record.expires_at <= time.time():
            _PENDING_ACTIONS.pop(str(action_id), None)
            return None
        if record.user_id != str(user_id) or record.chat_id != str(chat_id):
            return None
        _PENDING_ACTIONS.pop(str(action_id), None)
        return record.action


@tool
async def tg_send_buttons(text: str, buttons_json: str) -> str:
    """Send owner-visible Telegram buttons; callbacks contain opaque one-use server-side action IDs.

    `buttons_json` is a JSON array like `[{"label":"Check status","action":"show my status"}]`.
    Clicking a button submits the stored action through Jarvis' shared agent and Dangerous-Command Gate.
    """
    context = _CONTEXT.get()
    if context is None or context.role != "owner" or context.chat_id != context.user_id:
        return "🚫 tg_send_buttons sirf owner ke active Telegram private chat me chalta hai; koi button nahi bheja."
    try:
        rows = json.loads(buttons_json)
    except (TypeError, ValueError):
        return "❌ buttons_json valid JSON array nahi hai; koi button nahi bheja."
    if not isinstance(rows, list) or not rows or len(rows) > 8:
        return "❌ 1–8 buttons ka JSON array chahiye; koi button nahi bheja."

    validated: list[tuple[str, str]] = []
    for item in rows:
        if not isinstance(item, dict):
            return "❌ Har button me label aur action chahiye; koi button nahi bheja."
        label, action = str(item.get("label") or "").strip(), str(item.get("action") or "").strip()
        if not label or not action or len(label) > 64 or len(action) > 3000:
            return "❌ Button label/action khali ya size limit se bada hai; koi button nahi bheja."
        validated.append((label, action))

    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except Exception as exc:
        return f"❌ Telegram button dependency unavailable ({type(exc).__name__}); koi action run nahi hua."

    buttons: list[Any] = []
    for label, action in validated:
        action_id = _store_action(action, context)
        # Telegram callback_data contains only an opaque ID, never the prompt/action text.
        buttons.append(InlineKeyboardButton(label, callback_data=f"tgact:{action_id}"))

    keyboard = InlineKeyboardMarkup([buttons[index:index + 2] for index in range(0, len(buttons), 2)])
    from .md import html_chunks

    chunks = html_chunks(str(text or "Choose an action:"), limit=3500)
    try:
        for index, chunk in enumerate(chunks):
            await context.bot.send_message(
                chat_id=int(context.chat_id), text=chunk or "Choose an action:", parse_mode="HTML",
                reply_markup=keyboard if index == len(chunks) - 1 else None,
                disable_web_page_preview=True,
            )
    except Exception as exc:
        # A partial send failure must not leave usable actions behind.
        with _PENDING_LOCK:
            for button in buttons:
                callback = str(getattr(button, "callback_data", ""))
                action_id = callback.removeprefix("tgact:")
                _PENDING_ACTIONS.pop(action_id, None)
        return f"❌ Telegram buttons send nahi hue ({type(exc).__name__}); koi action run nahi hua."
    return (f"✅ {len(buttons)} owner-only Telegram action button(s) bheje. "
            f"{len(chunks)} safe message chunk(s); click par action ek baar chalega.")


TELEGRAM_TOOLS = [tg_send_buttons]
