"""
NikhilAI entry point
====================

    python -m jarvis              → interactive menu (Text / Voice / Telegram / Settings / Doctor)
    python -m jarvis cli          → text/CLI mode
    python -m jarvis voice        → realtime voice mode (LiveKit + Gemini Live)
    python -m jarvis console      → seedha LiveKit console (alias of `voice console`)
    python -m jarvis mic          → wahi console mode (alias)
    python -m jarvis voice keys   → voice ke liye exact keys + steps (ya `python -m jarvis keys`)
    python -m jarvis voice test   → keys ke bina voice pipeline test
    python -m jarvis telegram     → Telegram bot (aliases: tg, bot)
    python -m jarvis doctor       → diagnostics (alias: diag)
    python -m jarvis status       → ek nazar me status
    python -m jarvis test         → offline test suite
    python -m jarvis settings     → settings dekho/badlo
    python -m jarvis serve        → HTTP API + web console
    python -m jarvis "<request>"  → ek hi request chala kar exit

Koi bhi baaki text = AI ke liye request (prefix ya keyword ki zaroorat nahi):
    python -m jarvis hy                 → "hy" seedha AI ko jaata hai
    python -m jarvis aaj ka mausam batao
Close typo bhi chal jata hai ("consol" → console), aur galat samajhne par bataya jata hai.
"""

from __future__ import annotations

import os
import sys

USAGE = __doc__


def _print_usage() -> None:
    print(USAGE)


def cmd_status() -> int:
    from .cli import ui
    from .runtime.status import agent_status

    st = agent_status(compact=True)
    ui.status_panel(
        {
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
    )
    return 0


def cmd_test(passthrough: list[str]) -> int:
    """Saari test files chalao (offline suite + v2.3 subsystem suite)."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    tests = sorted((root / "tests").glob("test_*.py"))
    if not tests:
        print(f"❌ koi test file nahi mili: {root / 'tests'}")
        return 1
    codes: list[int] = []
    for test_file in tests:
        print(f"\n{'=' * 70}\n▶ {test_file.name}\n{'=' * 70}")
        codes.append(subprocess.call([sys.executable, str(test_file), *passthrough]))
    ok = all(code == 0 for code in codes)
    print(
        f"\n{'=' * 70}\n  {'✅ SAARE TESTS PASS' if ok else '⚠️ kuch tests fail hue'}"
        f"  ({len(tests)} file(s))\n{'=' * 70}"
    )
    return 0 if ok else 1


def cmd_settings(args: list[str]) -> int:
    from .cli import ui
    from .config import runtime as R

    if not args:
        ui.settings_table(R.describe())
        return 0
    if args[0].lower() == "reset":
        R.reset(args[1] if len(args) > 1 else None)
        ui.cprint("♻️ reset ho gaya", ui.OK)
        return 0
    if len(args) < 2:
        print("Usage: python -m jarvis settings <key> <value>")
        return 1
    key, value = args[0].lower(), " ".join(args[1:])
    applied, errors = R.set_many(**{key: value})
    if errors:
        print("⚠️ " + "; ".join(errors))
        return 1
    print(f"✅ {key} = {applied.get(key)}")
    return 0


def cmd_voice(args: list[str]) -> int:
    from .voice.livekit_agent import main as voice_main

    return voice_main(args)


def cmd_telegram(args: list[str]) -> int:
    from .telegram.bot import run_bot

    return run_bot()


def cmd_serve(args: list[str]) -> int:
    import uvicorn

    from .config import settings as S

    host = args[0] if args else S.API_HOST
    port = int(args[1]) if len(args) > 1 else S.API_PORT
    from .automation import scheduler as sch

    sch.ensure_scheduler()  # web se bani automations bhi fire hon
    print(f"🌐 {S.ASSISTANT_NAME} API + web console → http://{host}:{port} (⏰ scheduler on)")
    uvicorn.run("jarvis.api.server:app", host=host, port=port, log_level=S.LOG_LEVEL.lower())
    return 0


def cmd_menu() -> int:
    """Bina args → chhota menu (pehli baar wale ke liye)."""
    from .cli import ui
    from .config import settings as S

    ui.banner(S.ASSISTANT_NAME, S.OWNER_NAME)
    ui.cprint(S.config_report().as_text())
    ui.cprint()
    options = [
        ("1", "Text / CLI mode", lambda: __import__("jarvis.cli.app", fromlist=["main"]).main([])),
        ("2", "Voice mode — LiveKit console (mic se baat karo)", lambda: cmd_voice(["console"])),
        ("3", "Telegram bot", lambda: cmd_telegram([])),
        ("4", "Settings", lambda: cmd_settings([])),
        ("5", "Doctor (diagnostics)", lambda: __import__("jarvis.cli.doctor", fromlist=["run_doctor"]).run_doctor()),
        ("6", "Status", lambda: cmd_status()),
        ("7", "Run tests", lambda: cmd_test([])),
    ]
    for key, label, _ in options:
        ui.cprint(f"  [bold {ui.ACCENT}]{key}[/]  {label}")
    ui.cprint()
    try:
        choice = input("Choose (1-7, enter = 1): ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        return 0
    for key, _, fn in options:
        if key == choice:
            return int(fn() or 0)
    ui.cprint("❓ galat choice", ui.WARN)
    return 1


def main(argv: list[str] | None = None) -> int:
    # self-restart ko isi process tak seemit rakho (library/scripts se exec na ho)
    os.environ.setdefault("NIKHILAI_MAIN_PROCESS", "1")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return cmd_menu()

    from .cli.routing import resolve_command

    res = resolve_command(argv)
    if res.note:
        print(f"ℹ️  {res.note}")

    if res.kind == "help":
        _print_usage()
        return 0
    if res.kind == "menu":
        return cmd_menu()
    if res.kind == "cli":
        from .cli.app import main as cli_main

        return cli_main(res.args)
    if res.kind == "voice":
        return cmd_voice(res.args)
    if res.kind == "telegram":
        return cmd_telegram(res.args)
    if res.kind == "doctor":
        from .cli.doctor import run_doctor

        return run_doctor(json_out="--json" in res.args, fix="--fix" in res.args)
    if res.kind == "status":
        return cmd_status()
    if res.kind == "test":
        return cmd_test(res.args)
    if res.kind == "settings":
        return cmd_settings(res.args)
    if res.kind == "serve":
        return cmd_serve(res.args)
    if res.kind == "version":
        from .config import settings as S

        print(f"{S.ASSISTANT_NAME} v2.3 • provider={S.PROVIDER} • workspace={S.WORKSPACE_DIR}")
        return 0

    # koi command nahi → poori line ek AI request hai (jaise "hy", "aaj ka mausam batao")
    from .cli.app import main as cli_main

    return cli_main(res.args or argv)


if __name__ == "__main__":
    raise SystemExit(main())
