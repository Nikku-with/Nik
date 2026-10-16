#!/usr/bin/env python3
"""
scripts/audit_tools.py — "saare tools call ho payenge na?" ka asli jawab
=========================================================================

Har registered tool ke liye check karta hai:
  1. naam + description + args schema theek hai (LLM ko dikhne layak)
  2. ek *safe* synthetic call se actually invoke hota hai (crash nahi karta)
  3. error aaye to woh **tool ka kaam ka message** ho (traceback nahi)

Dangerous/system tools ke liye live call nahi karte — sirf schema + bound-check
(unhe chhedna system ko nuksaan pahuncha sakta hai). Web search/fetch/weather smoke calls
local fakes use karte hain; audit kabhi kisi public provider ko hit nahi karta. Include karne ke liye:
    python scripts/audit_tools.py --live-dangerous

Exit code 0 = sab call ho paye, 1 = kuch tool toota hua hai.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "✅", "🟡", "🔴"

# Tool → safe synthetic args (galat args par bhi tool ko crash nahi karna chahiye)
SAFE_ARGS: dict[str, dict[str, Any]] = {
    "open_app": {"app_title": "__nikhilai_nonexistent_app__"},
    "open_any_app": {"query": "__nikhilai_nonexistent_app__"},
    "close_app": {"window_title": "__nikhilai_nonexistent_window__"},
    "list_windows": {},
    "type_text": {"text": ""},
    "press_hotkey": {"keys": ""},
    "control_volume": {"action": "status"},
    "take_screenshot": {"name": "audit_smoke"},
    "system_info": {},
    "process_list": {},
    "process_logs": {"pid": "999999"},
    "find_files": {"name": "__nikhilai_nonexistent__"},
    "search_inside_files": {"query": "nikhilai-audit-needle"},
    "recent_files": {},
    "list_dir": {"path": "/tmp"},
    "list_folders": {},
    "file_stats": {"path": "/tmp"},
    "read_any_file": {"path": "/tmp/__nikhilai_nonexistent__.txt"},
    "read_file": {"path": "/tmp/__nikhilai_nonexistent__.txt"},
    "read_document": {"path": "/tmp/__nikhilai_nonexistent__.txt"},
    "list_notes": {},
    "read_note": {"name": "__nikhilai_nonexistent__"},
    "search_notes": {"query": "nikhilai"},
    "find_apps_tool": {"query": "notepad"},
    "run_python": {"code": "print(1+1)"},
    "python_repl": {"code": "1+1"},
    "sandbox_run": {"code": "print('ok')"},
    "run_python_isolated": {"code": "print('ok')", "timeout": 30},
    "repl_reset": {},
    "get_current_datetime": {},
    "browser_status": {},
    "memory_stats": {},
    "list_memories": {},
    "recall_facts": {"query": "nikhilai"},
    "conversation_history": {},
    "memory_compact": {},
    "full_system_status": {},
    "system_health": {},
    "list_automations": {},
    "list_skills": {},
    "read_skill": {"name": "gst_invoice"},
    "session_state": {},
    "list_code_scripts": {},
    "eval_math": {"expression": "2+2"},
    "clipboard_get": {},
    "log_search": {"query": "x"},
    "get_weather": {"city": "Kota"},
    "web_search": {"query": "python", "num": 1},
    "google_search": {"query": "python", "num": 1},
    "fetch_page": {"url": "https://example.com"},
}

# Live call se bachne wale: writes, execution, approvals, settings, ya active UI/state changes.
# Audit safe rehna chahiye; in tools ko schema-validate karo, invoke mat karo.
NO_LIVE = {
    "append_note", "browser_cli", "browser_close", "browser_research", "browser_screenshot", "browser_task",
    "browser_use_cli", "cancel_automation", "clear_cache_tool", "clear_process_tool",
    "clipboard_set", "close_app", "code_engine", "control_volume", "create_automation", "create_project",
    "deep_research", "delete_note", "folder_file", "forget_memory", "fix_code",
    "generate_code", "install_package", "memory_compact",
    "notification_send", "open_any_app", "open_app", "open_path", "play_file",
    "press_hotkey", "process_input", "process_stop", "python_repl", "refresh_app_index",
    "refresh_system_tool", "reload_config_tool", "remember_fact", "repl_reset", "run_python",
    "run_python_isolated", "sandbox_run",
    "restart_jarvis_tool", "run_automation", "run_automation_now", "run_code_script",
    "run_script_file", "run_shell_command", "save_note", "send_file_telegram",
    "undo_file_operation",
    "set_pending_steps", "set_preference", "set_session_note", "set_workspace",
    "session_note", "start_background_process", "take_screenshot", "toggle_automation",
    "type_text", "update_memory", "write_code_script", "write_file", "zip_create",
    "practice_tool", "tg_send_buttons",
    # Desktop tools are never invoked by the audit: even read-only observation may reveal
    # private screen/clipboard content, and input tools would affect a visible session.
    "desktop_click", "desktop_clipboard_read", "desktop_clipboard_write", "desktop_focus",
    "desktop_move", "desktop_observe", "desktop_ocr", "desktop_press", "desktop_scroll",
    "desktop_type", "desktop_windows",
}


def build_args(tool) -> dict[str, Any]:
    """Tool ke schema se safe args banao (jo SAFE_ARGS me na ho)."""
    name = getattr(tool, "name", "")
    if name in SAFE_ARGS:
        return dict(SAFE_ARGS[name])
    schema = getattr(tool, "args_schema", None)
    fields = getattr(schema, "model_fields", None) or {}
    args: dict[str, Any] = {}
    for fname, finfo in fields.items():
        ann = str(getattr(finfo, "annotation", "str")).lower()
        if "int" in ann:
            args[fname] = 1
        elif "float" in ann:
            args[fname] = 1.0
        elif "bool" in ann:
            args[fname] = False
        elif "list" in ann:
            args[fname] = []
        elif "dict" in ann:
            args[fname] = {}
        else:
            args[fname] = "audit"
    return args


def check_schema_shape(tool) -> str:
    """Schema me varargs/dodgy field naam ho to LLM tool ko call nahi kar payega.

    (Real bug: parameter ka naam `args` hone se LangChain usse `*args` samajh leta hai
    aur property `v__args` ban jaati hai — phir tool har call par TypeError deta hai.)
    """
    schema = getattr(tool, "args_schema", None)
    if not schema:
        return ""
    try:
        props = schema.model_json_schema().get("properties", {})
    except Exception as exc:  # noqa: BLE001
        return f"schema render fail: {exc}"
    weird = [p for p in props if p.startswith("v__") or p in ("kwargs", "self")]
    if weird:
        return (f"BROKEN schema: {weird} — parameter ka naam 'args'/'*args' ho to rename karo "
                f"(suggested: script_args)")
    return ""


def validate_schema(tool, args: dict[str, Any]) -> str:
    """args_schema se args validate karo — call kiye bina."""
    schema = getattr(tool, "args_schema", None)
    if not schema:
        return ""
    try:
        if hasattr(schema, "model_validate"):
            schema.model_validate(args)
        else:
            schema(**args)
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:80]}"


async def _invoke_with_network_fakes(tool, args: dict[str, Any]):
    """Invoke read-only web tools through deterministic fakes, never public services."""
    name = getattr(tool, "name", "")
    if name not in {"google_search", "web_search", "fetch_page", "get_weather"}:
        return await tool.ainvoke(args)

    from unittest.mock import patch

    from jarvis.tools import web_tools

    async def fake_search(query: str, num: int = 5):
        return [{"title": "Mock audit source", "url": "https://example.invalid/audit",
                 "snippet": f"Mock search for {query[:80]}"}][:num]

    async def fake_fetch(_url: str, max_chars: int = 12000, timeout: float = 25.0):
        return "Mock audit page content; no network was used."[:max_chars]

    def fake_weather(city: str = ""):
        return f"Mock audit weather for {city or 'Kota'}."

    with patch.object(web_tools, "web_search_results", fake_search), \
            patch.object(web_tools, "fetch_url_text", fake_fetch), \
            patch.object(web_tools, "_weather_sync", fake_weather):
        return await tool.ainvoke(args)


async def check_tool(tool, live_dangerous: bool) -> tuple[str, str]:
    """Ek tool ko check karo. Returns (status, detail)."""
    name = getattr(tool, "name", "?")
    desc = (getattr(tool, "description", "") or "").strip()

    if not name or name == "?":
        return FAIL, "naam nahi hai"
    if len(desc) < 12:
        return FAIL, f"description bahut chhoti ({len(desc)} chars)"
    if not hasattr(tool, "ainvoke"):
        return FAIL, "ainvoke method nahi hai"
    shape_err = check_schema_shape(tool)
    if shape_err:
        return FAIL, shape_err

    args = build_args(tool)
    schema_err = validate_schema(tool, args)

    if name in NO_LIVE and not live_dangerous:
        if schema_err:
            return WARN, f"schema guess galat (audit args): {schema_err}"
        return OK, f"schema ok • call skipped (side-effect) • args={sorted(args) or '-'}"

    t0 = time.time()
    try:
        out = await asyncio.wait_for(_invoke_with_network_fakes(tool, args), timeout=45)
    except asyncio.TimeoutError:
        return WARN, "45s me jawab nahi aaya (network/slow)"
    except TypeError as exc:
        return FAIL, f"args mismatch: {str(exc)[:110]}"
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"{type(exc).__name__}: {str(exc)[:110]}"

    ms = (time.time() - t0) * 1000
    text = str(out)
    if "Traceback (most recent call last)" in text:
        return FAIL, f"traceback leak ({ms:.0f}ms): {text[:110]}"
    if not text.strip():
        return WARN, f"khali output ({ms:.0f}ms)"
    # Tool output can contain user memory, file contents, or private paths; keep the
    # audit evidence content-free so reports are safe to commit and share.
    return OK, f"{ms:.0f}ms • invocation succeeded ({len(text)} output chars; content redacted)"


def _write_report(path: str, results: list[dict[str, str]], counts: dict[str, int]) -> None:
    """Markdown proof: har tool ka status (kis group me, kaise verify hua)."""
    import datetime
    import pathlib as _pl

    total = len(results)
    verdict = "PASS ✅" if counts["fail"] == 0 else "FAIL 🔴"
    lines = [
        "# Tool Callability Audit — NIKHILAI",
        "",
        f"**Date:** {datetime.date.today().isoformat()}  ·  **Tools:** {total}  ·  "
        f"**Verdict:** {verdict}",
        "",
        f"- ✅ callable: **{counts['ok']}**",
        f"- 🟡 warn: **{counts['warn']}**",
        f"- 🔴 broken: **{counts['fail']}**",
        "",
        "Har tool ko LLM ki tarah invoke kiya gaya (`ainvoke` + schema-validated args). "
        "Search/fetch/weather calls deterministic fakes use karte hain; koi public endpoint hit nahi hota. "
        "Side-effect wale tools schema-check hote hain (asli call `--live-dangerous` se).",
        "",
        "| # | group | tool | status | evidence |",
        "|---|-------|------|--------|----------|",
    ]
    for i, r in enumerate(sorted(results, key=lambda x: (x["group"], x["tool"])), 1):
        detail = (r["detail"] or "").replace("|", "\\|").replace("\n", " ")[:110]
        lines.append(f"| {i} | {r['group']} | `{r['tool']}` | {r['status']} | {detail} |")
    lines += ["", "Re-run the audit with `python scripts/audit_tools.py`; use `--report <path>` to save a detailed table.", ""]
    _pl.Path(path).write_text("\n".join(lines), encoding="utf-8")


def _install_network_guard() -> tuple[list[str], Any]:
    """Block accidental DNS/socket access during this smoke audit."""
    attempts: list[str] = []
    originals: list[tuple[object, str, object]] = []

    def denied(*_args, **_kwargs):
        attempts.append("socket/DNS")
        raise OSError("live network is disabled during the tool audit")

    for owner, name in (
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket.socket, "sendto"),
    ):
        if hasattr(owner, name):
            originals.append((owner, name, getattr(owner, name)))
            setattr(owner, name, denied)

    def restore() -> None:
        for owner, name, value in reversed(originals):
            setattr(owner, name, value)

    return attempts, restore


async def main() -> int:
    ap = argparse.ArgumentParser(description="Har tool call ho sakta hai ya nahi — live check")
    ap.add_argument("--live-dangerous", action="store_true",
                    help="side-effect wale tools ko bhi actually call karo (apne risk par)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--report", default="", metavar="FILE.md",
                    help="markdown report likho kisi chosen path par — proof ke liye")
    opts = ap.parse_args()

    from jarvis.tools import _TOOL_GROUPS as TOOL_GROUPS
    from jarvis.tools import ALL_TOOLS

    group_of = {}
    for gname, items in TOOL_GROUPS.items():
        for it in items:
            group_of.setdefault(getattr(it, "name", ""), gname)

    print("=" * 78)
    print(f"  TOOL CALLABILITY AUDIT — {len(ALL_TOOLS)} tools")
    print("=" * 78)

    results: list[dict[str, str]] = []
    network_attempts, restore_network = _install_network_guard()
    try:
        for tool in sorted(ALL_TOOLS, key=lambda t: getattr(t, "name", "")):
            status, detail = await check_tool(tool, opts.live_dangerous)
            results.append({"group": group_of.get(getattr(tool, "name", ""), "?"),
                            "tool": getattr(tool, "name", "?"), "status": status, "detail": detail})
    finally:
        restore_network()

    if network_attempts:
        print(f"\n🔴 Network isolation failed: {len(network_attempts)} socket/DNS attempt(s) blocked.")
        print("   Fix the smoke test to use mocked search/fetch/weather/browser backends.")
        return 1

    counts_pre = {"ok": sum(1 for r in results if r["status"] == OK),
                  "warn": sum(1 for r in results if r["status"] == WARN),
                  "fail": sum(1 for r in results if r["status"] == FAIL)}
    if opts.report:
        _write_report(opts.report, results, counts_pre)
        print(f"📄 report likh di: {opts.report}")

    if opts.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        cur = ""
        for r in results:
            if r["group"] != cur:
                cur = r["group"]
                print(f"\n▶ {cur}")
            print(f"  {r['status']} {r['tool']:26} {r['detail']}")

    counts = {OK: 0, WARN: 0, FAIL: 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n" + "=" * 78)
    print(f"  {counts[OK]} callable · {counts[WARN]} warn · {counts[FAIL]} broken   (total {len(results)})")
    if counts[FAIL]:
        print("\n  🔴 Broken (LLM call karega to fail hoga):")
        for r in results:
            if r["status"] == FAIL:
                print(f"     - {r['tool']}: {r['detail']}")
    print(f"  STATUS: {'PASS ✅' if not counts[FAIL] else 'FAIL 🔴'}")
    print("=" * 78)
    return 0 if not counts[FAIL] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
