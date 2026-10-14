"""
jarvis/tools/telegram_tools.py — agent → Telegram: buttons wala sawaal
=======================================================================
`tg_send_buttons`: owner ke Telegram par inline buttons bhejo, tap ka wait karo.

Use cases: agent ko beech me human decision chahiye —
"kaun sa option? [A] [B] [C]", "aage badhu? [Haan] [Nahi]".

Technical: Bot HTTP API seedha urllib se (koi extra dependency nahi);
tap `jarvis.telegram.bot` ke waiter registry se wapas aata hai.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import urllib.error
import urllib.request

from langchain_core.tools import tool

from ..config import settings as S

TELEGRAM_API = "https://api.telegram.org"


def _api_post(token: str, method: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{TELEGRAM_API}/bot{token}/{method}", data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


@tool("tg_send_buttons")
async def tg_send_buttons(
    text: str, buttons: list[str], chat_id: str = "", wait_for_tap: bool = True, timeout_s: int = 120
) -> str:
    """Telegram par inline buttons bhejo, owner ke tap ka wait karo.

    Args:
        text: sawaal/message (plain text).
        buttons: 1-6 labels, e.g. ["Haan, chalao", "Nahi"]. Value = label.
        chat_id: khaali = TELEGRAM_OWNER_ID (owner ka DM).
        wait_for_tap: True = tap aane tak ruko (agent ka interactive sawaal);
            False = sirf bhejo, jawab ka wait mat karo.
        timeout_s: kitni der tap ka wait karein (default 120s).

    Returns tap hua label, ya timeout/cancel/error message. Token kabhi print nahi hota.
    """
    labels = [str(b).strip() for b in (buttons or []) if str(b).strip()][:6]
    if not (text or "").strip():
        return "❌ text khaali hai — sawaal to likho."
    if not labels:
        return "❌ kam se kam 1 button chahiye."
    token = (S.TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        return "❌ TELEGRAM_BOT_TOKEN set nahi hai — Telegram send band hai."
    target = (chat_id or "").strip() or str(S.TELEGRAM_OWNER_ID or "").strip()
    if not target:
        return "❌ chat_id do ya TELEGRAM_OWNER_ID set karo."

    from ..telegram.bot import (
        TAP_CB_PREFIX,
        cancel_tap_waiter,
        register_tap_waiter,
        tap_result,
    )

    tap_id = secrets.token_hex(6)
    # 2-per-row keyboard; callback_data 64-char limit ke andar
    rows, row = [], []
    for i, label in enumerate(labels):
        row.append({"text": label[:30], "callback_data": f"{TAP_CB_PREFIX}{tap_id}:{i}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    try:
        resp = await asyncio.to_thread(
            _api_post,
            token,
            "sendMessage",
            {"chat_id": target, "text": text[:3500], "reply_markup": {"inline_keyboard": rows}},
        )
    except urllib.error.HTTPError as exc:
        return f"❌ Telegram send fail (HTTP {exc.code}) — token/chat_id check karo."
    except Exception as exc:
        return f"❌ Telegram send fail: {type(exc).__name__}: {str(exc)[:150]}"
    if not resp.get("ok"):
        return f"❌ Telegram ne mana kiya: {str(resp.get('description', ''))[:150]}"
    msg_id = (resp.get("result") or {}).get("message_id", "?")
    if not wait_for_tap:
        return f"✅ Telegram par bheja (message {msg_id}) — tap ka wait nahi kiya."

    waiter = register_tap_waiter(tap_id, [{"label": b, "value": b} for b in labels])
    try:
        await asyncio.wait_for(waiter.wait(), timeout=max(5, min(timeout_s, 600)))
    except asyncio.TimeoutError:
        cancel_tap_waiter(tap_id)
        return f"⏳ {timeout_s}s me koi tap nahi aaya — user busy hai. Default se aage badho ya baad me dobara poochho."
    box = tap_result(tap_id) or {}
    cancel_tap_waiter(tap_id)
    return f"✅ Tap: **{box.get('value', '?')}** (by {box.get('by', '?')})"


TELEGRAM_TOOLS = [tg_send_buttons]
