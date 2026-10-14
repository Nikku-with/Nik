"""
Interactive CLI — `python -m jarvis` / `python -m jarvis cli`
============================================================

Polished terminal UI: status panel, activity log, slash commands, aur seedha Hinglish input
(bina slash ke bhi kuch bhi bolo — agent samajh jata hai).

    /help  /status  /mode  /voice  /text  /model  /provider  /tools  /memory  /settings
    /refresh  /clear  /stop  /restart  /automation  /skills  /doctor  /quit
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from ..config import settings as S
from ..config import runtime as R
from ..runtime.agent import run_agent
from ..runtime.status import agent_status
from . import ui

COMMANDS: dict[str, str] = {
    "/help": "Ye madad list dikhao",
    "/status": "Agent ki halat (brain, model, tools, memory, voice, telegram)",
    "/mode text|voice|telegram": "Interface mode badlo",
    "/voice": "Voice mode ka status (LiveKit ke saath chalane ka tarika)",
    "/text": "Text mode par wapas aa jao",
    "/model [naam]": "Model dekho ya badlo (e.g. /model gemini-3.8-flash)",
    "/provider [naam]": "Brain provider dekho/badlo (gemini|openrouter|groq)",
    "/tools [group]": "Tools browser (group-wise) — /tools desktop",
    "/menu": "Button menu (↑/↓ + Enter) — tools, status, settings, automations",
    "/run <tool> [k=v ...]": "Koi bhi tool seedha chalao (schema auto) — /run system_info",
    "/dash": "Live dashboard ka header/metadata (brain, mode, tools, memory)",
    "console": "(slash ke bina bhi) seedha likho 'console chalao' → LiveKit voice console",
    "/memory [search|delete] [q]": "Memory dekho / dhoondho / delete",
    "/settings [key value]": "Settings dekho ya badlo",
    "/automation [list|on|off|run|cancel] [id]": "Scheduled tasks manage karo",
    "/skills": "SKILL.md workflows dikhao",
    "/refresh": "Config/tools/models dobara load (data safe)",
    "/clear": "Session clear (memory safe)",
    "/stop": "Background processes band karo",
    "/restart": "Jarvis restart karo",
    "/doctor": "Poora system check",
    "/reasoning on|off": "Reasoning summary dikhana band/chalu",
    "/quit": "Band karo",
}


def _status_dict() -> dict[str, Any]:
    st = agent_status(compact=True)
    return {
        "status": "ONLINE" if not st["offline"] else "OFFLINE / DEMO",
        "provider": st["provider"],
        "model": st["model"],
        "mode": st["mode"],
        "memory": st["memory"],
        "tools": f"{st.get('tool_count', '?')} enabled",
        "browser": st["browser"],
        "livekit": st["livekit"],
        "telegram": st["telegram"],
        "automations": st["automations"],
        "workspace": st["workspace"],
    }


def show_status() -> None:
    ui.status_panel(_status_dict())


# ---------------------------------------------------------------------------
# slash commands
# ---------------------------------------------------------------------------
async def handle_command(line: str, session_id: str) -> bool:
    """Return True = continue loop, False = quit."""
    parts = line.strip().split()
    cmd, args = parts[0].lower(), parts[1:]
    tail = " ".join(args)

    if cmd in ("/quit", "/exit", "/q"):
        ui.cprint("\n👋 Phir milenge boss!", ui.ACCENT)
        return False

    if cmd in ("/help", "/h", "/?"):
        ui.help_panel(COMMANDS)
        return True

    if cmd == "/status":
        show_status()
        return True

    if cmd == "/menu":
        from . import pro_ui

        def _show_tools() -> None:
            ui.cprint(pro_ui.tools_table(tail or None))

        def _browse_plans() -> None:
            from ..skills.loader import skills_prompt_block

            ui.cprint(skills_prompt_block(tail) or "📦 koi SKILL.md nahi mila", ui.ACCENT)

        ok = pro_ui.key_menu(
            "🎛️  NIKHILAI control panel",
            [
                ("s", "Status — brain, model, tools, memory", show_status),
                ("t", "Tools browser — groups + count", _show_tools),
                ("m", "Memory — recent entries", lambda: asyncio.create_task(handle_command("/memory", session_id))),
                (
                    "a",
                    "Automations — scheduled tasks",
                    lambda: asyncio.create_task(handle_command("/automation list", session_id)),
                ),
                ("g", "Settings — view/change", lambda: asyncio.create_task(handle_command("/settings", session_id))),
                ("v", "Voice (LiveKit) status", lambda: asyncio.create_task(handle_command("/voice", session_id))),
                (
                    "d",
                    "Doctor — system health check",
                    lambda: asyncio.create_task(handle_command("/doctor", session_id)),
                ),
                ("h", "Help — all commands", lambda: ui.help_panel(COMMANDS)),
                ("r", "Refresh config/tools", lambda: asyncio.create_task(handle_command("/refresh", session_id))),
                ("q", "Quit", lambda: None),
            ],
            subtitle="↑/↓ move • Enter run • hotkey letter • Esc back",
        )
        if not ok:
            ui.cprint("(keyboard menu ke liye real terminal chahiye — yeh list dekh lo)", ui.DIM)
            ui.help_panel(COMMANDS)
        return True

    if cmd == "/dash":
        from . import pro_ui

        dash = pro_ui.Dashboard(_header_meta())
        ui.cprint("Header metadata:")
        for k, v in _header_meta().items():
            ui.cprint(f"  {k}: {v}")
        return True

    if cmd in ("/run", "/tool"):
        if not args:
            ui.cprint("Usage: /run <tool> [key=value ...]   e.g. /run system_info", ui.ACCENT)
            ui.cprint("       /run write_file path=notes.txt content=hello", ui.DIM)
            ui.cprint("       /run search_inside_files query=jarvis  |  /run take_screenshot", ui.DIM)
            return True
        from . import pro_ui

        tool_name, rest = args[0], args[1:]
        payload: dict[str, Any] = {}
        raw_json = tail[len(tool_name) :].strip()
        if raw_json.startswith("{"):
            import json as _json

            try:
                payload = _json.loads(raw_json)
            except Exception as exc:
                ui.cprint(f"⚠️ JSON parse fail: {exc}", ui.WARN)
                return True
        else:
            for pair in rest:
                if "=" not in pair:
                    continue
                k, v = pair.split("=", 1)
                payload[k.strip()] = _coerce_arg(v.strip())
        schema = pro_ui.tool_schema(tool_name)
        if schema:
            ui.cprint(f"🔧 {tool_name} — args: " + ", ".join(schema.keys()), ui.DIM)
        try:
            out = await pro_ui.run_tool_async(tool_name, payload)
        except Exception as exc:  # tool crash → honest error
            ui.cprint(f"❌ {tool_name} fail: {type(exc).__name__}: {exc}", ui.BAD)
            return True
        ui.panel_text(out, title=f"🛠️ {tool_name}")
        return True

    if cmd == "/voice":
        from ..voice.livekit_agent import voice_status, voice_status_text

        ui.cprint(voice_status_text(), ui.ACCENT)
        st = voice_status()
        if not st["ready"]:
            ui.cprint("\nKeys chahiye?  `python -m jarvis voice keys`  (exact steps, 5 min ka kaam)", ui.DIM)
            ui.cprint("Bina keys test? `python -m jarvis voice test`", ui.DIM)
        else:
            ui.cprint(
                "\nChalao: `python -m jarvis voice console` (mic+speaker) ya "
                "`python -m jarvis voice dev` (LiveKit Cloud)",
                ui.DIM,
            )
        return True

    if cmd == "/text":
        R.set_many(mode="text")
        ui.cprint("✅ mode = text", ui.OK)
        return True

    if cmd == "/mode":
        if not tail:
            ui.cprint(f"Current mode: {R.get('mode', 'text')}", ui.ACCENT)
            return True
        applied, errors = R.set_many(mode=tail.strip().lower())
        if errors:
            ui.cprint_safe("⚠️ " + "; ".join(errors), ui.WARN)
        else:
            from ..runtime.modes import describe_mode, mode_entry_hint

            ui.cprint(f"✅ mode = {applied.get('mode')}", ui.OK)
            ui.cprint(f"   {describe_mode(applied.get('mode'))}", ui.DIM)
            if applied.get("mode") != "text":
                ui.cprint(f"   → {mode_entry_hint(applied.get('mode'))}", ui.DIM)
        return True

    if cmd == "/model":
        if not args:
            from ..llm import health

            h = health()
            ui.cprint(
                f"Brain: {h['roles']['brain']}\nAvailable providers: {', '.join(h['available']) or 'none'}", ui.ACCENT
            )
            return True
        from ..llm import reset_cache

        applied, errors = R.set_many(model=tail)
        reset_cache()
        ui.cprint("✅ model = " + tail if not errors else "⚠️ " + "; ".join(errors), ui.OK if not errors else ui.WARN)
        return True

    if cmd == "/provider":
        from ..llm import health, reset_cache

        if not args:
            h = health()
            ui.cprint(f"Active: {h['active']}\nKeys set: {', '.join(h['available']) or 'koi nahi'}", ui.ACCENT)
            return True
        applied, errors = R.set_many(provider=args[0].lower())
        reset_cache()
        ui.cprint(
            "✅ provider = " + args[0] if not errors else "⚠️ " + "; ".join(errors), ui.OK if not errors else ui.WARN
        )
        return True

    if cmd == "/tools":
        from . import pro_ui

        query = tail.strip().lower()
        groups = pro_ui.tool_groups()
        multi = {"all", "web", "browser"}  # overlapping groups — registry view me skip
        if not query:
            ui.tools_table({g: names for g, names in groups.items() if g not in multi})
            ui.cprint("Filter: /tools desktop  •  search: /tools memory  •  chalao: /run <tool> [k=v ...]", ui.DIM)
            return True
        if query in groups:
            ui.cprint(pro_ui.tools_table(query))
            return True
        # group naam nahi mila → tool-name search
        hits = [(g, n) for g, names in groups.items() if g not in multi for n in names if query in n]
        if hits:
            from rich.table import Table as _T  # type: ignore

            if pro_ui.HAS_RICH:
                t = _T(
                    title=f"🔎 '{query}' — {len(hits)} tool", border_style=ui.ACCENT, header_style=f"bold {ui.ACCENT}"
                )
                t.add_column("tool", style=ui.ACCENT)
                t.add_column("group", style=ui.DIM)
                t.add_column("args", style=ui.DIM)
                for g, n in hits:
                    t.add_row(n, g, ", ".join(pro_ui.tool_schema(n).keys()) or "-")
                pro_ui.Console().print(t)
            else:
                ui.cprint("\n".join(f"{n} ({g})" for g, n in hits))
            return True
        ui.cprint(f"❌ '{query}' naam ka group/tool nahi mila. Groups: {', '.join(groups)}", ui.WARN)
        return True

    if cmd == "/memory":
        from ..memory.manager import get_manager

        mgr = get_manager()
        sub = args[0].lower() if args else ""
        if sub == "search" and len(args) > 1:
            hits = mgr.recall(" ".join(args[1:]), limit=8)
            if not hits:
                ui.cprint("❌ kuch nahi mila", ui.WARN)
            for h in hits:
                ui.cprint_safe(f"• [{h.kind}] {h.text[:120]}", ui.ACCENT)
            return True
        if sub == "delete" and len(args) > 1:
            res = mgr.forget(" ".join(args[1:]))
            ui.cprint_safe(("🗑️ " if res.get("ok") else "🧐 ") + res["message"], ui.OK if res.get("ok") else ui.WARN)
            return True
        if sub == "all":
            res = mgr.forget_all()
            ui.cprint_safe(res["message"], ui.WARN)
            return True
        ui.memory_table(mgr.list_memories(limit=25))
        return True

    if cmd == "/settings":
        if not args:
            ui.settings_table(R.describe())
            return True
        if args[0].lower() == "reset":
            R.reset(args[1] if len(args) > 1 else None)
            ui.cprint("♻️ settings reset ho gayi", ui.OK)
            return True
        if len(args) < 2:
            ui.cprint("Usage: /settings <key> <value>   (list ke liye sirf /settings)", ui.WARN)
            return True
        key, value = args[0].lower(), " ".join(args[1:])
        applied, errors = R.set_many(**{key: value})
        if errors:
            ui.cprint_safe("⚠️ " + "; ".join(errors), ui.WARN)
        else:
            ui.cprint(f"✅ {key} = {applied.get(key)}", ui.OK)
            try:
                from ..llm import reset_cache

                reset_cache()
            except Exception:
                pass
        return True

    if cmd == "/automation":
        from ..automation import scheduler as sch

        sub = args[0].lower() if args else "list"
        if sub == "list":
            ui.automations_table(sch.get_store().list())
        elif sub in ("on", "off") and len(args) > 1:
            res = sch.get_store().set_enabled(args[1], sub == "on")
            ui.cprint_safe(("✅ " if res.get("ok") else "❌ ") + res["message"], ui.OK if res.get("ok") else ui.WARN)
        elif sub == "run" and len(args) > 1:
            res = sch.run_now(args[1])
            ui.cprint_safe(
                ("▶️ " if res.get("ok") else "❌ ") + str(res.get("message")), ui.OK if res.get("ok") else ui.WARN
            )
        elif sub == "cancel" and len(args) > 1:
            res = sch.get_store().cancel(args[1])
            ui.cprint_safe(("🗑️ " if res.get("ok") else "❌ ") + res["message"], ui.OK if res.get("ok") else ui.WARN)
        else:
            ui.cprint("Usage: /automation list | on <id> | off <id> | run <id> | cancel <id>", ui.WARN)
        return True

    if cmd == "/skills":
        from ..skills.loader import catalog

        ui.cprint_safe(catalog(), ui.ACCENT)
        return True

    if cmd == "/refresh":
        from ..tools.system_ops import refresh_system

        res = refresh_system(clear_caches=True)
        for step in res["steps"]:
            ui.activity("refresh", step, "verify" if res["ok"] else "warn")
        return True

    if cmd == "/clear":
        from ..memory.session_state import get_state

        st = get_state()
        st.data["task_history"] = []
        st.data["pending_steps"] = []
        st.data["last_error"] = ""
        st.save()
        ui.cprint("🧽 Session clear (memory aur notes safe hain)", ui.OK)
        return True

    if cmd == "/stop":
        from ..tools.system_ops import clear_processes

        res = clear_processes()
        ui.cprint("🧯 " + str(res.get("message")), ui.ACCENT)
        return True

    if cmd == "/restart":
        from ..tools.system_ops import restart_self

        ui.cprint("🔄 Restart ho raha hai…", ui.ACCENT)
        res = restart_self()
        ui.cprint_safe(str(res.get("message", "")), ui.WARN)
        return True

    if cmd == "/doctor":
        from .doctor import run_doctor

        run_doctor()
        return True

    if cmd == "/reasoning":
        val = (args[0].lower() if args else "on") in ("on", "true", "1", "haan")
        R.set_many(show_reasoning=val)
        ui.cprint(f"✅ reasoning summary {'ON' if val else 'OFF'}", ui.OK)
        return True

    ui.cprint(f"❓ '{cmd}' command nahi pata. /help dekho.", ui.WARN)
    return True


def _coerce_arg(value: str) -> Any:
    """`k=v` string ko sahi type me badlo (bool/int/float/str) — /run ke liye."""
    low = value.lower()
    if low in ("true", "yes", "on", "haan"):
        return True
    if low in ("false", "no", "off", "nahi"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _header_meta() -> dict[str, Any]:
    """Dashboard header ke liye ek jagah se metadata."""
    import os as _os

    try:
        from importlib.metadata import version as _v

        _version = _v("nikhilai")
    except Exception:
        _version = "2.3.0"
    from ..config import OFFLINE_MODE as _offline

    mem_count = "?"
    tools_count = "?"
    try:
        from ..memory.store import get_memory

        mem_count = str(len(get_memory().recent(limit=999)))
    except Exception:
        pass
    try:
        from ..tools import ALL_TOOLS

        tools_count = str(len(ALL_TOOLS))
    except Exception:
        pass
    return {
        "name": R.get("assistant_name") or "NIKHILAI",
        "version": _version,
        "model": R.get("model") or _os.environ.get("NIKHILAI_MODEL") or "gemini-3.8-flash",
        "provider": (R.get("provider") or _os.environ.get("PROVIDER") or "auto").upper(),
        "mode": R.get("mode", "text"),
        "tools": tools_count,
        "memory": mem_count,
        "live": not _offline,
    }


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------
async def run_cli(session_id: str = "cli", show_reasoning: bool | None = None, once: str | None = None) -> int:
    """Interactive REPL. `once` diya to sirf ek request chala kar exit."""
    from ..automation import scheduler as sch

    # scheduler ko agent se jodo (automation → graph)
    async def _runner(task: str, auto_id: str) -> str:
        res = await run_agent(task, session_id=f"automation:{auto_id}", surface="automation")
        return res.answer

    def _runner_sync(task: str, auto_id: str) -> str:
        try:
            return asyncio.run(_runner(task, auto_id))
        except RuntimeError:  # already running loop
            loop = asyncio.new_event_loop()
            return loop.run_until_complete(_runner(task, auto_id))

    sch.RUNNER = _runner_sync
    sch.start_background()

    if once:
        from . import pro_ui

        dash = pro_ui.Dashboard(_header_meta(), show_reasoning=bool(show_reasoning))
        show_prog = str(R.get("cli_progress", "on")).lower() not in ("off", "0", "false", "no", "band")
        with pro_ui.live_run(dash, enabled=show_prog):
            res = await run_agent(once, session_id=session_id, on_progress=dash.on_event)
        ui.answer_panel(res.answer, meta=f"{', '.join(res.workers) or 'direct'} • {res.seconds:.1f}s")
        if res.errors:
            ui.error_panel("\n".join(str(e)[:300] for e in res.errors[:4]), context=dash.events_trace(6))
        if show_reasoning if show_reasoning is not None else R.get("show_reasoning", True):
            ui.reasoning_panel(res.reasoning)
        return 0

    ui.banner(R.get("assistant_name", S.ASSISTANT_NAME), R.get("owner_name", S.OWNER_NAME))
    show_status()
    ui.cprint(
        f"\n[{ui.DIM}]Seedha Hinglish me likho — jaise 'chrome kholo' ya "
        f"'aaj ka mausam batao'. /help se commands dekho.[/]"
    )
    ui.cprint(
        f"[{ui.DIM}]Mode: {R.get('mode', 'text')}  •  brain: {S.PROVIDER}  •  "
        f"{'OFFLINE DEMO' if S.OFFLINE_MODE else 'LIVE'}[{ui.DIM}]\n"
    )

    while True:
        try:
            line = await asyncio.to_thread(input, "you › ")
        except (EOFError, KeyboardInterrupt):
            ui.cprint("\n👋 Bye!", ui.ACCENT)
            return 0
        line = (line or "").strip()
        if not line:
            continue

        if line.startswith("/"):
            try:
                if not await handle_command(line, session_id):
                    return 0
            except Exception as exc:
                ui.cprint(f"⚠️ command fail: {exc}", ui.BAD)
            continue

        # natural line jo local session start karti hai — jaise "console chalao" / "telegram"
        from .routing import route_natural_command

        routed = route_natural_command(line)
        if routed is not None:
            ui.cprint(f"🎛️  {routed.note}", ui.DIM)
            if routed.kind == "voice":
                from ..voice.livekit_agent import main as voice_main

                ui.cprint("🎙️  voice console shuru — mic se baat karo, Ctrl+C se wapas CLI me.", ui.ACCENT)
                try:
                    voice_main(routed.args or ["console"])
                except KeyboardInterrupt:
                    ui.cprint("\n⏹️  voice band — wapas CLI me.", ui.WARN)
                except Exception as exc:
                    ui.error_panel(f"voice mode fail: {type(exc).__name__}: {exc}")
                continue
            if routed.kind == "telegram":
                from ..telegram.bot import run_bot

                ui.cprint("📨 Telegram bot shuru — Ctrl+C se band.", ui.ACCENT)
                try:
                    run_bot()
                except KeyboardInterrupt:
                    ui.cprint("\n⏹️  bot band.", ui.WARN)
                except Exception as exc:
                    ui.error_panel(f"telegram fail: {type(exc).__name__}: {exc}")
                continue
            # baaki (doctor/menu) commands handle_command se hi chalao
            cmdline = (
                "/" + routed.kind if routed.kind in ("doctor", "menu") else f"/{routed.kind} {' '.join(routed.args)}"
            )
            try:
                if not await handle_command(cmdline, session_id):
                    return 0
            except Exception as exc:
                ui.cprint(f"⚠️ command fail: {exc}", ui.BAD)
            continue

        # normal request → agent (live dashboard ke saath)
        from . import pro_ui

        show_prog = str(R.get("cli_progress", "on")).lower() not in ("off", "0", "false", "no", "band")
        dash = pro_ui.Dashboard(_header_meta(), show_reasoning=bool(show_reasoning))
        with pro_ui.live_run(dash, enabled=show_prog) as live:
            try:
                res = await run_agent(line, session_id=session_id, on_progress=dash.on_event)
            except KeyboardInterrupt:
                live.__exit__(None, None, None)
                ui.cprint("\n⏹️  cancel kiya", ui.WARN)
                continue
            except Exception as exc:  # crash → honest error, dashboard band
                live.__exit__(None, None, None)
                ui.error_panel(f"{type(exc).__name__}: {exc}", context=dash.events_trace(6))
                continue
            live.tick()

        # graph-level events (jinke liye progress emit nahi hua) — activity log me
        for ev in (res.events or [])[:12]:
            node = ev.get("node") if isinstance(ev, dict) else None
            if node and node not in ("route", "final", "model_attempts"):
                ui.event_to_activity(node, ev.get("events") if isinstance(ev, dict) else [])

        meta = f"{', '.join(res.workers) or 'direct'} • {res.seconds:.1f}s"
        if res.artifacts:
            meta += f" • {len(res.artifacts)} artifact(s)"
        if dash.tools_used:
            meta += f" • {len(dash.tools_used)} tool call"
        ui.answer_panel(res.answer, meta=meta)
        if res.errors:
            ui.error_panel("\n".join(str(e)[:300] for e in res.errors[:4]), context=dash.events_trace(6))
        if show_reasoning if show_reasoning is not None else R.get("show_reasoning", True):
            ui.reasoning_panel(res.reasoning)
        ui.cprint()


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
    once = None
    if argv and not argv[0].startswith("-"):
        once = " ".join(argv)
    try:
        return asyncio.run(run_cli(once=once))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
