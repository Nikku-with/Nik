"""
Telegram interface — same agent, phone se
========================================

Bot token daalo, `/start` bhejo, aur wahi NikhilAI agent Telegram se chalao:

    "chrome kholo"  ·  "aaj ka mausam batao"  ·  "ye file padho"  ·  "/mode voice"
    "/settings owner Nikhil"  ·  "/memory"  ·  "/automation list"

Security (yahan laxness sabse mehngi padti hai):
  * sirf `TELEGRAM_OWNER_ID` / `TELEGRAM_ALLOWED_CHAT_IDS` / `TELEGRAM_ALLOWED_USERS` wale chalta hai
  * owner = poore tools, guest = sirf READ-level (desktop/shell/file-write band)
  * har chat ka apna session + apni memory namespace — cross-user leak nahi
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from ..config import settings as S

log = logging.getLogger("nikhilai.telegram")

HELP_TEXT = """🤖 *NikhilAI* — aapka AI operator (Telegram se)

Seedha Hinglish me likho, slash command ki zaroorat nahi:
• `chrome kholo`
• `aaj ka mausam batao`
• `meri resume pdf dhoondh ke padho`
• `ek script banao jo csv se report banaye`
• `yaad rakh mera favourite color black hai`

*Commands*
/status — readiness, current interface, brain/model, tools, memory
/mode [text|voice|telegram] — default interface preference (active interface alag rehta hai)
/model [naam] · /provider [naam] — brain preference/status (connectivity test nahi hota)
/settings [key value] — settings dekho/badlo
/tools — registered tool schemas
/memory [pending|confirm <id>|cancel <id>|delete <description>] · /remember <baat> · /forget <baat>
/apps [list|forget <query>|clear] — verified app-launch mappings
/automation [list|off <id>|on <id>|run <id>|cancel <id>]
/gate — Dangerous-Command Gate mode
/refresh · /clear · /stop · /restart · /voice · /progress
/help — yahi message
"""


# ---------------------------------------------------------------------------
# authorization
# ---------------------------------------------------------------------------
def _id_set(raw: list[str]) -> set[str]:
    return {str(x).strip() for x in raw if str(x).strip()}


def authorize(user_id: int | str, chat_id: int | str) -> tuple[bool, str]:
    """(allowed, role) — role: owner | guest"""
    uid, cid = str(user_id), str(chat_id)
    owner_id = str(S.TELEGRAM_OWNER_ID or "").strip()
    allowed_chats = _id_set(S.TELEGRAM_ALLOWED_CHAT_IDS)
    allowed_users = _id_set(S.TELEGRAM_ALLOWED_USERS)

    if owner_id and uid == owner_id:
        return True, "owner"
    if allowed_users and uid in allowed_users:
        return True, "guest"
    if allowed_chats and cid in allowed_chats:
        return True, "guest"
    if not owner_id and not allowed_chats and not allowed_users:
        # Fail closed: unconfigured bot never promotes its first random caller.
        return False, "unconfigured"
    return False, "blocked"


def surface_for(role: str) -> str:
    return "telegram_owner" if role == "owner" else "telegram_guest"


# ---------------------------------------------------------------------------
# agent bridge (ek hi runner: LangGraph)
# ---------------------------------------------------------------------------
async def run_agent(text: str, chat_id: int | str, role: str = "owner", on_progress=None,
                    user_id: int | str | None = None) -> dict[str, Any]:
    """Telegram message → wahi LangGraph agent → result.

    `on_progress`: live thinking/tool feed (LiveProgress.on_event) — isse user ko
    real-time dikhta hai ki agent kya kar raha hai, aur error bhi surface hote hain.
    """
    from ..runtime.agent import run_agent as core_run

    session = f"telegram:{chat_id}:{user_id or chat_id}"
    memory_user_id = "nikhil_mehra" if role == "owner" else f"telegram_guest:{user_id or chat_id}"
    result = await core_run(text, session_id=session, surface=surface_for(role),
                            on_progress=on_progress, memory_user_id=memory_user_id)
    # AgentResult → dict (bot ke baaki handlers isi shape par chalte hain)
    data = result.as_dict() if hasattr(result, "as_dict") else dict(result)
    data.setdefault("response", data.get("answer", ""))
    return data


def telegram_available() -> bool:
    try:
        import telegram  # noqa: F401

        return True
    except Exception:
        return False


def config_ok() -> tuple[bool, str]:
    if not S.TELEGRAM_BOT_TOKEN:
        return False, "TELEGRAM_BOT_TOKEN set nahi hai (@BotFather se lo)"
    if not telegram_available():
        return False, "`pip install python-telegram-bot` chahiye"
    if not (S.TELEGRAM_OWNER_ID or S.TELEGRAM_ALLOWED_CHAT_IDS or S.TELEGRAM_ALLOWED_USERS):
        return False, ("Telegram authorization missing: set TELEGRAM_OWNER_ID or an explicit allowed user/chat ID; "
                       "bot stays closed until configured.")
    return True, "ok"


# ---------------------------------------------------------------------------
# bot
# ---------------------------------------------------------------------------
def build_application():
    """python-telegram-bot Application banao (commands + free-text handler)."""
    from telegram import Update
    from telegram.constants import ChatAction, ParseMode
    from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

    async def _deny(update: Update) -> None:
        await update.effective_message.reply_text(
            "🚫 Aap is bot ke allowed users me nahi ho.\n"
            "Owner ko bolo apna Telegram ID `TELEGRAM_ALLOWED_USERS` me daale."
        )

    async def _reply_long(msg, text: str, *, reply_markup=None) -> None:
        """Markdown ko escaped Telegram HTML me render karke balanced 4096-safe chunks bhejo."""
        from .md import html_chunks

        chunks = html_chunks(text, limit=3900)
        for index, chunk in enumerate(chunks):
            await msg.reply_text(
                chunk, parse_mode=ParseMode.HTML,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
                disable_web_page_preview=True,
            )

    async def _guard(update: Update) -> tuple[bool, str]:
        user = update.effective_user
        chat = update.effective_chat
        allowed, role = authorize(user.id if user else 0, chat.id if chat else 0)
        if not allowed:
            if role == "unconfigured":
                await update.effective_message.reply_text(
                    "🔒 Bot abhi band hai: owner ID ya explicit allowlist `.env` me configure karein "
                    "(TELEGRAM_OWNER_ID / TELEGRAM_ALLOWED_USERS / TELEGRAM_ALLOWED_CHAT_IDS)."
                )
            else:
                await _deny(update)
        return allowed, role

    def _memory_user(update: Update, role: str) -> str:
        if role == "owner":
            return "nikhil_mehra"
        user = update.effective_user
        return f"telegram_guest:{user.id if user else (update.effective_chat.id if update.effective_chat else 0)}"

    # ------------------------------------------------------------ commands
    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        name = S.OWNER_NAME if role == "owner" else "there"
        extra = "" if role == "owner" else "\n\n🔒 Aap guest ho — sirf read-only tools chalenge."
        await _reply_long(update.effective_message,
                          f"🙌 Namaste {name}! Main **{S.ASSISTANT_NAME}** hoon.{extra}\n\n{HELP_TEXT}",
                          reply_markup=MAIN_KB())

    async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _role = await _guard(update)
        if not ok:
            return
        await _reply_long(update.effective_message, HELP_TEXT)

    async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        from ..runtime.status import agent_status_text

        await _reply_long(update.effective_message, agent_status_text(compact=True))

    async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        from ..config import runtime as R

        args = list(ctx.args or [])
        if not args:
            await update.effective_message.reply_text(
                f"Default interface preference: {R.get('mode', 'text')}\n"
                "Current interface: Telegram text (this preference does not start voice or another bot).\n"
                "Usage: /mode text|voice|telegram"
            )
            return
        if role != "owner":
            await update.effective_message.reply_text("🔒 sirf owner mode badal sakta hai.")
            return
        if len(args) != 1:
            await update.effective_message.reply_text("Usage: /mode text|voice|telegram")
            return
        arg = str(args[0]).lower().strip()
        applied, errors = R.set_many(mode=arg)
        if errors or applied.get("mode") != arg:
            detail = "; ".join(errors) or "runtime did not confirm the requested value"
            await update.effective_message.reply_text("⚠️ Mode preference save nahi hui; current setting unchanged. " + detail)
            return
        note = (" Telegram chat text-based hi rahega; mic/LiveKit connection start nahi hua. For voice, explicitly run "
                "python -m jarvis voice console or python -m jarvis voice dev." if arg == "voice" else
                " Current Telegram chat remains text; no interface was started or switched.")
        await update.effective_message.reply_text(f"✅ Default interface preference {arg} successfully saved.{note}")

    async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        if role != "owner":
            await update.effective_message.reply_text("🔒 sirf owner model badal sakta hai.")
            return
        from ..runtime.controls import model_provider_status, save_model_preference

        args = list(ctx.args or [])
        if not args:
            info = model_provider_status()
            available = ", ".join(info["available_providers"]) or "none"
            await update.effective_message.reply_text(
                f"Brain model preference: {info['model_preference']}\n"
                f"Provider preference: {info['provider_preference']} • active configured provider: {info['active_provider']}\n"
                f"Brain route: {info['brain_route']}\nProviders with keys: {available}\n"
                "Provider connectivity: not tested. Change with /model <name>."
            )
            return
        saved, message = save_model_preference(" ".join(str(arg) for arg in args))
        await update.effective_message.reply_text(("✅ " if saved else "⚠️ ") + message)

    async def cmd_provider(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        if role != "owner":
            await update.effective_message.reply_text("🔒 sirf owner provider badal sakta hai.")
            return
        from ..runtime.controls import model_provider_status, save_provider_preference

        args = list(ctx.args or [])
        if not args:
            info = model_provider_status()
            available = ", ".join(info["available_providers"]) or "none"
            await update.effective_message.reply_text(
                f"Provider preference: {info['provider_preference']}\n"
                f"Active configured provider: {info['active_provider']}\n"
                f"Brain route: {info['brain_route']}\nProviders with keys: {available}\n"
                "Provider connectivity: not tested. Change with /provider <name>."
            )
            return
        if len(args) != 1:
            await update.effective_message.reply_text("Usage: /provider gemini|openrouter|groq|openai|anthropic|xai|ollama|auto")
            return
        saved, message = save_provider_preference(str(args[0]))
        await update.effective_message.reply_text(("✅ " if saved else "⚠️ ") + message)

    async def cmd_tools(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _role = await _guard(update)
        if not ok:
            return
        from ..tools import tool_policy_summary

        await update.effective_message.reply_text(f"🧰 {tool_policy_summary()}")

    async def cmd_apps(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        if role != "owner":
            await update.effective_message.reply_text("🔒 /apps sirf owner ke liye hai.")
            return
        from ..tools import app_finder

        args = list(ctx.args or [])
        action = args[0].lower() if args else "list"
        query = " ".join(args[1:]).strip()
        if action not in {"list", "forget", "clear"} or (action == "forget" and not query):
            await update.effective_message.reply_text("Usage: /apps [list|forget <query>|clear]")
            return
        try:
            result = await app_finder.manage_apps.ainvoke({"action": action, "query": query})
        except Exception as exc:
            log.exception("telegram apps command failed")
            await update.effective_message.reply_text(f"⚠️ App memory operation fail hua ({type(exc).__name__}); koi success claim nahi.")
            return
        await _reply_long(update.effective_message, str(result))

    async def cmd_gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        from ..runtime.controls import gate_mode_details, save_gate_mode

        args = list(ctx.args or [])
        behavior = {
            "off": "Dangerous-command approvals are disabled.",
            "risky": "Only classified dangerous/sensitive operations require approval; ordinary commands do not prompt.",
            "all": "Every command requires owner approval.",
        }
        if not args:
            info = gate_mode_details()
            await update.effective_message.reply_text(
                f"Dangerous-Command Gate: {info['mode']} (source: {info['source']}).\n"
                f"{behavior.get(info['mode'], '')}\nOwner-only change: /gate off|risky|all"
            )
            return
        if role != "owner":
            await update.effective_message.reply_text("🔒 Gate mode sirf owner badal sakta hai.")
            return
        if len(args) != 1:
            await update.effective_message.reply_text("Usage: /gate off|risky|all")
            return
        value = str(args[0]).lower().strip()
        saved, message = save_gate_mode(value)
        if saved and value == "off":
            message += " ⚠️ Dangerous-command approvals are off; one warning is emitted at startup when the process starts in this mode."
        await update.effective_message.reply_text(("✅ " if saved else "⚠️ ") + message)

    async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        from ..config import runtime as R

        if not ctx.args:
            snap = R.snapshot()
            rows = "\n".join(f"• `{k}` = {v}" for k, v in list(snap.items())[:18])
            await _reply_long(update.effective_message, f"⚙️ *Settings*\n{rows}\n\nBadlo: `/settings owner_name Nikhil`")
            return
        if role != "owner":
            await update.effective_message.reply_text("🔒 sirf owner settings badal sakta hai.")
            return
        if len(ctx.args) < 2:
            await update.effective_message.reply_text("Usage: `/settings <key> <value>`")
            return
        key, value = ctx.args[0].lower(), " ".join(ctx.args[1:])
        if key in {"model", "provider", "exec_approval"}:
            from ..runtime.controls import save_gate_mode, save_model_preference, save_provider_preference

            if key == "model":
                saved, message = save_model_preference(value)
            elif key == "provider":
                saved, message = save_provider_preference(value)
            else:
                saved, message = save_gate_mode(value)
            await update.effective_message.reply_text(("✅ " if saved else "⚠️ ") + message)
            return
        applied, errors = R.set_many(**{key: value})
        if errors:
            await update.effective_message.reply_text("⚠️ " + "; ".join(errors))
        else:
            await _reply_long(update.effective_message, f"✅ `{key}` = {applied.get(key)}")

    async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        from ..memory.manager import get_manager

        mgr = get_manager(user_id=_memory_user(update, role))
        arg = ctx.args[0].lower() if ctx.args else ""
        if arg == "pending":
            if role != "owner":
                await update.effective_message.reply_text("🔒 sirf owner pending sensitive memory dekh sakta hai.")
                return
            pending = mgr.pending()
            if not pending:
                await update.effective_message.reply_text("Koi pending sensitive memory nahi hai.")
                return
            body = "\n".join(
                f"• Pending ID: {row['id']}\n"
                f"  Save: /memory confirm {row['id']}  |  Cancel: /memory cancel {row['id']}"
                for row in pending
            )
            await _reply_long(update.effective_message, "⏳ Pending sensitive memories (5-minute expiry):\n" + body)
            return
        if arg in {"confirm", "cancel"}:
            if role != "owner":
                await update.effective_message.reply_text("🔒 sirf owner sensitive memory confirm/cancel kar sakta hai.")
                return
            if len(ctx.args) != 2:
                await update.effective_message.reply_text(f"Usage: /memory {arg} <exact pending ID>")
                return
            resolved = mgr.confirm_pending(confirmed=arg == "confirm", pending_id=ctx.args[1])
            prefix = "✅ " if resolved.get("action") == "confirmed" else (
                "🚫 " if resolved.get("action") == "cancelled" else "⚠️ "
            )
            await update.effective_message.reply_text(prefix + resolved.get("message", "Memory request resolve nahi hua."))
            return
        if arg == "search" and len(ctx.args) > 1:
            hits = mgr.recall(" ".join(ctx.args[1:]), limit=6)
            body = "\n".join(f"• [{h.kind}] {h.text[:120]}" for h in hits) or "kuch nahi mila"
            await _reply_long(update.effective_message, f"🔎 Memory search:\n{body}")
            return
        if arg == "delete" and len(ctx.args) > 1:
            if role != "owner":
                await update.effective_message.reply_text("🔒 sirf owner memory delete kar sakta hai.")
                return
            delete_args = [item for item in ctx.args[1:] if item.lower() != "--all"]
            res = mgr.forget(" ".join(delete_args), all_matches=("--all" in [x.lower() for x in ctx.args[1:]]))
            await update.effective_message.reply_text(("🗑️ " if res.get("ok") else "❌ ") + res["message"])
            return
        if arg == "all":
            if role != "owner":
                await update.effective_message.reply_text("🔒 sirf owner memory delete kar sakta hai.")
                return
            res = mgr.forget_all()
            await update.effective_message.reply_text(("🗑️ " if res.get("ok") else "❌ ") + res["message"])
            return
        rows = mgr.list_memories(limit=25)
        if not rows:
            await update.effective_message.reply_text("🧠 Memory khali hai. Bolo: `yaad rakh mera naam ...`",
                                            parse_mode=ParseMode.MARKDOWN)
            return
        body = "\n".join(f"• [{r['kind']}] {r['text'][:110]}  `{r['id'][:8]}`" for r in rows)
        await _reply_long(update.effective_message, f"🧠 *Memory* ({len(rows)})\n{body}")

    async def cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        if role != "owner":
            await update.effective_message.reply_text("🔒 Guest memory write disabled; kuch save nahi hua.")
            return
        from ..memory.manager import get_manager

        text = " ".join(ctx.args) if ctx.args else ""
        if not text:
            await update.effective_message.reply_text("Usage: `/remember mera birthday 5 August hai`")
            return
        res = get_manager(user_id=_memory_user(update, role)).remember(text)
        response = ("🧠 " if res.get("ok") else "⚠️ ") + res["message"]
        if res.get("action") == "pending_confirmation":
            pending_id = str(res.get("id") or "")
            response += (
                f"\nPending ID: {pending_id}\n"
                f"Save this exact item: /memory confirm {pending_id}\n"
                f"Cancel it: /memory cancel {pending_id}\n"
                "You may also reply 'haan, save karo' only when exactly one item is pending."
            )
        await update.effective_message.reply_text(response)

    async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok or role != "owner":
            await update.effective_message.reply_text("🔒 sirf owner memory delete kar sakta hai.")
            return
        from ..memory.manager import get_manager

        args = list(ctx.args or [])
        all_matches = any(item.lower() == "--all" for item in args)
        text = " ".join(item for item in args if item.lower() != "--all")
        if not text:
            await update.effective_message.reply_text("Usage: `/forget mera purana address`")
            return
        res = get_manager(user_id=_memory_user(update, role)).forget(text, all_matches=all_matches)
        await update.effective_message.reply_text(("🗑️ " if res.get("ok") else "❌ ") + res["message"])

    async def cmd_automation(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        from ..automation import scheduler as sch

        args = ctx.args or []
        sub = args[0].lower() if args else "list"
        if sub == "list" or not args:
            items = await asyncio.to_thread(sch.get_store().list)
            if not items:
                await update.effective_message.reply_text("⏰ Koi automation nahi. Bolo: `har subah 8 baje mausam batao`",
                                                parse_mode=ParseMode.MARKDOWN)
                return
            body = "\n".join(
                f"{'🟢' if i['enabled'] else '🔴'} `{i['id'][:8]}` {i['schedule']} → {i['task'][:60]}"
                for i in items)
            await _reply_long(update.effective_message, f"⏰ *Automations*\n{body}")
            return
        if role != "owner":
            await update.effective_message.reply_text("🔒 sirf owner automation badal sakta hai.")
            return
        if sub in ("off", "on") and len(args) > 1:
            res = await asyncio.to_thread(sch.get_store().set_enabled, args[1], sub == "on")
        elif sub == "run" and len(args) > 1:
            res = await asyncio.to_thread(sch.run_now, args[1])
        elif sub == "cancel" and len(args) > 1:
            res = await asyncio.to_thread(sch.get_store().cancel, args[1])
        else:
            res = {"ok": False, "message": "Usage: /automation list | off <id> | on <id> | run <id> | cancel <id>"}
        await update.effective_message.reply_text(("✅ " if res.get("ok") else "❌ ") + str(res.get("message")))

    async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok or role != "owner":
            await update.effective_message.reply_text("🔒 sirf owner refresh kar sakta hai.")
            return
        from ..tools.system_ops import refresh_system

        res = await asyncio.to_thread(refresh_system, True, True, True, True)
        await update.effective_message.reply_text("♻️ " + "\n".join(res["steps"]))

    async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await _guard(update)
        if not ok:
            return
        from ..memory.session_state import get_state

        st = get_state(user_id=_memory_user(update, _))
        st.data["task_history"] = []
        st.data["pending_steps"] = []
        st.save()
        await update.effective_message.reply_text("🧽 Session clear ho gaya (memory safe hai). Naya task bolo.")

    async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok or role != "owner":
            await update.effective_message.reply_text("🔒 sirf owner.")
            return
        from ..security.permissions import use_surface
        from ..tools.system_ops import clear_processes_authorized

        with use_surface("telegram_owner"):
            res = await clear_processes_authorized()
        await update.effective_message.reply_text(("🧯 " if res.get("ok") else "🚫 ") + str(res.get("message")))

    async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok or role != "owner":
            await update.effective_message.reply_text("🔒 sirf owner.")
            return
        from ..security.permissions import use_surface
        from ..tools.system_ops import restart_self

        with use_surface("telegram_owner"):
            res = await restart_self()
        if res.get("ok"):
            await update.effective_message.reply_text("✅ Restart handoff successful; process dobara start hoga.")
        else:
            await update.effective_message.reply_text("⚠️ Restart nahi hua: " + str(res.get("message")))

    async def cmd_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await _guard(update)
        if not ok:
            return
        from ..voice.livekit_agent import voice_status_text

        await update.effective_message.reply_text(voice_status_text())

    # --------------------------------------------------------- free text/media
    async def _prepare_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> tuple[str, str | None]:
        message = update.effective_message
        user_text = str(getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
        context_parts: list[str] = []
        quoted = getattr(message, "reply_to_message", None)
        if quoted is not None:
            quoted_text = str(getattr(quoted, "text", None) or getattr(quoted, "caption", None) or "").strip()
            if quoted_text:
                context_parts.append("Replying to this message:\\n" + quoted_text[:1600])
            else:
                context_parts.append("Reply context: user replied to a previous Telegram message without text.")

        media_kind = ""
        media_obj = None
        if getattr(message, "photo", None):
            media_kind, media_obj = "photo", message.photo[-1]
        else:
            for attr, label in (("document", "document"), ("voice", "voice note"),
                                ("audio", "audio"), ("video", "video"),
                                ("animation", "animation"), ("video_note", "video note")):
                candidate = getattr(message, attr, None)
                if candidate is not None:
                    media_kind, media_obj = label, candidate
                    break

        attachment_path: str | None = None
        if media_obj is not None:
            file_id = str(getattr(media_obj, "file_id", "") or "")
            if not file_id:
                raise ValueError("Telegram media file ID missing")
            original_name = str(getattr(media_obj, "file_name", "") or "")
            suffix = Path(original_name).suffix[:12] if original_name else (".jpg" if media_kind == "photo" else ".bin")
            fd, attachment_path = tempfile.mkstemp(prefix="nikhilai-telegram-", suffix=suffix)
            os.close(fd)
            try:
                remote_file = await ctx.bot.get_file(file_id)
                await remote_file.download_to_drive(custom_path=attachment_path)
            except Exception:
                try:
                    os.unlink(attachment_path)
                except OSError:
                    pass
                raise
            details = f"{media_kind} attachment"
            if original_name:
                details += f" named {original_name}"
            context_parts.append(
                f"User shared {details}. Temporary local path: {attachment_path}. "
                "Use an available registered file/image/audio tool if it supports this format; "
                "otherwise state the limitation clearly."
            )

        if user_text:
            context_parts.append("User request: " + user_text)
        elif media_obj is not None:
            context_parts.append(f"User asks you to inspect the attached {media_kind} and report what you can verify.")
        prompt = "\\n\\n".join(context_parts).strip()
        return prompt, attachment_path

    async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        try:
            text, attachment_path = await _prepare_message(update, ctx)
        except Exception as exc:
            await update.effective_message.reply_text(
                f"⚠️ Telegram attachment retrieve nahi hua ({type(exc).__name__}); file par koi action nahi hua."
            )
            return
        if not text:
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id if update.effective_user else chat_id
        await update.effective_chat.send_action(ChatAction.TYPING)

        # live thinking/planning/tool progress (user ki demand: "chup-chaap kaam na kare")
        from .live import LiveProgress, progress_enabled_for

        live = LiveProgress(update.effective_message, enabled=progress_enabled_for(role))
        await live.start()

        started = time.time()
        try:
            from .tools import use_telegram_context

            with use_telegram_context(ctx.bot, chat_id, user_id, role):
                result = await run_agent(text, chat_id, role, on_progress=live.on_event, user_id=user_id)
        except Exception as exc:
            log.exception("telegram agent failed")
            await live.fail(exc, where="agent")
            return
        finally:
            if attachment_path:
                try:
                    Path(attachment_path).unlink(missing_ok=True)
                except OSError:
                    log.warning("failed to remove temporary Telegram attachment")

        answer = str(result.get("response") or result.get("answer") or "(koi jawab nahi bana)")
        workers = ", ".join(result.get("selected_workers") or result.get("workers") or [])
        footer = f"\n\n— {workers or 'direct'} • {time.time() - started:.1f}s"
        errors = [str(e) for e in (result.get("errors") or []) if str(e).strip()]
        await live.finish(answer, footer, errors=errors)

    # ------------------------------------------------------- buttons (inline)
    def _kb(*rows: list[tuple[str, str]]):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows])

    MAIN_KB = lambda: _kb(  # noqa: E731
        [("📊 Status", "cmd:status"), ("🧰 Tools", "cmd:tools")],
        [("⚙️ Settings", "cmd:settings"), ("🧠 Memory", "cmd:memory")],
        [("🔁 Refresh", "cmd:refresh"), ("🎛️ Mode", "cmd:mode")],
        [("⏰ Automations", "cmd:automation"), ("📱 Apps", "cmd:apps")],
        [("🛡️ Gate", "cmd:gate"), ("❓ Help", "cmd:help_btn")],
    )

    async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await _guard(update)
        if not ok:
            return
        await update.effective_message.reply_text("🎛️ *Control panel* — button dabao ya seedha likho:",
                                        parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_KB())

    async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Owner-bound approval/action callbacks plus shared command button routes."""
        q = update.callback_query
        raw = str(q.data or "")
        user = getattr(q, "from_user", None)
        chat = getattr(getattr(q, "message", None), "chat", None)
        user_id = str(getattr(user, "id", ""))
        chat_id = str(getattr(chat, "id", ""))
        allowed, role = authorize(user_id or "0", chat_id or "0")

        if raw.startswith("gate:"):
            if not allowed or role != "owner":
                await q.answer("Sirf configured owner approval de sakta hai.", show_alert=True)
                return
            await q.answer()
            parts = raw.split(":")
            if len(parts) != 3 or parts[2] not in {"approve", "cancel"}:
                await q.message.reply_text("⚠️ Approval button invalid ya expire ho gaya; command execute nahi hua.")
                return
            allow = parts[2] == "approve"
            from ..security import gate

            resolved = await gate.BROKER.resolve(parts[1], allow, user_id, surface="telegram_owner")
            if not resolved:
                await q.message.reply_text("⚠️ Approval expire/consume ho chuka hai; command execute nahi hua.")
                return
            status = "✅ Approved. Command ab execute ho raha hai…" if allow else "🚫 Cancelled. Command execute nahi hoga."
            try:
                await q.message.edit_text(status, reply_markup=None)
            except Exception:
                await q.message.reply_text(status)
            return

        if raw.startswith("tgact:"):
            if not allowed or role != "owner" or chat_id != user_id:
                await q.answer("Owner ke private chat me hi action chalega.", show_alert=True)
                return
            await q.answer()
            from .tools import consume_button_action, use_telegram_context

            action_id = raw.removeprefix("tgact:")
            action = consume_button_action(action_id, user_id=user_id, chat_id=chat_id)
            if not action:
                await q.message.reply_text("⚠️ Ye action expire ya pehle use ho chuka hai; dobara bhejne ko kahiye.")
                return
            from .live import LiveProgress, progress_enabled_for

            live = LiveProgress(q.message, enabled=progress_enabled_for(role))
            await live.start()
            try:
                with use_telegram_context(ctx.bot, chat_id, user_id, role):
                    result = await run_agent(action, chat_id, role, on_progress=live.on_event, user_id=user_id)
                answer = str(result.get("response") or result.get("answer") or "(koi jawab nahi bana)")
                errors = [str(item) for item in (result.get("errors") or []) if str(item).strip()]
                await live.finish(answer, "\\n\\n— Telegram button action", errors=errors)
            except Exception as exc:
                log.exception("telegram button action failed")
                await live.fail(exc, where="button action")
            return

        await q.answer()
        name = raw.split(":", 1)[1] if raw.startswith("cmd:") else raw
        handler = {
            "status": cmd_status, "tools": cmd_tools, "settings": cmd_settings,
            "memory": cmd_memory, "refresh": cmd_refresh, "mode": cmd_mode,
            "automation": cmd_automation, "apps": cmd_apps, "gate": cmd_gate,
        }.get(name)
        if name == "help_btn":
            await _reply_long(q.message, HELP_TEXT, reply_markup=MAIN_KB())
            return
        if handler is None:
            await q.message.reply_text("🤔 Ye button supported command se wired nahi hai.")
            return
        shim = _UpdateShim(q)
        try:
            await handler(shim, ctx)  # type: ignore[arg-type]
        except Exception as exc:
            log.exception("telegram button handler failed")
            await q.message.reply_text(f"⚠️ Button fail: {type(exc).__name__}: {str(exc)[:250]}")

    async def cmd_progress(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        from ..config import runtime as R

        if not ctx.args:
            cur = R.get("telegram_progress", "on")
            await update.effective_message.reply_text(
                f"📡 Live progress abhi: *{cur}*\nBadlo: `/progress on` ya `/progress off`",
                parse_mode=ParseMode.MARKDOWN)
            return
        val = ctx.args[0].lower()
        applied, errors = R.set_many(telegram_progress="on" if val in ("on", "1", "yes", "chalu") else "off")
        if errors:
            await update.effective_message.reply_text("⚠️ " + "; ".join(errors))
        else:
            await update.effective_message.reply_text(f"✅ Live progress: *{applied.get('telegram_progress')}*",
                                            parse_mode=ParseMode.MARKDOWN)

    approval_messages: dict[str, Any] = {}

    async def _ask_telegram_approval(request) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        owner_id = str(S.TELEGRAM_OWNER_ID or "").strip()
        if not owner_id:
            raise RuntimeError("TELEGRAM_OWNER_ID is required for Telegram approvals")
        from .md import html_chunks

        text = f"""**⚠️ Dangerous command approval required**
**Why:** {request.why_flagged}
**Working directory:** `{request.cwd}`
**Command:**
```text
{request.command}
```
Approve sirf is exact command ke ek run ko allow karega."""
        chunks = html_chunks(text, limit=3900)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve once", callback_data=f"gate:{request.id}:approve"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"gate:{request.id}:cancel"),
        ]])
        message = None
        for index, chunk in enumerate(chunks):
            message = await app.bot.send_message(
                chat_id=int(owner_id), text=chunk, parse_mode="HTML",
                reply_markup=keyboard if index == len(chunks) - 1 else None,
                disable_web_page_preview=True,
            )
        approval_messages[str(request.id)] = message

    async def _complete_telegram_approval(request_id: str, status: str, result: str) -> None:
        message = approval_messages.pop(str(request_id), None)
        if message is None:
            return
        from .md import html_chunks

        label = "approved" if str(status).lower() in {"ran", "done", "ok", "approved"} else str(status).lower()
        body = f"""**Approval {label}**
{str(result or '(no result)')}"""
        chunks = html_chunks(body, limit=3900)
        try:
            await message.edit_text(chunks[0], parse_mode="HTML", reply_markup=None,
                                    disable_web_page_preview=True)
        except Exception:
            await message.reply_text(chunks[0], parse_mode="HTML", disable_web_page_preview=True)
        for chunk in chunks[1:]:
            await message.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)

    from ..security import gate as command_gate

    delivery_loop: asyncio.AbstractEventLoop | None = None

    async def _send_automation_event(event: dict[str, Any]) -> None:
        from .md import html_chunks

        progress = event.get("progress") or []
        progress_text = "\n".join(
            f"• {str(item.get('text') or item.get('kind') or 'progress')}"
            for item in progress if isinstance(item, dict)
        )
        result = event.get("result") or event.get("error") or "(no output)"
        text = f"""**⏰ Automation {event.get('status') or 'unknown'}**
**Task:** {event.get('task') or ''}
**Run:** `{event.get('run_id') or ''}`
**Result:**
{result}"""
        if progress_text:
            text += f"\n**Progress:**\n{progress_text}"
        for chunk in html_chunks(text, limit=3900):
            await app.bot.send_message(
                chat_id=int(S.TELEGRAM_OWNER_ID), text=chunk, parse_mode="HTML",
                disable_web_page_preview=True,
            )

    def _deliver_automation_event(event: dict[str, Any]) -> None:
        if not S.TELEGRAM_OWNER_ID:
            raise RuntimeError("TELEGRAM_OWNER_ID missing; automation result has no configured destination")
        coroutine = _send_automation_event(event)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is delivery_loop:
            current_loop.create_task(coroutine)
        elif delivery_loop is not None and delivery_loop.is_running():
            asyncio.run_coroutine_threadsafe(coroutine, delivery_loop).result(timeout=30)
        else:
            asyncio.run(coroutine)

    async def _post_init(application) -> None:
        nonlocal delivery_loop
        delivery_loop = asyncio.get_running_loop()
        from ..automation import scheduler as sch

        command_gate.register_approval_handler("telegram_owner", _ask_telegram_approval)
        command_gate.register_approval_completion_handler("telegram_owner", _complete_telegram_approval)
        sch.configure_agent_runner()
        if S.TELEGRAM_OWNER_ID:
            sch.register_delivery_handler("telegram_owner", _deliver_automation_event)
        sch.start_background()

    async def _post_shutdown(application) -> None:
        from ..automation import scheduler as sch

        sch.stop_background()
        sch.register_delivery_handler("telegram_owner", None)
        command_gate.register_approval_handler("telegram_owner", None)
        command_gate.register_approval_completion_handler("telegram_owner", None)

    app = (Application.builder()
           .token(S.TELEGRAM_BOT_TOKEN)
           .concurrent_updates(True)
           .post_init(_post_init)
           .post_shutdown(_post_shutdown)
           .build())
    for name, fn in (("start", cmd_start), ("help", cmd_help), ("status", cmd_status), ("mode", cmd_mode),
                     ("model", cmd_model), ("provider", cmd_provider), ("tools", cmd_tools),
                     ("apps", cmd_apps), ("gate", cmd_gate),
                     ("settings", cmd_settings), ("memory", cmd_memory), ("remember", cmd_remember),
                     ("forget", cmd_forget), ("automation", cmd_automation), ("refresh", cmd_refresh),
                     ("clear", cmd_clear), ("stop", cmd_stop), ("restart", cmd_restart), ("voice", cmd_voice),
                     ("menu", cmd_menu), ("buttons", cmd_menu), ("progress", cmd_progress)):
        app.add_handler(CommandHandler(name, fn))
    from telegram.ext import CallbackQueryHandler

    app.add_handler(CallbackQueryHandler(on_button))
    message_filter = (
        filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO
        | filters.VIDEO | filters.ANIMATION | filters.VIDEO_NOTE
    ) & ~filters.COMMAND
    app.add_handler(MessageHandler(message_filter, on_message))
    return app


