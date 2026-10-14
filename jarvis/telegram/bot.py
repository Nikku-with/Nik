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
import time
from typing import Any

from ..config import settings as S
from ..security.permissions import check as perm_check
from ..voice.speech import for_speech

log = logging.getLogger("nikhilai.telegram")

HELP_TEXT = """🤖 *NikhilAI* — aapka AI operator (Telegram se)

Seedha Hinglish me likho, slash command ki zaroorat nahi:
• `chrome kholo`
• `aaj ka mausam batao`
• `meri resume pdf dhoondh ke padho`
• `ek script banao jo csv se report banaye`
• `yaad rakh mera favourite color black hai`

*Commands*
/status — agent ki halat (brain, model, tools, memory)
/mode text|voice — interface mode
/model <naam> · /provider <naam> — brain badlo
/settings [key value] — settings dekho/badlo
/tools — kitne tools live hain
/memory · /remember <baat> · /forget <baat>
/automation [list|off <id>|run <id>]
/refresh · /clear · /stop · /restart
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
        # kuch bhi configure nahi kiya → pehla user owner ban jata hai (warn karke)
        return True, "owner"
    return False, "blocked"


def surface_for(role: str) -> str:
    return "telegram_owner" if role == "owner" else "telegram_guest"


# ---------------------------------------------------------------------------
# agent bridge (ek hi runner: LangGraph)
# ---------------------------------------------------------------------------
async def run_agent(text: str, chat_id: int | str, role: str = "owner", on_progress=None) -> dict[str, Any]:
    """Telegram message → wahi LangGraph agent → result.

    `on_progress`: live thinking/tool feed (LiveProgress.on_event) — isse user ko
    real-time dikhta hai ki agent kya kar raha hai, aur error bhi surface hote hain.
    """
    from ..runtime.agent import run_agent as core_run

    session = f"telegram:{chat_id}"
    result = await core_run(text, session_id=session, surface=surface_for(role),
                            on_progress=on_progress)
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
        return True, ("⚠️ koi TELEGRAM_OWNER_ID/ALLOWED_CHAT_IDS set nahi — pehla user owner ban jayega. "
                      "Sahi tarika: apna numeric id daalo (Telegram pe @userinfobot se milega).")
    return True, "ok"


# ---------------------------------------------------------------------------
# bot
# ---------------------------------------------------------------------------
def build_application():
    """python-telegram-bot Application banao (commands + free-text handler)."""
    from telegram import Update
    from telegram.constants import ChatAction, ParseMode
    from telegram.ext import (Application, CommandHandler, ContextTypes,
                              MessageHandler, filters)

    async def _deny(update: Update) -> None:
        await update.effective_message.reply_text(
            "🚫 Aap is bot ke allowed users me nahi ho.\n"
            "Owner ko bolo apna Telegram ID `TELEGRAM_ALLOWED_USERS` me daale."
        )

    async def _reply_long(msg, text: str) -> None:
        """Telegram ka 4096 char limit — lambe jawab ko todo."""
        for chunk_start in range(0, len(text), 3800):
            await msg.reply_text(text[chunk_start:chunk_start + 3800], parse_mode=ParseMode.MARKDOWN)

    async def _guard(update: Update) -> tuple[bool, str]:
        user = update.effective_user
        chat = update.effective_chat
        allowed, role = authorize(user.id if user else 0, chat.id if chat else 0)
        if not allowed:
            await _deny(update)
        return allowed, role

    # ------------------------------------------------------------ commands
    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        name = S.OWNER_NAME if role == "owner" else "there"
        extra = "" if role == "owner" else "\n\n🔒 Aap guest ho — sirf read-only tools chalenge."
        await update.message.reply_text(
            f"🙌 Namaste {name}! Main *{S.ASSISTANT_NAME}* hoon.{extra}\n\n{HELP_TEXT}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_KB())

    async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

    async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        from ..runtime.status import agent_status_text

        await update.message.reply_text(agent_status_text(compact=True), parse_mode=ParseMode.MARKDOWN)

    async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        if role != "owner":
            await update.message.reply_text("🔒 sirf owner mode badal sakta hai.")
            return
        from ..config import runtime as R

        arg = (ctx.args[0].lower() if ctx.args else "")
        if arg not in ("text", "voice", "telegram"):
            await update.message.reply_text("Usage: `/mode text|voice|telegram`", parse_mode=ParseMode.MARKDOWN)
            return
        applied, errors = R.set_many(mode=arg)
        await update.message.reply_text(
            f"✅ mode = *{arg}*" + (f"\n(voice mode ke liye LiveKit keys chahiye)" if arg == "voice" else "")
            + (f"\n⚠️ {errors}" if errors else ""), parse_mode=ParseMode.MARKDOWN)

    async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        if role != "owner":
            await update.message.reply_text("🔒 sirf owner model badal sakta hai.")
            return
        from ..config import runtime as R
        from ..llm import health, reset_cache

        if not ctx.args:
            h = health()
            await update.message.reply_text(
                f"🧠 Active provider: `{h['active']}`\nModel: `{h['roles']['brain']}`\n\n"
                "Badalne ke liye: `/model <naam>` ya `/provider openrouter`",
                parse_mode=ParseMode.MARKDOWN)
            return
        applied, errors = R.set_many(model=" ".join(ctx.args))
        reset_cache()
        await update.message.reply_text(
            ("✅ model set: `" + " ".join(ctx.args) + "`") if not errors else f"⚠️ {errors}",
            parse_mode=ParseMode.MARKDOWN)

    async def cmd_provider(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok or role != "owner":
            await update.message.reply_text("🔒 sirf owner provider badal sakta hai.")
            return
        from ..config import runtime as R
        from ..llm import health, reset_cache

        if not ctx.args:
            await update.message.reply_text(f"Available: `{', '.join(health()['available'])}`\n"
                                            "Set: `/provider groq`", parse_mode=ParseMode.MARKDOWN)
            return
        applied, errors = R.set_many(provider=ctx.args[0].lower())
        reset_cache()
        await update.message.reply_text(("✅ provider = " + ctx.args[0] if not errors else f"⚠️ {errors}"),
                                        parse_mode=ParseMode.MARKDOWN)

    async def cmd_tools(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        from ..tools import tool_policy_summary

        await update.message.reply_text(f"🧰 {tool_policy_summary()}")

    async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        from ..config import runtime as R

        if not ctx.args:
            snap = R.snapshot()
            rows = "\n".join(f"• `{k}` = {v}" for k, v in list(snap.items())[:18])
            await update.message.reply_text(f"⚙️ *Settings*\n{rows}\n\nBadlo: `/settings owner_name Nikhil`",
                                            parse_mode=ParseMode.MARKDOWN)
            return
        if role != "owner":
            await update.message.reply_text("🔒 sirf owner settings badal sakta hai.")
            return
        if len(ctx.args) < 2:
            await update.message.reply_text("Usage: `/settings <key> <value>`")
            return
        key, value = ctx.args[0].lower(), " ".join(ctx.args[1:])
        applied, errors = R.set_many(**{key: value})
        if errors:
            await update.message.reply_text("⚠️ " + "; ".join(errors))
        else:
            await update.message.reply_text(f"✅ `{key}` = {applied.get(key)}", parse_mode=ParseMode.MARKDOWN)

    async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        from ..memory.manager import get_manager

        mgr = get_manager()
        arg = ctx.args[0].lower() if ctx.args else ""
        if arg == "search" and len(ctx.args) > 1:
            hits = mgr.recall(" ".join(ctx.args[1:]), limit=6)
            body = "\n".join(f"• [{h.kind}] {h.text[:120]}" for h in hits) or "kuch nahi mila"
            await update.message.reply_text(f"🔎 Memory search:\n{body}")
            return
        if arg == "delete" and len(ctx.args) > 1:
            res = mgr.forget(" ".join(ctx.args[1:]))
            await update.message.reply_text(("🗑️ " if res.get("ok") else "❌ ") + res["message"])
            return
        if arg == "all":
            res = mgr.forget_all()
            await update.message.reply_text(("🗑️ " if res.get("ok") else "❌ ") + res["message"])
            return
        rows = mgr.list_memories(limit=25)
        if not rows:
            await update.message.reply_text("🧠 Memory khali hai. Bolo: `yaad rakh mera naam ...`",
                                            parse_mode=ParseMode.MARKDOWN)
            return
        body = "\n".join(f"• [{r['kind']}] {r['text'][:110]}  `{r['id'][:8]}`" for r in rows)
        await update.message.reply_text(f"🧠 *Memory* ({len(rows)})\n{body}", parse_mode=ParseMode.MARKDOWN)

    async def cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await _guard(update)
        if not ok:
            return
        from ..memory.manager import get_manager

        text = " ".join(ctx.args) if ctx.args else ""
        if not text:
            await update.message.reply_text("Usage: `/remember mera birthday 5 August hai`")
            return
        res = get_manager().remember(text)
        await update.message.reply_text(("🧠 " if res.get("ok") else "⚠️ ") + res["message"])

    async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok or role != "owner":
            await update.message.reply_text("🔒 sirf owner memory delete kar sakta hai.")
            return
        from ..memory.manager import get_manager

        text = " ".join(ctx.args) if ctx.args else ""
        if not text:
            await update.message.reply_text("Usage: `/forget mera purana address`")
            return
        res = get_manager().forget(text, all_matches=("--all" in text))
        await update.message.reply_text(("🗑️ " if res.get("ok") else "❌ ") + res["message"])

    async def cmd_automation(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        from ..automation import scheduler as sch

        args = ctx.args or []
        sub = args[0].lower() if args else "list"
        if sub == "list" or not args:
            items = sch.get_store().list()
            if not items:
                await update.message.reply_text("⏰ Koi automation nahi. Bolo: `har subah 8 baje mausam batao`",
                                                parse_mode=ParseMode.MARKDOWN)
                return
            body = "\n".join(
                f"{'🟢' if i['enabled'] else '🔴'} `{i['id'][:8]}` {i['schedule']} → {i['task'][:60]}"
                for i in items)
            await update.message.reply_text(f"⏰ *Automations*\n{body}", parse_mode=ParseMode.MARKDOWN)
            return
        if role != "owner":
            await update.message.reply_text("🔒 sirf owner automation badal sakta hai.")
            return
        if sub in ("off", "on") and len(args) > 1:
            res = sch.get_store().set_enabled(args[1], sub == "on")
        elif sub == "run" and len(args) > 1:
            res = sch.run_now(args[1])
        elif sub == "cancel" and len(args) > 1:
            res = sch.get_store().cancel(args[1])
        else:
            res = {"ok": False, "message": "Usage: /automation list | off <id> | on <id> | run <id> | cancel <id>"}
        await update.message.reply_text(("✅ " if res.get("ok") else "❌ ") + str(res.get("message")))

    async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok or role != "owner":
            await update.message.reply_text("🔒 sirf owner refresh kar sakta hai.")
            return
        from ..tools.system_ops import refresh_system

        res = await asyncio.to_thread(refresh_system, True, True, True, True)
        await update.message.reply_text("♻️ " + "\n".join(res["steps"]))

    async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await _guard(update)
        if not ok:
            return
        from ..memory.session_state import get_state

        st = get_state()
        st.data["task_history"] = []
        st.data["pending_steps"] = []
        st.save()
        await update.message.reply_text("🧽 Session clear ho gaya (memory safe hai). Naya task bolo.")

    async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok or role != "owner":
            await update.message.reply_text("🔒 sirf owner.")
            return
        from ..tools.system_ops import clear_processes

        res = await asyncio.to_thread(clear_processes)
        await update.message.reply_text("🧯 " + str(res.get("message")))

    async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok or role != "owner":
            await update.message.reply_text("🔒 sirf owner.")
            return
        await update.message.reply_text("🔄 Restart ho raha hai… (systemd/supervisor ho to auto wapas aa jaunga)")
        from ..tools.system_ops import restart_self

        res = restart_self()
        if not res.get("ok"):
            await update.message.reply_text("⚠️ " + str(res.get("message")))

    async def cmd_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await _guard(update)
        if not ok:
            return
        from ..voice.livekit_agent import voice_status_text

        await update.message.reply_text(voice_status_text())

    # --------------------------------------------------------- free text
    async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        chat_id = update.effective_chat.id
        await update.effective_chat.send_action(ChatAction.TYPING)

        # live thinking/planning/tool progress (user ki demand: "chup-chaap kaam na kare")
        from .live import LiveProgress, progress_enabled_for

        live = LiveProgress(update.message, enabled=progress_enabled_for(role))
        await live.start()

        started = time.time()
        try:
            result = await run_agent(text, chat_id, role, on_progress=live.on_event)
        except Exception as exc:  # agent crash → user ko readable error + traceback tail
            log.exception("telegram agent failed")
            await live.fail(exc, where="agent")
            return

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
        [("⏰ Automations", "cmd:automation"), ("❓ Help", "cmd:help_btn")],
    )

    async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await _guard(update)
        if not ok:
            return
        await update.message.reply_text("🎛️ *Control panel* — button dabao ya seedha likho:",
                                        parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_KB())

    async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Inline button → same command function (koi duplicate logic nahi)."""
        q = update.callback_query
        await q.answer()
        data = (q.data or "").split(":", 1)[-1]
        handler = {
            "status": cmd_status, "tools": cmd_tools, "settings": cmd_settings,
            "memory": cmd_memory, "refresh": cmd_refresh, "mode": cmd_mode,
            "automation": cmd_automation,
        }.get(data)
        if data == "help_btn":
            await q.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_KB())
            return
        if handler is None:
            await q.message.reply_text("🤔 ye button abhi wired nahi hai.")
            return
        shim = _UpdateShim(q)
        try:
            await handler(shim, ctx)  # type: ignore[arg-type]
        except Exception as exc:  # button fail → error dikhao, chup na raho
            await q.message.reply_text(f"⚠️ Button fail: {type(exc).__name__}: {str(exc)[:250]}")

    async def cmd_progress(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ok, role = await _guard(update)
        if not ok:
            return
        from ..config import runtime as R

        if not ctx.args:
            cur = R.get("telegram_progress", "on")
            await update.message.reply_text(
                f"📡 Live progress abhi: *{cur}*\nBadlo: `/progress on` ya `/progress off`",
                parse_mode=ParseMode.MARKDOWN)
            return
        val = ctx.args[0].lower()
        applied, errors = R.set_many(telegram_progress="on" if val in ("on", "1", "yes", "chalu") else "off")
        if errors:
            await update.message.reply_text("⚠️ " + "; ".join(errors))
        else:
            await update.message.reply_text(f"✅ Live progress: *{applied.get('telegram_progress')}*",
                                            parse_mode=ParseMode.MARKDOWN)

    app = (Application.builder()
           .token(S.TELEGRAM_BOT_TOKEN)
           .concurrent_updates(True)
           .build())
    for name, fn in (("start", cmd_start), ("help", cmd_help), ("status", cmd_status), ("mode", cmd_mode),
                     ("model", cmd_model), ("provider", cmd_provider), ("tools", cmd_tools),
                     ("settings", cmd_settings), ("memory", cmd_memory), ("remember", cmd_remember),
                     ("forget", cmd_forget), ("automation", cmd_automation), ("refresh", cmd_refresh),
                     ("clear", cmd_clear), ("stop", cmd_stop), ("restart", cmd_restart), ("voice", cmd_voice),
                     ("menu", cmd_menu), ("buttons", cmd_menu), ("progress", cmd_progress)):
        app.add_handler(CommandHandler(name, fn))
    from telegram.ext import CallbackQueryHandler

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app


class _UpdateShim:
    """Inline button ke callback ko command-handler jaisa dikhao (update.message shim).

    Isse ek hi command function buttons aur text dono se chalta hai — duplicate code nahi.
    """

    def __init__(self, query):
        self.callback_query = query
        self.effective_user = query.from_user
        self.effective_chat = query.message.chat
        self.message = _MessageShim(query.message)


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
    print(f"🤖 {S.ASSISTANT_NAME} Telegram bot live — {S.TELEGRAM_BOT_TOKEN[:10]}… "
          f"(owner: {S.TELEGRAM_OWNER_ID or 'auto-detect'})")
    print("   Telegram pe apne bot ko /start bhejo. Ctrl+C se band.")
    app = build_application()
    app.run_polling(poll_interval=poll_interval, drop_pending_updates=True)
    return 0
