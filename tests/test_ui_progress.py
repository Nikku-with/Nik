"""
tests/test_ui_progress.py — live progress / CLI pro-UI / Telegram error-visibility suite
=======================================================================================

Chalao:  python tests/test_ui_progress.py     (pytest bhi chalega)

Ye test un cheezon ko verify karta hai jinki shikayat user ne ki thi:
  ✅ thinking / planning / tool-progress events asli me nikalte hain (CollectingSink + callback)
  ✅ Telegram live progress: sync-safe on_event, HTML escaping, error block, crash traceback
  ✅ Markdown → Telegram HTML converter (bold/code/heading/link + injection-safe)
  ✅ CLI pro dashboard: header, live stream, plan/tools side panel, plain fallback (no TTY)
  ✅ `/run` tool runner: koi bhi tool schema ke saath direct chalta hai
  ✅ tool schemas LLM-callable hain (v__args / varargs bug wapas na aaye) — saare 77
  ✅ restart safety: library se `os.execv` NAHI hota (process kill bug)
  ✅ voice console: keys guide + bina-keys pipeline test (spoken text clean)
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JARVIS_OFFLINE_MODE", "1")
os.environ.setdefault("PROVIDER", "demo")
os.environ.setdefault("NIKHILAI_MAIN_PROCESS", "0")  # restart test: exec kabhi na ho

PASS, FAIL = "✅", "❌"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
async def t_progress_core() -> None:
    from jarvis.runtime.progress import CallbackSink, ProgressEvent, emit, use_sink
    from tests.support import CollectingSink

    sink = CollectingSink()
    with use_sink(sink):
        emit("thinking", "soch raha hoon")
        emit("tool_start", "system_info chala raha hoon", tool="system_info")
        emit("error", "boom", status="fail")
    check("progress: events sink tak pahunche", len(sink.events) == 3, f"{len(sink.events)} events")
    check("progress: icons/line format", all(ev.line() for ev in sink.events))
    check("progress: errors() filter", len(sink.errors()) == 1)

    seen: list[ProgressEvent] = []
    with use_sink(CallbackSink(seen.append)):
        emit("plan", "3 steps")
    check("progress: CallbackSink delivers", len(seen) == 1)

    def boom(_ev):  # progress callback kabhi main flow na toote
        raise RuntimeError("callback crash")

    with use_sink(CallbackSink(boom)):
        ev = emit("done", "khatam")
    check("progress: callback crash swallow hota hai", ev.kind == "done")

    # sink ke bahar emit = silent no-op (library use me kuch print na ho)
    out = emit("thinking", "kuch nahi hona chahiye")
    check("progress: bina sink = silent no-op", isinstance(out, ProgressEvent))


async def t_agent_progress() -> None:
    from jarvis.runtime.agent import run_agent

    seen = []
    res = await run_agent("aaj ki date batao", session_id="prog-test",
                          on_progress=seen.append)
    kinds = [e.kind for e in seen]
    check("agent: on_progress events aate hain", len(seen) >= 4, f"{len(seen)} events")
    check("agent: start event pehla", kinds[0] == "start", kinds[:3])
    check("agent: done event aakhri", kinds[-1] == "done", kinds[-1:])
    check("agent: thinking events maujood", "thinking" in kinds)
    check("agent: answer bhi mila", bool(res.answer.strip()), res.answer[:60])


async def t_telegram_live() -> None:
    from jarvis.runtime.agent import run_agent
    from jarvis.telegram.live import LiveProgress

    class FakeMsg:
        def __init__(self):
            self.edits: list[tuple[str, str]] = []

        async def reply_text(self, text, **kw):
            self.edits.append(("SEND", text))
            return self

        async def edit_text(self, text, **kw):
            self.edits.append(("EDIT", text))
            return self

        async def delete(self):
            self.edits.append(("DEL", ""))

    # 1) sync on_event — coroutine warning nahi aani chahiye (pehle bug tha)
    fm = FakeMsg()
    live = LiveProgress(fm, enabled=True, min_interval=999)  # flusher ko chup rakho
    await live.start()
    from jarvis.runtime.progress import ProgressEvent

    live.on_event(ProgressEvent("tool_start", "open_any_app chala raha hoon", tool="open_any_app"))
    live.on_event(ProgressEvent("tool_end", "✅ khul gaya", tool="open_any_app", ms=300, status="ok"))
    live.on_event(ProgressEvent("tool_end", "❌ type_text fail: pyautogui missing", tool="type_text",
                                ms=12, status="fail"))
    check("tg: on_event sync-safe hai (koi await zaroori nahi)", len(live.events) == 3)
    check("tg: progress message bheja gaya", fm.edits and fm.edits[0][0] == "SEND")
    check("tg: live render me tool line hai", "open_any_app" in live._html())

    # 2) final answer + error block
    await live.finish("Kaam ho gaya", "\n\n— desktop • 1.2s", errors=["honesty_guard: all actions failed"])
    final = fm.edits[-1][1]
    check("tg: final message edit hua", fm.edits[-1][0] == "EDIT")
    check("tg: errors final message me visible", "Errors (1)" in final and "honesty_guard" in final)
    check("tg: failed step trail bhi dikhta hai", "Failed steps" in final and "type_text" in final)

    # 3) crash → traceback bhi visible
    fm2 = FakeMsg()
    live2 = LiveProgress(fm2, enabled=True, min_interval=999)
    await live2.start()
    try:
        raise ValueError("test crash")
    except ValueError as exc:
        await live2.fail(exc, where="agent")
    crash_msg = fm2.edits[-1][1]
    check("tg: crash par traceback show hota hai",
          "Traceback" in crash_msg and "ValueError" in crash_msg)

    # 4) disabled → sirf plain final (koi live spam nahi)
    fm3 = FakeMsg()
    live3 = LiveProgress(fm3, enabled=False)
    await live3.start()
    await live3.finish("plain jawab")
    check("tg: progress off hone par sirf final jata hai",
          len(fm3.edits) == 1 and fm3.edits[0][0] == "SEND")

    # 5) real agent → live progress end-to-end (demo)
    fm4 = FakeMsg()
    live4 = LiveProgress(fm4, enabled=True, min_interval=999)
    await live4.start()
    res = await run_agent("aaj ki date batao", session_id="tg-live-e2e", on_progress=live4.on_event)
    await live4.finish(str(res.answer)[:200], errors=[str(e) for e in (res.errors or [])])
    check("tg: agent ke events live message me gaye", len(live4.events) >= 4,
          f"{len(live4.events)} events")
    check("tg: settings se on/off padha jata hai",
          isinstance(__import__("jarvis.telegram.live", fromlist=["x"]).progress_enabled_for(), bool))


def t_md_to_html() -> None:
    from jarvis.telegram.md import chunk_text, md_to_html

    check("md: bold convert", "<b>bold</b>" in md_to_html("ye **bold** hai"))
    check("md: inline code", "<code>x=1</code>" in md_to_html("chalao `x=1`"))
    check("md: code block <pre>", "<pre>" in md_to_html("```python\nprint(1)\n```"))
    check("md: heading bold", "<b>Title</b>" in md_to_html("# Title"))
    check("md: link", '<a href="https://a.com"' in md_to_html("[site](https://a.com)"))
    check("md: bullets", "• item" in md_to_html("- item"))
    injected = md_to_html("<script>alert(1)</script> & <b>hi</b>")
    check("md: HTML injection escape (Telegram parse fail nahi hoga)",
          "<script>" not in injected and "&lt;script&gt;" in injected)
    chunks = chunk_text("para\n\n" * 900, limit=1000)
    check("md: chunking 4096-limit ke andar", all(len(c) <= 1000 for c in chunks) and len(chunks) > 1,
          f"{len(chunks)} chunks")


def t_cli_pro_ui() -> None:
    from jarvis.cli import pro_ui

    dash = pro_ui.Dashboard({"name": "NIKHILAI", "model": "gemini:gemini-3.8-flash",
                             "fast_model": "gemini:gemini-3.5-flash-lite", "tools": "77",
                             "memory": "5", "live": False, "interface": "CLI / TEXT",
                             "status": "CONFIGURED (connectivity not tested)"})
    from jarvis.runtime.progress import ProgressEvent

    dash.on_event(ProgressEvent("plan", "2 steps", detail="app kholo → verify"))
    dash.on_event(ProgressEvent("model_start", "Gemini request started", tool="gemini:gemini-3.8-flash"))
    dash.on_event(ProgressEvent("model_end", "Gemini response ok", tool="gemini:gemini-3.8-flash",
                                status="ok", ms=250))
    dash.on_event(ProgressEvent("tool_start", "open_any_app", tool="open_any_app"))
    dash.on_event(ProgressEvent("tool_end", "✅ khul gaya", tool="open_any_app", ms=120, status="ok"))
    dash.on_event(ProgressEvent("error", "type_text fail", tool="type_text", status="fail"))
    check("cli: dashboard plan track karta hai", len(dash.plan) == 2, str(dash.plan))
    check("cli: actual model calls/status track karta hai",
          dash.models_used == [("gemini:gemini-3.8-flash", "ok", 250)], str(dash.models_used))
    check("cli: dashboard tools track karta hai", len(dash.tools_used) == 1)
    check("cli: events_trace milta hai", len(dash.events_trace(5)) >= 3)
    if pro_ui.HAS_RICH:
        from rich.console import Console

        con = Console(width=100, record=True, file=open(os.devnull, "w"))
        con.print(dash.render())
        text = con.export_text()
        check("cli: dashboard render me thinking/model/context aur text-mode state aate hain",
              "NIKHILAI" in text and "Live activity" in text and "Context" in text
              and "Model calls" in text and "CLI / TEXT" in text and "VOICE" not in text
              and "CONFIGURED" in text)
    else:
        check("cli: dashboard render (rich missing — plain fallback)", True, "rich nahi hai")

    check("cli: key_menu non-TTY par safe False deta hai", pro_ui.key_menu("t", [("a", "x", lambda: None)]) is False)
    groups = pro_ui.tool_groups()
    check("cli: tool groups inventory milti hai", len(groups) >= 8 and sum(len(v) for v in groups.values()) >= 70,
          f"{len(groups)} groups")


async def t_tool_runner() -> None:
    from jarvis.cli import pro_ui

    out = await pro_ui.run_tool_async("system_info", {})
    check("/run: system_info direct chalta hai", "Python" in out or "🐍" in out or "Linux" in out, out[:50])
    bad = await pro_ui.run_tool_async("koi_tool_nahi", {})
    check("/run: unknown tool honest error deta hai", bad.startswith("❌"))
    schema = pro_ui.tool_schema("write_file")
    check("/run: schema introspection", "path" in schema and "content" in schema, str(list(schema)))
    from jarvis.cli.app import _coerce_arg

    check("/run: k=v type coercion", _coerce_arg("5") == 5 and _coerce_arg("true") is True
          and _coerce_arg("hi") == "hi")


def t_tool_schema_shapes() -> None:
    """LLM ko saare tools call kar paane chahiye — varargs/dodgy schema kabhi na ho."""
    from jarvis.tools import ALL_TOOLS

    bad = []
    empty = []
    for t in ALL_TOOLS:
        schema = getattr(t, "args_schema", None)
        if not schema:
            continue
        props = schema.model_json_schema().get("properties", {})
        if any(p.startswith("v__") or p in ("kwargs", "self") for p in props):
            bad.append(t.name)
        if not props:
            empty.append(t.name)
    check("tools: koi tool varargs/v__args schema nahi banata", not bad, ", ".join(bad[:5]))
    check("tools: koi tool schema-less nahi (LLM args bhej sake)",
          len(ALL_TOOLS) == len([t for t in ALL_TOOLS if getattr(t, "args_schema", None) is not None
                                 or not getattr(t, "args", None)]),
          f"{len(ALL_TOOLS)} tools")


async def t_restart_safety() -> None:
    from jarvis.tools.system_ops import restart_self

    old_allow = os.environ.get("NIKHILAI_ALLOW_RESTART")
    old_main = os.environ.get("NIKHILAI_MAIN_PROCESS")
    try:
        os.environ["NIKHILAI_ALLOW_RESTART"] = "1"
        os.environ["NIKHILAI_MAIN_PROCESS"] = "0"
        res = await restart_self()
        check("restart: library/script se exec NAHI hota", res.get("ok") is False
              and "sirf" in str(res.get("message", "")), str(res.get("message"))[:70])
        os.environ["NIKHILAI_ALLOW_RESTART"] = "0"
        res2 = await restart_self()
        check("restart: NIKHILAI_ALLOW_RESTART=0 se band", res2.get("ok") is False
              and "disabled" in str(res2.get("message", "")))
    finally:
        if old_allow is None:
            os.environ.pop("NIKHILAI_ALLOW_RESTART", None)
        else:
            os.environ["NIKHILAI_ALLOW_RESTART"] = old_allow
        if old_main is None:
            os.environ.pop("NIKHILAI_MAIN_PROCESS", None)
        else:
            os.environ["NIKHILAI_MAIN_PROCESS"] = old_main


async def t_exec_approval_security() -> None:
    """A real fake subprocess proves a human denial/approval controls execution."""
    from unittest.mock import patch

    from jarvis.security.gate import use_approval_handler
    from jarvis.tools.system_tools import run_shell_command

    old_mode = os.environ.get("JARVIS_EXEC_APPROVAL")
    os.environ["JARVIS_EXEC_APPROVAL"] = "risky"
    calls = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"fake output", b""

    async def fake_shell(command, **_kwargs):
        calls.append(command)
        return FakeProcess()

    try:
        with patch("asyncio.create_subprocess_shell", fake_shell):
            with use_approval_handler(lambda _request: False):
                denied = await run_shell_command.ainvoke({"command": "rm -rf /tmp/gate-probe"})
            check("gate: denial means subprocess is never called", not calls and "nahi chalaya" in denied)
            with use_approval_handler(lambda _request: True):
                allowed = await run_shell_command.ainvoke({"command": "rm -rf /tmp/gate-probe"})
            check("gate: human yes runs exact command once", calls == ["rm -rf /tmp/gate-probe"]
                  and "fake output" in allowed)
            safe = await run_shell_command.ainvoke({"command": "echo safe-ok"})
            check("gate: normal command runs without approval", "fake output" in safe and len(calls) == 2)
        from jarvis.tools.system_tools import run_shell_command as shell_tool
        check("gate: model-visible schema has no confirmed input", "confirmed" not in shell_tool.args)
    finally:
        if old_mode is None:
            os.environ.pop("JARVIS_EXEC_APPROVAL", None)
        else:
            os.environ["JARVIS_EXEC_APPROVAL"] = old_mode


def t_settings_surface() -> None:
    from jarvis.config import runtime as R

    snap = R.snapshot()
    for key in ("telegram_progress", "cli_progress", "cli_theme"):
        check(f"settings: {key} registered", key in snap, str(snap.get(key)))
    applied, errors = R.set_many(telegram_progress="off")
    off_val = applied.get("telegram_progress")
    check("settings: telegram_progress badla ja sakta hai",
          not errors and str(off_val).lower() in ("off", "false", "0"), str(off_val))
    from jarvis.telegram.live import progress_enabled_for

    check("settings: off hone par progress band", progress_enabled_for() is False)
    R.set_many(telegram_progress="on")
    check("settings: on karne par wapas chalu", progress_enabled_for() is True)


def t_voice_console() -> None:
    from jarvis.voice.livekit_agent import KEYS_GUIDE, VOICE_HELP, cmd_test, voice_status

    check("voice: keys guide me LiveKit steps hain",
          "cloud.livekit.io" in KEYS_GUIDE and "LIVEKIT_API_SECRET" in KEYS_GUIDE)
    check("voice: help me console/dev/start modes hain",
          "voice console" in VOICE_HELP and "voice dev" in VOICE_HELP and "voice test" in VOICE_HELP)
    st = voice_status()
    check("voice: status structure (ready/keys/missing)",
          set(st) >= {"ready", "keys", "missing", "model", "voice"})
    code = cmd_test("aaj ki date batao")
    check("voice: bina keys pipeline test PASS", code == 0)


def t_telegram_wiring() -> None:
    """Telegram bot module-level wiring (library na ho to skip — fail nahi)."""
    import importlib.util

    src = (ROOT / "jarvis" / "telegram" / "bot.py").read_text()
    check("tg bot: live progress import wired", "LiveProgress" in src and "progress_enabled_for" in src)
    check("tg bot: crash par fail() call hota hai", "await live.fail(" in src)
    check("tg bot: inline buttons + /menu + /progress commands",
          "CallbackQueryHandler" in src and "cmd_menu" in src and "cmd_progress" in src)
    check("tg bot: final me errors pass hote hain", "errors=errors" in src)
    if importlib.util.find_spec("telegram") is None:
        check("tg bot: python-telegram-bot installed nahi (skip build_application)", True, "skip")
        return
    import jarvis.config.settings as _S

    _S.TELEGRAM_BOT_TOKEN = "123456789:" + "x" * 35  # dummy/fake token — koi network call nahi
    from jarvis.telegram.bot import build_application

    try:
        app = build_application()
        handlers = [h.__class__.__name__ for h in app.handlers[0]]
        check("tg bot: application build + handlers registered",
              any("CommandHandler" in h for h in handlers) and any("CallbackQueryHandler" in h for h in handlers),
              f"{len(handlers)} handlers")
    except Exception as exc:
        check("tg bot: application build", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
async def main() -> int:
    print("\n" + "=" * 66)
    print("  NIKHILAI — UI / progress / telegram-visibility suite")
    print("=" * 66)

    print("\n[1] progress event bus")
    await t_progress_core()
    print("\n[2] agent live progress")
    await t_agent_progress()
    print("\n[3] Telegram live progress + error visibility")
    await t_telegram_live()
    print("\n[4] markdown → Telegram HTML")
    t_md_to_html()
    print("\n[5] CLI pro dashboard")
    t_cli_pro_ui()
    print("\n[6] /run tool runner")
    await t_tool_runner()
    print("\n[7] tool schema shapes (LLM-callable)")
    t_tool_schema_shapes()
    print("\n[8] restart safety")
    await t_restart_safety()
    print("\n[9] exec approval security (risky commands)")
    await t_exec_approval_security()
    print("\n[9b] runtime settings surface")
    t_settings_surface()
    print("\n[10] voice console modes")
    t_voice_console()
    print("\n[11] telegram bot wiring")
    t_telegram_wiring()

    passed = sum(1 for _n, ok, _d in _results if ok)
    total = len(_results)
    print("\n" + "=" * 66)
    print(f"  RESULT: {passed}/{total} checks passed  ({'ALL GOOD 🚀' if passed == total else 'kuch fail hue ⚠️'})")
    print("=" * 66 + "\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
