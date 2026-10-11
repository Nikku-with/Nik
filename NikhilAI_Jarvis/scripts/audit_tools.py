#!/usr/bin/env python3
"""
scripts/audit_tools.py — "saare tools call ho payenge na?" ka asli jawab
=========================================================================

Har registered tool ke liye check karta hai:
  1. naam + description + args schema theek hai (LLM ko dikhne layak)
  2. ek *safe* synthetic call se actually invoke hota hai (crash nahi karta)
  3. error aaye to woh **tool ka kaam ka message** ho (traceback nahi)

Dangerous/system tools ke liye live call nahi karte — sirf schema + bound-check
(unhe chhedna system ko nuksaan pahuncha sakta hai). Include karne ke liye:
    python scripts/audit_tools.py --live-dangerous

Exit code 0 = sab call ho paye, 1 = kuch tool toota hua hai.
"""
from __future__ import annotations

import argparse
import asyncio
import json
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

# Live call se bachne wale (real side-effects)
NO_LIVE = {
    "run_shell_command", "start_background_process", "process_stop", "process_input",
    "write_file", "save_note", "append_note", "delete_note", "type_text", "press_hotkey",
    "control_volume", "take_screenshot", "open_app", "open_any_app", "close_app", "open_path",
    "play_file", "folder_file", "create_automation", "remember_fact", "refresh_system_tool",
    "clear_process_tool", "zip_create", "browser_task", "browser_research", "browser_use_cli",
    "send_file_telegram", "notification_send", "clipboard_set", "write_code_script",
    "generate_code", "fix_code", "deep_research", "run_code_script", "forget_memory",
    "update_memory", "toggle_automation", "run_automation_now", "cancel_automation",
    "set_session_note", "practice_tool",
    # process-control: inhe live call karna poore process ko maar/exec kar deta hai
    "restart_jarvis_tool", "clear_process_tool", "process_stop", "process_input",
    "start_background_process", "run_shell_command", "repl_reset", "memory_compact",
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
        out = await asyncio.wait_for(tool.ainvoke(args), timeout=45)
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
    return OK, f"{ms:.0f}ms • {text[:78].replace(chr(10), ' ')}"


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
        "Side-effect wale tools schema-check hote hain (asli call `--live-dangerous` se).",
        "",
        "| # | group | tool | status | evidence |",
        "|---|-------|------|--------|----------|",
    ]
    for i, r in enumerate(sorted(results, key=lambda x: (x["group"], x["tool"])), 1):
        detail = (r["detail"] or "").replace("|", "\\|").replace("\n", " ")[:110]
        lines.append(f"| {i} | {r['group']} | `{r['tool']}` | {r['status']} | {detail} |")
    lines += ["", "Regenerate: `python scripts/audit_tools.py --report TOOL_AUDIT.md`", ""]
    _pl.Path(path).write_text("\n".join(lines), encoding="utf-8")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Har tool call ho sakta hai ya nahi — live check")
    ap.add_argument("--live-dangerous", action="store_true",
                    help="side-effect wale tools ko bhi actually call karo (apne risk par)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--report", default="", metavar="FILE.md",
                    help="markdown report likho (e.g. TOOL_AUDIT.md) — proof ke liye")
    opts = ap.parse_args()

    from jarvis.tools import ALL_TOOLS
    from jarvis.tools import _TOOL_GROUPS as TOOL_GROUPS

    group_of = {}
    for gname, items in TOOL_GROUPS.items():
        for it in items:
            group_of.setdefault(getattr(it, "name", ""), gname)

    print("=" * 78)
    print(f"  TOOL CALLABILITY AUDIT — {len(ALL_TOOLS)} tools")
    print("=" * 78)

    results: list[dict[str, str]] = []
    for tool in sorted(ALL_TOOLS, key=lambda t: getattr(t, "name", "")):
        status, detail = await check_tool(tool, opts.live_dangerous)
        results.append({"group": group_of.get(getattr(tool, "name", ""), "?"),
                        "tool": getattr(tool, "name", "?"), "status": status, "detail": detail})

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