class _UpdateShim:
    """Inline button ke callback ko command-handler jaisa dikhao (update.effective_message shim).

    Isse ek hi command function buttons aur text dono se chalta hai — duplicate code nahi.
    """

    def __init__(self, query):
        self.callback_query = query
        self.effective_user = query.from_user
        self.effective_chat = query.message.chat
        self.message = _MessageShim(query.message)
        self.effective_message = self.message


class _MessageShim:
    def __init__(self, message):
        self._m = message

    async def reply_text(self, *args, **kwargs):
        return await self._m.reply_text(*args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._m, item)


def run_bot(poll_interval: float = 1.0) -> int:
    """Blocking: bot ko long-polling par chalao."""
    ok, msg = config_ok()
    if not ok:
        print(f"❌ Telegram bot start nahi ho sakta: {msg}")
        return 1
    if msg != "ok":
        print(msg)
    logging.basicConfig(level=getattr(logging, S.LOG_LEVEL, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(f"🤖 {S.ASSISTANT_NAME} Telegram bot live — token configured "
          f"(owner: {S.TELEGRAM_OWNER_ID or 'not configured'})")
    print("   Telegram pe apne bot ko /start bhejo. Ctrl+C se graceful shutdown.")
    app = build_application()
    # Infinite bootstrap retry keeps transient network failures from ending the bot.
    # PTB's polling loop also restarts after recoverable request/transport errors.
    app.run_polling(poll_interval=poll_interval, bootstrap_retries=-1, drop_pending_updates=True)
    return 0
