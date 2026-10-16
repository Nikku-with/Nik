"""
Interactive CLI — `python -m jarvis` / `python -m jarvis cli`
============================================================

Polished terminal UI: status panel, activity log, slash commands, aur seedha Hinglish input
(bina slash ke bhi kuch bhi bolo — agent samajh jata hai).

    /help  /status  /mode  /voice  /text  /model  /provider  /gate  /tools  /memory  /apps  /settings
    /trace [limit]  /refresh  /clear  /stop  /restart  /automation  /skills  /doctor  /quit
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import threading
import time
from datetime import datetime
from typing import Any

from ..config import runtime as R
from ..config import settings as S
from ..runtime.agent import run_agent
from ..runtime.status import agent_status
from . import ui

COMMANDS: dict[str, str] = {
    "/help": "Ye madad list dikhao",
    "/status": "Agent ki halat (brain, model, tools, memory, voice, telegram)",
    "/mode text|voice|telegram": "Interface mode badlo",
    "/voice": "Voice mode ka status (LiveKit ke saath chalane ka tarika)",
    "/text": "Text mode par wapas aa jao",
    "/model [naam]": "Brain model preference dekho/badlo (route/connectivity alag se report hoti hai)",
    "/provider [naam]": "Provider preference dekho/badlo (connectivity test nahi hota)",
    "/gate [off|risky|all]": "Dangerous-Command Gate status ya owner preference badlo",
    "/tools [group]": "Tools browser (group-wise) — /tools desktop",
    "/menu": "Button menu (↑/↓ + Enter) — tools, status, settings, automations",
    "/run <tool> [k=v ...]": "Koi bhi tool seedha chalao (schema auto) — /run system_info",
    "/dash": "Live dashboard ka header/metadata (brain, mode, tools, memory)",
    "/trace [limit]": "Last run ke sanitized stage timings (prompt/output content nahi)",
    "console": "(slash ke bina bhi) seedha likho 'console chalao' → LiveKit voice console",
    "/memory [search|delete] [q]": "Memory dekho / dhoondho / delete",
    "/apps [list|forget <query>|clear]": "Verified app-launch mappings dekho/manage karo",
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

_LAST_TRACE: list[str] = []
_TRACE_EVENT_LIMIT = 40
_TRACE_KINDS = frozenset({
    "start", "thinking", "plan", "model_start", "model_end", "tool_start", "tool_end",
    "verify", "trace", "warn", "error", "done",
})
_CLI_TASKS: set[asyncio.Task[Any]] = set()
_LOG = logging.getLogger("nikhilai.cli")


def _report_cli_task(task: asyncio.Task[Any]) -> None:
    _CLI_TASKS.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        _LOG.error("CLI menu action failed", exc_info=(type(error), error, error.__traceback__))


def _spawn_cli_task(coroutine) -> asyncio.Task[Any]:
    """Retain an async menu action until completion and log any unhandled error."""
    task = asyncio.create_task(coroutine)
    _CLI_TASKS.add(task)
    task.add_done_callback(_report_cli_task)
    return task


def _safe_trace_token(value: Any, default: str = "-") -> str:
    token = re.sub(r"[^A-Za-z0-9_.:/-]+", "_", str(value or "").strip())[:64]
    return token or default


def _build_trace_report(events: list[Any], result: Any = None, *,
                        status: str = "failed", elapsed_s: float = 0.0) -> list[str]:
    """Summarize measured spans without including request, prompt, tool arguments, or output."""
    event_rows = list(events or [])
    model_ends = [event for event in event_rows if getattr(event, "kind", "") == "model_end"]
    tool_ends = [event for event in event_rows if getattr(event, "kind", "") == "tool_end"]
    model_ms = sum(max(0, int(getattr(event, "ms", 0) or 0)) for event in model_ends)
    tool_ms = sum(max(0, int(getattr(event, "ms", 0) or 0)) for event in tool_ends)
    result_seconds = getattr(result, "seconds", None)
    try:
        wall_s = float(result_seconds) if result_seconds is not None and float(result_seconds) > 0 else max(0.0, float(elapsed_s))
    except (TypeError, ValueError):
        wall_s = max(0.0, float(elapsed_s))
    final_status = _safe_trace_token(getattr(result, "status", "") or status)
    lines = [
        f"Last run: status={final_status} wall={wall_s * 1000:.0f}ms",
        f"Spans: models={len(model_ends)}/{model_ms}ms tools={len(tool_ends)}/{tool_ms}ms (operation sums may overlap)",
    ]
    safe_events = [event for event in event_rows if getattr(event, "kind", "") in _TRACE_KINDS]
    for event in safe_events[-_TRACE_EVENT_LIMIT:]:
        kind = _safe_trace_token(getattr(event, "kind", "event"))
        label = _safe_trace_token(getattr(event, "tool", ""), default="")
        event_status = _safe_trace_token(getattr(event, "status", ""), default="")
        try:
            stamp = datetime.fromtimestamp(float(getattr(event, "ts", 0) or 0)).strftime("%H:%M:%S")
        except (OverflowError, OSError, TypeError, ValueError):
            stamp = "--:--:--"
        try:
            duration = max(0, int(getattr(event, "ms", 0) or 0))
        except (TypeError, ValueError):
            duration = 0
        parts = [stamp, kind]
        if label:
            parts.append(label)
        if event_status:
            parts.append(f"status={event_status}")
        if duration:
            parts.append(f"{duration}ms")
        lines.append(" ".join(parts))
    if not safe_events:
        lines.append("No visible stage events were captured.")
    return lines


def _capture_trace(dashboard: Any, result: Any = None, *, status: str = "failed", elapsed_s: float = 0.0) -> None:
    global _LAST_TRACE
    _LAST_TRACE = _build_trace_report(getattr(dashboard, "events", []), result,
                                      status=status, elapsed_s=elapsed_s)


def _status_dict() -> dict[str, Any]:
    st = agent_status(compact=True, surface="CLI/Text")
    return {
        "status": st["status"], "provider": st["provider"], "model": st["model"],
        "fast_model": st["fast_model"], "interface": "CLI / TEXT",
        "gate": f"{st.get('gate_mode', 'unknown')} ({st.get('gate_mode_source', 'unknown')})",
        "memory": st["memory"], "tools": f"{st.get('tool_count', '?')} registered",
        "browser": st["browser"], "livekit": st["livekit_status"],
        "telegram": st["telegram_status"], "automations": st["automations"],
        "workspace": st["workspace"], "provider_status": st.get("provider_status", "unknown"),
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

        ok = pro_ui.key_menu("🎛️  NIKHILAI control panel",
                             [("s", "Status — brain, model, tools, memory", show_status),
                              ("t", "Tools browser — groups + count", _show_tools),
                              ("m", "Memory — recent entries", lambda: _spawn_cli_task(
                                  handle_command("/memory", session_id))),
                              ("a", "Automations — scheduled tasks", lambda: _spawn_cli_task(
                                  handle_command("/automation list", session_id))),
                              ("g", "Settings — view/change", lambda: _spawn_cli_task(
                                  handle_command("/settings", session_id))),
                              ("v", "Voice (LiveKit) status", lambda: _spawn_cli_task(
                                  handle_command("/voice", session_id))),
                              ("d", "Doctor — system health check", lambda: _spawn_cli_task(
                                  handle_command("/doctor", session_id))),
                              ("h", "Help — all commands", lambda: ui.help_panel(COMMANDS)),
                              ("r", "Refresh config/tools", lambda: _spawn_cli_task(
                                  handle_command("/refresh", session_id))),
                              ("q", "Quit", lambda: None)],
                             subtitle="↑/↓ move • Enter run • hotkey letter • Esc back")
        if not ok:
            ui.cprint("(keyboard menu ke liye real terminal chahiye — yeh list dekh lo)", ui.DIM)
            ui.help_panel(COMMANDS)
        return True

    if cmd == "/dash":
        header = _header_meta()
        ui.cprint("Header metadata:")
        for k, v in header.items():
            ui.cprint(f"  {k}: {v}")
        return True

    if cmd == "/trace":
        if len(args) > 1:
            ui.cprint("Usage: /trace [number of recent stage events, 1–40]", ui.WARN)
            return True
        limit = 20
        if args:
            try:
                limit = int(args[0])
            except ValueError:
                limit = 0
            if not 1 <= limit <= _TRACE_EVENT_LIMIT:
                ui.cprint("Usage: /trace [number of recent stage events, 1–40]", ui.WARN)
                return True
        if not _LAST_TRACE:
            ui.cprint("Is CLI process me abhi koi run trace nahi hai.", ui.DIM)
            return True
        summary, event_lines = _LAST_TRACE[:2], _LAST_TRACE[2:]
        for trace_line in summary + event_lines[-limit:]:
            ui.cprint_safe(trace_line, ui.ACCENT)
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
        raw_json = tail[len(tool_name):].strip()
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
        from ..security.gate import authorize_command

        approval = await authorize_command(
            f"CLI /run tool={tool_name} args={payload!r}", str(S.WORKSPACE_DIR), force=True,
            surface="cli_owner", reason="direct CLI tool invocation",
        )
        if not approval.allowed:
            ui.cprint(f"🚫 Tool nahi chalaya: {approval.reason}", ui.WARN)
            return True
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
            ui.cprint("\nChalao: `python -m jarvis voice console` (mic+speaker) ya "
                      "`python -m jarvis voice dev` (LiveKit Cloud)", ui.DIM)
        return True

    if cmd == "/text":
        applied, errors = R.set_many(mode="text")
        if errors or applied.get("mode") != "text":
            ui.cprint_safe("⚠️ Text preference save nahi hui; current setting unchanged. " + "; ".join(errors), ui.WARN)
        else:
            ui.cprint("✅ Default interface preference saved: text • current interface: CLI / TEXT", ui.OK)
        return True

    if cmd == "/mode":
        if not tail:
            ui.cprint(f"Default interface preference: {R.get('mode', 'text')} • current interface: CLI / TEXT",
                      ui.ACCENT)
            return True
        requested = tail.strip().lower()
        applied, errors = R.set_many(mode=requested)
        if errors or applied.get("mode") != requested:
            detail = "; ".join(errors) or "runtime did not confirm the requested value"
            ui.cprint_safe("⚠️ Preference save nahi hui; current setting unchanged. " + detail, ui.WARN)
        else:
            ui.cprint_safe(f"✅ Default interface preference saved: {requested}; current interface remains CLI / TEXT.", ui.OK)
            if requested == "voice":
                ui.cprint_safe("Microphone/LiveKit was not started. Run `python -m jarvis voice console` explicitly.", ui.DIM)
            elif requested == "telegram":
                ui.cprint_safe("Telegram bot was not started; this CLI session remains text-based.", ui.DIM)
        return True

    if cmd == "/model":
        from ..runtime.controls import model_provider_status, save_model_preference

        if not args:
            info = model_provider_status()
            available = ", ".join(info["available_providers"]) or "none"
            ui.cprint_safe(
                f"Brain model preference: {info['model_preference']}\n"
                f"Provider preference: {info['provider_preference']} • active configured provider: {info['active_provider']}\n"
                f"Brain route: {info['brain_route']}\nProviders with keys: {available}\n"
                "Connectivity: not tested.", ui.ACCENT,
            )
            return True
        ok, message = save_model_preference(tail)
        ui.cprint_safe(("✅ " if ok else "⚠️ ") + message, ui.OK if ok else ui.WARN)
        return True

    if cmd == "/provider":
        from ..runtime.controls import model_provider_status, save_provider_preference

        if not args:
            info = model_provider_status()
            available = ", ".join(info["available_providers"]) or "none"
            ui.cprint_safe(
                f"Provider preference: {info['provider_preference']}\n"
                f"Active configured provider: {info['active_provider']}\n"
                f"Brain route: {info['brain_route']}\nProviders with keys: {available}\n"
                "Connectivity: not tested.", ui.ACCENT,
            )
            return True
        if len(args) != 1:
            ui.cprint_safe("Usage: /provider <gemini|openrouter|groq|openai|anthropic|xai|ollama|auto>", ui.WARN)
            return True
        ok, message = save_provider_preference(args[0])
        ui.cprint_safe(("✅ " if ok else "⚠️ ") + message, ui.OK if ok else ui.WARN)
        return True

    if cmd == "/gate":
        from ..runtime.controls import gate_mode_details, save_gate_mode

        behavior = {
            "off": "Dangerous-command approvals are disabled.",
            "risky": "Only classified dangerous/sensitive operations require approval; ordinary commands do not prompt.",
            "all": "Every command requires owner approval.",
        }
        if not args:
            info = gate_mode_details()
            ui.cprint_safe(
                f"Dangerous-Command Gate: {info['mode']} (source: {info['source']}).\n"
                f"{behavior.get(info['mode'], '')}\nUsage: /gate off|risky|all",
                ui.ACCENT if info["mode"] != "off" else ui.WARN,
            )
            return True
        if len(args) != 1:
            ui.cprint_safe("Usage: /gate off|risky|all", ui.WARN)
            return True
        ok, message = save_gate_mode(args[0])
        if ok and args[0].lower() == "off":
            message += " ⚠️ Dangerous-command approvals are off; the process emits one startup warning when started in this mode."
        ui.cprint_safe(("✅ " if ok else "⚠️ ") + message, ui.OK if ok else ui.WARN)
        return True

    if cmd == "/tools":
        from . import pro_ui

        query = tail.strip().lower()
        groups = pro_ui.tool_groups()
        multi = {"all", "web", "browser"}          # overlapping groups — registry view me skip
        if not query:
            ui.tools_table({g: names for g, names in groups.items() if g not in multi})
            ui.cprint("Filter: /tools desktop  •  search: /tools memory  •  "
                      "chalao: /run <tool> [k=v ...]", ui.DIM)
            return True
        if query in groups:
            ui.cprint(pro_ui.tools_table(query))
            return True
        # group naam nahi mila → tool-name search
        hits = [(g, n) for g, names in groups.items() if g not in multi
                for n in names if query in n]
        if hits:
            from rich.table import Table as _T  # type: ignore

            if pro_ui.HAS_RICH:
                t = _T(title=f"🔎 '{query}' — {len(hits)} tool", border_style=ui.ACCENT,
                       header_style=f"bold {ui.ACCENT}")
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

    if cmd == "/apps":
        from ..tools.app_finder import manage_apps

        sub = args[0].lower() if args else "list"
        if sub == "list":
            result = await manage_apps.ainvoke({"action": "list"})
        elif sub == "forget" and len(args) > 1:
            result = await manage_apps.ainvoke({"action": "forget", "query": " ".join(args[1:])})
        elif sub == "clear":
            result = await manage_apps.ainvoke({"action": "clear"})
        else:
            ui.cprint("Usage: /apps list | /apps forget <query/app> | /apps clear", ui.WARN)
            return True
        ui.cprint_safe(str(result), ui.ACCENT)
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
        if key in {"model", "provider", "exec_approval"}:
            from ..runtime.controls import save_gate_mode, save_model_preference, save_provider_preference

            if key == "model":
                ok, message = save_model_preference(value)
            elif key == "provider":
                ok, message = save_provider_preference(value)
            else:
                ok, message = save_gate_mode(value)
            ui.cprint_safe(("✅ " if ok else "⚠️ ") + message, ui.OK if ok else ui.WARN)
            return True
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
            for event in (res.get("progress") or [])[-4:]:
                if isinstance(event, dict):
                    ui.cprint_safe("  • " + str(event.get("text") or event.get("kind") or "progress"), ui.DIM)
            ui.cprint_safe(("▶️ " if res.get("ok") else "❌ ") + str(res.get("message")), ui.OK if res.get("ok") else ui.WARN)
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
        from ..tools.system_ops import clear_processes_authorized

        res = await clear_processes_authorized()
        ui.cprint("🧯 " + str(res.get("message")), ui.ACCENT)
        return True

    if cmd == "/restart":
        from ..tools.system_ops import restart_self

        ui.cprint("🔄 Restart approval check ho raha hai…", ui.ACCENT)
        res = await restart_self()
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
    """Header labels come from effective role routes and real DB counts."""
    try:
        from importlib.metadata import version as _v

        _version = _v("nikhilai")
    except Exception:
        _version = "2.3.0"
    status = agent_status(compact=True, surface="CLI/Text")
    return {
        "name": R.get("assistant_name") or "NIKHILAI",
        "version": _version,
        "model": status["model"],
        "fast_model": status["fast_model"],
        "provider": status["provider"].upper(),
        "interface": "CLI / TEXT",
        "tools": str(status.get("tool_count", "?")),
        "memory": status["memory"],
        # A configured key is not an active connection; no connectivity probe runs here.
        "live": bool(status.get("connection_tested", False)),
        "status": status["status"],
        "provider_status": status.get("provider_status", "not tested"),
    }


def _run_coroutine_sync(coroutine_factory):
    """Run one coroutine and always close its event loop, even if called inside another loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine_factory())

    result: dict[str, Any] = {}
    errors: list[BaseException] = []

    def _thread_main() -> None:
        try:
            result["value"] = asyncio.run(coroutine_factory())
        except BaseException as exc:  # propagate the original exception to the caller
            errors.append(exc)

    thread = threading.Thread(target=_thread_main, name="jarvis-automation-runner", daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result.get("value")


def _run_automation_sync(coroutine_factory):
    """Retry only an explicitly typed transient failure; never retry broad RuntimeErrors."""
    from ..automation.scheduler import TransientError

    for attempt in range(2):
        try:
            return _run_coroutine_sync(coroutine_factory)
        except TransientError:
            if attempt == 1:
                raise
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------
def _launch_local_interface(args: list[str]) -> int:
    """Run another loop-owning interface in its own Jarvis process."""
    import subprocess

    return subprocess.call([sys.executable, "-m", "jarvis", *args])


async def run_cli(session_id: str = "cli", show_reasoning: bool | None = None,
                  once: str | None = None) -> int:
    """Interactive REPL. `once` diya to sirf ek request chala kar exit."""
    from ..automation import scheduler as sch

    # scheduler ko agent se jodo (automation → graph)
    async def _runner(task: str, auto_id: str, run_id: str, origin_surface: str = "cli_owner") -> dict[str, Any]:
        res = await run_agent(
            task, session_id=f"automation:{auto_id}", surface=origin_surface or "cli_owner", run_id=run_id
        )
        return res.as_dict()

    def _runner_sync(task: str, auto_id: str, run_id: str, origin_surface: str = "cli_owner") -> dict[str, Any]:
        return _run_automation_sync(lambda: _runner(task, auto_id, run_id, origin_surface))

    sch.RUNNER = _runner_sync

    def _deliver_cli_automation(event: dict[str, Any]) -> None:
        status = "✅" if event.get("status") == "done" else "⚠️"
        detail = event.get("result") or event.get("error") or "No result"
        ui.cprint_safe(f"\n{status} Automation #{str(event.get('automation_id', ''))[:8]} "
                       f"({event.get('status')}): {str(detail)[:500]}", ui.ACCENT)

    sch.register_delivery_handler("cli_owner", _deliver_cli_automation)
    sch.start_background()

    if once:
        from . import pro_ui

        dash = pro_ui.Dashboard(_header_meta(), show_reasoning=bool(show_reasoning))
        show_prog = str(R.get("cli_progress", "on")).lower() not in ("off", "0", "false", "no", "band")
        trace_started = time.monotonic()
        _capture_trace(dash, status="running")
        try:
            with pro_ui.live_run(dash, enabled=show_prog):
                res = await run_agent(once, session_id=session_id, on_progress=dash.on_event)
        except KeyboardInterrupt:
            _capture_trace(dash, status="cancelled", elapsed_s=time.monotonic() - trace_started)
            raise
        except Exception:
            _capture_trace(dash, status="failed", elapsed_s=time.monotonic() - trace_started)
            raise
        _capture_trace(dash, res, elapsed_s=time.monotonic() - trace_started)
        ui.answer_panel(res.answer,
                        meta=f"status: {res.status} • {', '.join(res.workers) or 'direct'} • {res.seconds:.1f}s",
                        status=res.status)
        if res.errors:
            ui.error_panel("\n".join(str(e)[:300] for e in res.errors[:4]), context=dash.events_trace(6))
        if (show_reasoning if show_reasoning is not None else R.get("show_reasoning", True)):
            ui.reasoning_panel(res.reasoning)
        terminal = (res.status or "").lower()
        if terminal in {"failed", "error", "blocked", "cancelled", "partial_failure", "done_with_errors"}:
            return 1
        if res.errors or (res.execution_required and not res.verified_execution):
            return 1
        return 0

    ui.banner(R.get("assistant_name", S.ASSISTANT_NAME), R.get("owner_name", S.OWNER_NAME))
    show_status()
    ui.cprint(f"\n[{ui.DIM}]Seedha Hinglish me likho — jaise 'chrome kholo' ya "
              f"'aaj ka mausam batao'. /help se commands dekho.[/]")
    current = agent_status(compact=True, surface="CLI/Text")
    ui.cprint(f"[{ui.DIM}]Interface: CLI / TEXT  •  brain: {current['provider']} → {current['model']}  •  "
              f"{current['status']}[/{ui.DIM}]\n")

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
                ui.cprint("🎙️  voice console shuru — mic se baat karo, Ctrl+C se wapas CLI me.", ui.ACCENT)
                try:
                    code = _launch_local_interface(routed.args or ["console"])
                    if code:
                        ui.cprint(f"⚠️ Voice process exit code {code}.", ui.WARN)
                except KeyboardInterrupt:
                    ui.cprint("\n⏹️  voice band — wapas CLI me.", ui.WARN)
                except Exception as exc:
                    ui.error_panel(f"voice mode fail: {type(exc).__name__}: {exc}")
                continue
            if routed.kind == "telegram":
                ui.cprint("📨 Telegram bot shuru — Ctrl+C se band.", ui.ACCENT)
                try:
                    code = _launch_local_interface(["telegram"])
                    if code:
                        ui.cprint(f"⚠️ Telegram process exit code {code}.", ui.WARN)
                except KeyboardInterrupt:
                    ui.cprint("\n⏹️  bot band.", ui.WARN)
                except Exception as exc:
                    ui.error_panel(f"telegram fail: {type(exc).__name__}: {exc}")
                continue
            # baaki (doctor/menu) commands handle_command se hi chalao
            cmdline = "/" + routed.kind if routed.kind in ("doctor", "menu") else f"/{routed.kind} {' '.join(routed.args)}"
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
        trace_started = time.monotonic()
        _capture_trace(dash, status="running")
        with pro_ui.live_run(dash, enabled=show_prog) as live:
            try:
                res = await run_agent(line, session_id=session_id, on_progress=dash.on_event)
            except KeyboardInterrupt:
                _capture_trace(dash, status="cancelled", elapsed_s=time.monotonic() - trace_started)
                live.__exit__(None, None, None)
                ui.cprint("\n⏹️  cancel kiya", ui.WARN)
                continue
            except Exception as exc:  # crash → honest error, dashboard band
                _capture_trace(dash, status="failed", elapsed_s=time.monotonic() - trace_started)
                live.__exit__(None, None, None)
                ui.error_panel(f"{type(exc).__name__}: {exc}",
                               context=dash.events_trace(6))
                continue
            live.tick()
        _capture_trace(dash, res, elapsed_s=time.monotonic() - trace_started)

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
        ui.answer_panel(res.answer, meta=f"status: {res.status} • {meta}", status=res.status)
        if res.errors:
            ui.error_panel("\n".join(str(e)[:300] for e in res.errors[:4]),
                           context=dash.events_trace(6))
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
