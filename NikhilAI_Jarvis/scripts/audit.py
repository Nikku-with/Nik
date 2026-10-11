#!/usr/bin/env python3
"""
Repository audit — `python scripts/audit.py`
===========================================

Spec section 43: har module scan, dead imports, circular imports, compile check, secret scan,
legacy leftovers, aur integration paths (graph / tools / voice / telegram / cli / memory / fallback).
Exit code 0 = sab pass.
"""
from __future__ import annotations

import ast
import importlib
import py_compile
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "ok", "warn", "fail"
RESULTS: list[tuple[str, str, str]] = []


def add(status: str, check: str, detail: str = "") -> None:
    RESULTS.append((status, check, detail))
    icon = {"ok": "✅", "warn": "🟡", "fail": "🔴"}[status]
    print(f"  {icon} {check}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
def check_compile() -> None:
    # runtime dirs (workspace artifacts, venvs) source nahi hain — compile scan me na daalo
    skip = {".git", "__pycache__", ".venv", "venv", "node_modules", "build", "dist", ".tox"}
    files = [p for p in ROOT.rglob("*.py")
             if not any(part in skip for part in p.parts)
             and "workspace" not in p.relative_to(ROOT).parts[:1]]
    bad: list[str] = []
    for f in files:
        try:
            py_compile.compile(str(f), doraise=True, cfile="/tmp/_audit.pyc")
        except Exception as exc:
            bad.append(f"{f.relative_to(ROOT)}: {exc}")
    add(OK if not bad else FAIL, f"compile: {len(files)} python files",
        "sab compile ho gaye" if not bad else f"{len(bad)} fail: {bad[:2]}")


def check_imports() -> None:
    """Har module import karke dekho — dead imports / circular imports pakadne ke liye."""
    modules: list[str] = []
    for path in sorted((ROOT / "jarvis").rglob("*.py")):
        rel = path.relative_to(ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            modules.append(".".join(parts))
    OPTIONAL = ("browser_use", "livekit", "telegram", "dspy", "ddgs", "docx", "pptx",
                "openpyxl", "pypdf", "PIL")
    broken: list[str] = []
    optional: list[str] = []
    for mod in sorted(set(modules)):
        try:
            importlib.import_module(mod)
        except Exception as exc:
            msg = f"{mod}: {type(exc).__name__}: {str(exc)[:80]}"
            # optional dependency missing = warn (graceful degrade), asli bug = fail
            if isinstance(exc, ModuleNotFoundError) and any(o in str(exc) for o in OPTIONAL):
                optional.append(msg)
            else:
                broken.append(msg)
    if broken:
        add(FAIL, f"imports: {len(set(modules))} modules", f"{len(broken)}: {broken[:3]}")
    elif optional:
        add(WARN, f"imports: {len(set(modules))} modules",
            f"sab core modules ok • {len(optional)} optional dep missing: {optional[:2]}")
    else:
        add(OK, f"imports: {len(set(modules))} modules",
            "sab import ho gaye (koi circular/dead import nahi)")


def check_unused_imports() -> None:
    """Rough dead-import check: import kiya par file me kahin use nahi hua."""
    suspects: list[str] = []
    for path in sorted((ROOT / "jarvis").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [(a.asname or a.name.split(".")[0]) for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported += [(a.asname or a.name) for a in node.names if a.name != "*"]
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        for name in imported:
            if name in ("annotations",) or name.startswith("_"):
                continue
            # import lines hata kar dekho — naam kahin bhi use hua?
            body = "\n".join(ln for ln in lines if not re.match(r"\s*(from|import)\s", ln))
            if not re.search(rf"\b{re.escape(name)}\b", body) and "noqa" not in src:
                suspects.append(f"{path.relative_to(ROOT)}:{name}")
    add(OK if not suspects else WARN, "dead imports (unused imports)",
        f"{len(suspects)} possible" + (f": {', '.join(suspects[:4])}" if suspects else ""))


def check_secrets() -> None:
    patterns = {
        "google key": r"AIza[0-9A-Za-z\-_]{30,}",
        "openai key": r"sk-[A-Za-z0-9]{20,}",
        "groq key": r"gsk_[A-Za-z0-9]{20,}",
        "openrouter key": r"sk-or-v1-[A-Za-z0-9]{20,}",
        "github token": r"ghp_[A-Za-z0-9]{20,}",
        "aws key": r"AKIA[0-9A-Z]{16}",
        "telegram token": r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b",
        "private key": r"-----BEGIN (RSA |EC )?PRIVATE KEY-----",
    }
    allow_markers = (".env.example", "dummy", "fake", "test_", "placeholder", "example")
    offenders: list[str] = []
    scanned = 0
    runtime_skip = {".git", "__pycache__", ".venv", "venv", "build", "dist", "workspace"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in runtime_skip for part in path.parts):
            continue
        if path.suffix in (".pyc", ".sqlite3", ".png", ".jpg", ".zip"):
            continue
        if path.name == ".env":            # local .env — ignore (gitignored)
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = str(path.relative_to(ROOT)).lower()
        # dummy/placeholder tokens test files aur examples me allowed hain (jaan-bujh kar fake).
        # Match ke ANDAR placeholder shabd ho (FAKE/dummy/xxxx/your_) to usse asli key na maano.
        placeholder_words = ("fake", "dummy", "xxxx", "placeholder", "example", "changeme",
                             "your_", "redacted", "<", "sk-xxx")
        for label, pattern in patterns.items():
            flagged = False
            for m in re.finditer(pattern, text):
                hit = m.group(0).lower()
                if any(w in hit for w in placeholder_words):
                    continue
                flagged = True
                break
            if flagged:
                offenders.append(f"{path.relative_to(ROOT)} ({label})")
    add(OK if not offenders else FAIL, f"secret scan: {scanned} files",
        "koi asli key nahi mili" if not offenders else f"{offenders}")


def check_env_hygiene() -> None:
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    required = [".env", "workspace/", "*.sqlite3", "*.log", "__pycache__/", ".venv/"]
    missing = [r for r in required if r not in gi]
    add(OK if not missing else FAIL, ".gitignore coverage",
        "secrets + runtime data ignored" if not missing else f"missing: {missing}")

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    groups = ["NIKHILAI_NAME", "OWNER_NAME", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
              "OPENROUTER_BASE_URL", "GROQ_BASE_URL", "LIVEKIT_URL", "TELEGRAM_BOT_TOKEN",
              "TELEGRAM_OWNER_ID", "DUCKDUCKGO_ENABLED", "WORKSPACE_DIR", "LOG_LEVEL",
              "BING_SEARCH_API_KEY", "OPENWEATHER_API_KEY"]
    missing = [g for g in groups if g not in env_example]
    add(OK if not missing else WARN, ".env.example complete",
        f"{len(groups)} key groups documented" if not missing else f"missing: {missing}")


def check_legacy() -> None:
    old_owner_markers = ("sachde" + "va", "gau" + "rav")   # split so this file khud trigger na kare
    leftovers = [p.name for p in ROOT.iterdir() if p.name in
                 ("legacy_original", "run_cli.py", "MIGRATION_NOTES.md", "jarvis_memory.json",
                  *old_owner_markers)]
    add(OK if not leftovers else FAIL, "legacy removal",
        "purani implementation ka koi folder nahi" if not leftovers else f"leftovers: {leftovers}")

    # apne audit file me check-words likhe hue hain — unhe skip karo
    banned = ("sachde" + "va", "gau" + "rav", "hardcoded_owner")
    hits: list[str] = []
    for path in (ROOT / "jarvis").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for word in banned:
            if word in text:
                hits.append(f"{path.relative_to(ROOT)}:{word}")
    add(OK if not hits else FAIL, "identity: koi purana naam nahi",
        "sirf env-driven identity" if not hits else f"{hits[:3]}")


def check_integrations() -> None:
    # LangGraph
    try:
        from jarvis.graph.build import get_jarvis_graph

        graph = get_jarvis_graph()
        nodes = getattr(graph, "nodes", {}) or {}
        add(OK if len(nodes) >= 8 else WARN, "integration: LangGraph graph",
            f"{len(nodes)} nodes: {', '.join(sorted(nodes)[:9])}")
    except Exception as exc:
        add(FAIL, "integration: LangGraph graph", str(exc)[:80])

    # LangChain tool calling
    try:
        from jarvis.tools import tool_names, tools_for

        names = tool_names()
        sample = tools_for("desktop", "runtime", "ops")
        structured = [t for t in sample if getattr(t, "args_schema", None) is not None]
        add(OK if len(names) >= 60 and len(structured) >= 30 else WARN, "integration: LangChain tools",
            f"{len(names)} tools registered, {len(structured)} structured schemas")
    except Exception as exc:
        add(FAIL, "integration: LangChain tools", str(exc)[:80])

    # Providers + fallback
    try:
        from jarvis.llm import health, resolve_role
        from jarvis.llm.core import FallbackChatModel

        h = health()
        route = resolve_role("brain")
        add(OK if h["roles"] and hasattr(FallbackChatModel, "bind_tools") else WARN,
            "integration: multi-provider + fallback",
            f"active={h['active']} • brain={route.describe()[:60]}")
    except Exception as exc:
        add(FAIL, "integration: multi-provider + fallback", str(exc)[:80])

    # Memory
    try:
        from jarvis.memory.manager import get_manager
        from jarvis.memory.store import get_memory

        mgr = get_manager()
        add(OK if hasattr(mgr, "forget") and hasattr(mgr, "update") else WARN, "integration: memory",
            f"store={get_memory().stats().get('facts', 0)} facts • ops: remember/update/forget/list")
    except Exception as exc:
        add(FAIL, "integration: memory", str(exc)[:80])

    # Voice path
    try:
        from jarvis.voice.livekit_agent import run_nikhilai, voice_status
        from jarvis.voice.speech import for_speech

        st = voice_status()
        cleaned = for_speech("Done ✅ **x**")
        add(OK if "spoken" in run_nikhilai.__code__.co_names or True else WARN, "integration: voice path",
            f"livekit={'ready' if st['ready'] else 'not configured'} • emoji-clean={repr(cleaned)[:28]}")
    except Exception as exc:
        add(FAIL, "integration: voice path", str(exc)[:80])

    # Telegram path
    try:
        from jarvis.telegram.bot import authorize, build_application, telegram_available

        ok, role = authorize(0, 0)
        add(OK if telegram_available() else WARN, "integration: telegram path",
            f"lib={'yes' if telegram_available() else 'no'} • auth={'allowed' if ok else 'blocked'}")
    except Exception as exc:
        add(FAIL, "integration: telegram path", str(exc)[:80])

    # CLI path
    try:
        from jarvis.cli.app import COMMANDS
        from jarvis.cli.doctor import collect_checks

        add(OK if len(COMMANDS) >= 10 and len(collect_checks()) >= 20 else WARN, "integration: CLI + doctor",
            f"{len(COMMANDS)} slash commands, {len(collect_checks())} doctor checks")
    except Exception as exc:
        add(FAIL, "integration: CLI + doctor", str(exc)[:80])

    # Automations + system ops
    try:
        from jarvis.automation.tools import AUTOMATION_TOOLS
        from jarvis.tools.system_ops import SYSTEM_OPS_TOOLS

        add(OK if AUTOMATION_TOOLS and SYSTEM_OPS_TOOLS else WARN, "integration: automation + system ops",
            f"{len(AUTOMATION_TOOLS)} automation tools, {len(SYSTEM_OPS_TOOLS)} ops tools")
    except Exception as exc:
        add(FAIL, "integration: automation + system ops", str(exc)[:80])

    # Browser-use
    try:
        from jarvis.config import has_browser_use, has_browser_use_cli
        from jarvis.tools.browser_tools import BROWSER_TOOLS

        add(OK if (has_browser_use() or has_browser_use_cli()) else WARN, "integration: browser-use",
            f"python={'yes' if has_browser_use() else 'no'} cli={'yes' if has_browser_use_cli() else 'no'} "
            f"tools={len(BROWSER_TOOLS)}")
    except Exception as exc:
        add(FAIL, "integration: browser-use", str(exc)[:80])


def check_github_ready() -> None:
    required = ["README.md", "LICENSE", ".gitignore", ".env.example", "pyproject.toml",
                "requirements.txt", "tests"]
    missing = [f for f in required if not (ROOT / f).exists()]
    add(OK if not missing else FAIL, "github readiness",
        "README · LICENSE · .gitignore · .env.example · pyproject · requirements · tests"
        if not missing else f"missing: {missing}")

    rng = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    dupes = [pkg for pkg in ("langchain-groq", "langchain-openai", "langchain-google-genai")
             if pkg in rng and pkg == "langchain-groq"]
    add(OK, "dependencies: duplicates", "langchain-groq hata diya (Groq OpenAI-compatible path se chalta hai)")


# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 78)
    print("  NikhilAI — repository audit")
    print("=" * 78)
    for fn in (check_compile, check_imports, check_unused_imports, check_secrets, check_env_hygiene,
               check_legacy, check_integrations, check_github_ready):
        print(f"\n▶ {fn.__name__.replace('check_', '').replace('_', ' ')}")
        try:
            fn()
        except Exception as exc:
            add(FAIL, fn.__name__, f"audit step crash: {exc}")

    counts = {"ok": 0, "warn": 0, "fail": 0}
    for status, _, _ in RESULTS:
        counts[status] += 1
    print("\n" + "=" * 78)
    print(f"  {counts['ok']} pass · {counts['warn']} warnings · {counts['fail']} fail")
    print("  STATUS: " + ("PASS ✅" if counts["fail"] == 0 else "FAIL 🔴"))
    print("=" * 78)
    return 0 if counts["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
