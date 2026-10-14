"""
Diagnostics — `python -m jarvis doctor`
======================================

Sab kuch check karta hai: python, deps, env, provider, model, livekit, telegram, browser,
filesystem, memory, shell, desktop, search, workspace. Aur secrets kabhi print nahi karta —
sirf "set hai / nahi hai" batata hai.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

from ..config import settings as S
from ..config import runtime as R
from . import ui


def _dep(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _ver(name: str) -> str:
    try:
        import importlib.metadata as md

        return md.version(name)
    except Exception:
        return "?"


def collect_checks() -> list[tuple[str, str, str]]:
    """(status, check, detail) — status: ok | warn | fail"""
    checks: list[tuple[str, str, str]] = []

    # --- python / environment
    py = sys.version.split()[0]
    major, minor = sys.version_info[:2]
    checks.append(("ok" if (major, minor) >= (3, 10) else "fail",
                   "Python", f"{py} ({'supported' if (major, minor) >= (3, 10) else '3.10+ chahiye'})"))
    checks.append(("ok", "Platform", f"{sys.platform} • cwd={Path.cwd()}"))
    venv = os.environ.get("VIRTUAL_ENV") or ("yes" if sys.prefix != getattr(sys, "base_prefix", sys.prefix) else "no")
    checks.append(("ok" if venv != "no" else "warn", "Virtualenv",
                   f"{venv}" + ("" if venv != "no" else " — venv use karna better hai")))

    # --- core deps
    for label, mod, pkg in (("langchain", "langchain", "langchain"), ("langgraph", "langgraph", "langgraph"),
                            ("langchain-core", "langchain_core", "langchain-core"),
                            ("dspy", "dspy", "dspy"), ("rich", "rich", "rich"),
                            ("httpx", "httpx", "httpx"), ("ddgs", "ddgs", "ddgs")):
        checks.append(("ok" if _dep(mod) else "warn", label, _ver(pkg) if _dep(mod) else "missing (pip install)"))

    # --- providers
    from ..llm import health

    h = health()
    checks.append(("ok" if h["active"] != "demo" else "warn", "Brain provider",
                   f"{h['active']}" + ("" if h["active"] != "demo" else " — OFFLINE/DEMO (koi key nahi)")))
    checks.append(("ok" if h["roles"].get("brain") else "warn", "Brain model", h["roles"].get("brain", "?")))
    checks.append(("ok" if h["available"] else "warn", "Providers with keys",
                   ", ".join(h["available"]) or "koi nahi — GEMINI/OPENROUTER/GROQ me se koi ek daalo"))
    for role in ("fast", "code", "browser", "vision", "critic"):
        checks.append(("ok", f"  ↳ {role} model", h["roles"].get(role, "?")))

    # --- integrations
    bu_py = S.has_browser_use()
    bu_cli = S.has_browser_use_cli()
    checks.append(("ok" if (bu_py or bu_cli) else "warn", "browser-use",
                   f"python={'yes' if bu_py else 'no'} cli={'yes' if bu_cli else 'no'}"))
    if bu_cli:
        checks.append(("ok", "browser CLI path", shutil.which("browser-use") or "?"))
    chrom = next((p for p in ("chromium", "chromium-browser", "google-chrome", "chrome") if shutil.which(p)), "")
    checks.append(("ok" if (chrom or bu_py) else "warn", "Browser binary",
                   chrom or ("browser-use install karega" if bu_py else "koi browser nahi mila")))

    lk = S.has_livekit()
    lk_keys = all([S.LIVEKIT_URL, S.LIVEKIT_API_KEY, S.LIVEKIT_API_SECRET])
    checks.append(("ok" if (lk and lk_keys) else "warn", "LiveKit (voice)",
                   f"lib={'yes' if lk else 'no'} keys={'yes' if lk_keys else 'no'}"
                   + ("" if (lk and lk_keys) else " — pip install livekit-agents + cloud.livekit.io keys")))
    checks.append(("ok" if S.GEMINI_API_KEY else "warn", "Gemini Live (voice brain)",
                   "key set" if S.GEMINI_API_KEY else "GEMINI_API_KEY chahiye"))

    tg_lib = _dep("telegram")
    checks.append(("ok" if (S.TELEGRAM_BOT_TOKEN and tg_lib) else "warn", "Telegram bot",
                   f"token={'yes' if S.TELEGRAM_BOT_TOKEN else 'no'} lib={'yes' if tg_lib else 'no'}"))
    if S.TELEGRAM_BOT_TOKEN and not (S.TELEGRAM_OWNER_ID or S.TELEGRAM_ALLOWED_CHAT_IDS):
        checks.append(("warn", "Telegram auth",
                       "TELEGRAM_OWNER_ID set nahi — pehla user owner ban jayega!"))

    # --- filesystem / memory / shell / desktop
    try:
        probe = S.WORKSPACE_DIR / ".doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(("ok", "Filesystem (workspace writable)", str(S.WORKSPACE_DIR)))
    except Exception as exc:
        checks.append(("fail", "Filesystem", f"workspace likh nahi sakte: {exc}"))

    try:
        from ..memory.store import get_memory

        stats = get_memory().stats()
        checks.append(("ok", "Memory (SQLite + FTS)",
                       f"facts={stats.get('facts', 0)} episodes={stats.get('episodes', 0)} notes={stats.get('notes', 0)}"))
    except Exception as exc:
        checks.append(("fail", "Memory", str(exc)[:80]))

    try:
        from ..security.gate import effective_mode
        _gate_mode = effective_mode()
    except Exception:
        _gate_mode = S.EXEC_APPROVAL
    checks.append(("ok" if S.SHELL_ENABLED else "warn", "Shell executor",
                   f"enabled={S.SHELL_ENABLED} gate={_gate_mode} (JARVIS_EXEC_APPROVAL=off|risky|all)"))
    try:
        from ..runtime import process_manager as pm

        checks.append(("ok", "Process manager", f"{len(pm.list_processes())} background process"))
    except Exception as exc:
        checks.append(("warn", "Process manager", str(exc)[:70]))

    checks.append(("ok" if S.DESKTOP_ENABLED else "warn", "Desktop tools",
                   "enabled" if S.DESKTOP_ENABLED else "disabled"))
    try:
        from ..tools.app_finder import build_app_index

        apps = build_app_index()
        checks.append(("ok" if apps else "warn", "App index", f"{len(apps)} apps mile"))
    except Exception as exc:
        checks.append(("warn", "App index", str(exc)[:60]))

    try:
        from ..tools.file_readers import supported_types

        checks.append(("ok", "File readers", supported_types()[:80]))
    except Exception as exc:
        checks.append(("warn", "File readers", str(exc)[:60]))

    from ..tools import web_tools

    chain = getattr(web_tools, "SEARCH_STRATEGY", None)
    if chain:
        checks.append(("ok", "Search chain", " → ".join(chain)))
    else:
        checks.append(("ok" if S.DUCKDUCKGO_ENABLED else "warn", "Search",
                       "ddgs (keyless)" if S.DUCKDUCKGO_ENABLED else "koi search provider nahi"))

    try:
        from ..automation import scheduler as sch

        st = sch.status()
        checks.append(("ok", "Automations", f"{st['total']} saved, {st['enabled']} on, scheduler={'live' if st['running'] else 'off'}"))
    except Exception as exc:
        checks.append(("warn", "Automations", str(exc)[:60]))

    try:
        from ..skills.loader import catalog

        cat = catalog()
        checks.append(("ok", "Skills", f"{len(cat.splitlines())} skill(s) loaded"))
    except Exception as exc:
        checks.append(("warn", "Skills", str(exc)[:60]))

    # --- runtime settings
    snap = R.snapshot()
    checks.append(("ok", "Runtime settings",
                   f"owner={snap['owner_name']} mode={snap['mode']} personality={snap['personality']}"))
    checks.append(("ok", "Workspace settings file", str(S.RUNTIME_SETTINGS_FILE)))

    rep = S.config_report()
    if rep.missing_required:
        checks.append(("fail", "Config report", "; ".join(rep.missing_required)))
    elif rep.missing_optional:
        checks.append(("ok", "Config report", f"{len(rep.ready)} ready, {len(rep.missing_optional)} optional missing"))
    return checks


def run_doctor(json_out: bool = False, fix: bool = False) -> int:
    checks = collect_checks()
    if json_out:
        import json

        print(json.dumps([{"status": s, "check": c, "detail": d} for s, c, d in checks], indent=2))
    else:
        ui.cprint()
        ui.banner(S.ASSISTANT_NAME, S.OWNER_NAME)
        ui.cprint("[bold]Doctor — poora system check[/]")
        ui.doctor_table(checks)

    count = {"ok": 0, "warn": 0, "fail": 0}
    for status, _, _ in checks:
        count[status] = count.get(status, 0) + 1

    if not json_out:
        summary = (f"[{ui.OK}]✅ {count['ok']} ok[/]  "
                   f"[{ui.WARN}]🟡 {count['warn']} warnings[/]  "
                   f"[{ui.BAD}]🔴 {count['fail']} failed[/]")
        ui.cprint(summary)
        if count["fail"]:
            ui.cprint("\n[bold]Fix karo:[/] failing checks dekh ke .env update karo, phir "
                      "[bold]python -m jarvis doctor[/] dobara chalao.", ui.WARN)
        elif count["warn"]:
            ui.cprint("\nSystem chalne layak hai — warnings optional features ki hain "
                      "(voice/telegram/browser jaise).", ui.OK)
        else:
            ui.cprint("\nSab kuch ready hai 🚀  Chalao: [bold]python -m jarvis[/]", ui.OK)

    if fix:
        try:
            from ..tools.system_ops import clear_cache

            res = clear_cache(scope="all")
            ui.cprint("♻️  " + res["message"], ui.ACCENT)
        except Exception as exc:
            ui.cprint(f"auto-fix skip: {exc}", ui.WARN)

    return 0 if count["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run_doctor())
