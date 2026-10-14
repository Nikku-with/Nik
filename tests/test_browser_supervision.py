"""
tests/test_browser_supervision.py — browser-use supervision + tool-request channel (spec §14)
=============================================================================================
  pytest tests/test_browser_supervision.py
  python tests/test_browser_supervision.py

No network, no real browser. Covers: supervised allowlist execution, dangerous-tool
refusal, bad/oversized args, audit trail, jarvis_tool action registration, step
tracking + progress events, per-run log files, doctor supervision lines.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JARVIS_OFFLINE_MODE", "1")

import jarvis.tools.browser_tools as bt  # noqa: E402
from jarvis.runtime.progress import CollectingSink, emit, use_sink  # noqa: E402

PASS = 0


def check(name, cond, extra=""):
    global PASS
    assert cond, f"FAIL: {name} {extra}"
    PASS += 1
    print(f"  ✅ {name}")


def _fresh_audit():
    bt._LAST_RESULT.clear()
    bt._LAST_RESULT.update({"task": "t", "started": 0.0, "steps": [], "progress": []})


# ---------------------------------------------------------------------------
# supervised tool-request channel
# ---------------------------------------------------------------------------


def test_allowed_tool_executes():
    _fresh_audit()
    out = asyncio.run(bt._supervisor_tool_call("get_current_datetime", "{}"))
    check("allowed tool runs", out and not out.startswith("❌"), out[:80])
    check(
        "audit ok entry", any(e["name"] == "get_current_datetime" and e["ok"] for e in bt._LAST_RESULT["tool_requests"])
    )


def test_dangerous_tools_refused():
    _fresh_audit()
    for nasty in ("run_shell_command", "delete_file", "browser_cli", "type_text"):
        out = asyncio.run(bt._supervisor_tool_call(nasty, "{}"))
        check(f"{nasty} refused", out.startswith("❌") and "google_search" in out, out[:100])
    check("refusals audited", all(not e["ok"] for e in bt._LAST_RESULT["tool_requests"]))


def test_bad_and_oversized_args():
    _fresh_audit()
    out = asyncio.run(bt._supervisor_tool_call("get_weather", "{oops"))
    check("bad json refused", "valid JSON" in out, out[:80])
    out = asyncio.run(bt._supervisor_tool_call("get_weather", "[1,2]"))
    check("non-object refused", "JSON object" in out, out[:80])
    out = asyncio.run(bt._supervisor_tool_call("save_note", '{"a": "' + "x" * 5000 + '"}'))
    check("oversized refused", "too large" in out and "4096" in out, out[:80])


def test_long_result_truncated():
    _fresh_audit()

    class _Big:
        async def ainvoke(self, args):
            return "z" * 5000

    old_cache = bt._ALL_TOOLS_CACHE
    bt._ALL_TOOLS_CACHE = {"get_weather": _Big()}
    try:
        out = asyncio.run(bt._supervisor_tool_call("get_weather", "{}"))
    finally:
        bt._ALL_TOOLS_CACHE = old_cache
    check("result truncated", len(out) < 5000 and "truncated" in out, f"len={len(out)}")


def test_jarvis_tool_action_registered():
    tools = bt._build_browser_tools()
    actions = tools.registry.registry.actions
    for name in ("jarvis_tool", "jarvis_save_note", "jarvis_remember", "jarvis_progress"):
        check(f"action {name}", name in actions)
    desc = str(actions["jarvis_tool"].description or "")
    check("jarvis_tool advertises allowlist", "google_search" in desc, desc[:100])


def test_jarvis_tool_action_calls_supervisor():
    tools = bt._build_browser_tools()
    fn = tools.registry.registry.actions["jarvis_tool"].function
    _fresh_audit()
    out = asyncio.run(fn(tool_name="run_shell_command", arguments_json="{}"))
    check("action refusal end-to-end", out.startswith("❌") and "Allowed" in out, out[:80])


# ---------------------------------------------------------------------------
# step tracking + run logs
# ---------------------------------------------------------------------------


def test_record_browser_step_tracks_and_emits():
    _fresh_audit()
    sink = CollectingSink()
    with use_sink(sink):
        bt.record_browser_step(3, "https://example.com/a?x=1", emit)
    steps = bt._LAST_RESULT["steps"]
    check("step appended", steps and steps[-1]["step"] == 3 and "example.com" in steps[-1]["url"])
    kinds = [(e.kind, e.text) for e in sink.events]
    check("thinking emitted with host", any(k == "thinking" and "example.com" in m for k, m in kinds), str(kinds))


def test_offline_run_writes_log_with_run_id(tmp_path):
    async def _no_agent(*a, **k):
        return None

    async def _no_http(task, sites=None):
        return "(test http fallback)"

    old_build, old_http = bt.build_browser_agent, bt._http_fallback
    old_env = os.environ.get("JARVIS_BROWSER_RUNS_DIR")
    os.environ["JARVIS_BROWSER_RUNS_DIR"] = str(tmp_path)
    bt.build_browser_agent, bt._http_fallback = _no_agent, _no_http
    try:
        outcome = asyncio.run(bt.run_browser_task(task="test task alpha", start_url="https://x.example"))
    finally:
        bt.build_browser_agent, bt._http_fallback = old_build, old_http
        if old_env is None:
            os.environ.pop("JARVIS_BROWSER_RUNS_DIR", None)
        else:
            os.environ["JARVIS_BROWSER_RUNS_DIR"] = old_env
    check("offline payload", outcome["offline"] is True)
    check("run_id present", outcome.get("run_id", "").startswith("br-task-"), outcome.get("run_id"))
    log = Path(outcome.get("run_log", ""))
    check("run log file exists", log.is_file(), str(log))
    record = json.loads(log.read_text(encoding="utf-8"))
    check("log has task", "test task alpha" in record.get("task", ""), str(record)[:120])


def test_browser_task_tool_shows_run_id(tmp_path):
    async def _no_agent(*a, **k):
        return None

    async def _no_http(task, sites=None):
        return "(test http fallback)"

    old_build, old_http = bt.build_browser_agent, bt._http_fallback
    old_env = os.environ.get("JARVIS_BROWSER_RUNS_DIR")
    os.environ["JARVIS_BROWSER_RUNS_DIR"] = str(tmp_path)
    bt.build_browser_agent, bt._http_fallback = _no_agent, _no_http
    try:
        text = asyncio.run(bt.browser_task.ainvoke({"task": "hello browser"}))
    finally:
        bt.build_browser_agent, bt._http_fallback = old_build, old_http
        if old_env is None:
            os.environ.pop("JARVIS_BROWSER_RUNS_DIR", None)
        else:
            os.environ["JARVIS_BROWSER_RUNS_DIR"] = old_env
    check("tool report has run line", "🧾 run: br-task-" in text, text[-200:])


def test_doctor_supervision_lines():
    text = asyncio.run(bt.browser_doctor.ainvoke({}))
    check("doctor supervised count", "supervised tools" in text and str(len(bt.SUPERVISED_TOOL_ALLOWLIST)) in text)
    check("doctor run logs dir", "run logs" in text and "browser_runs" in text, text)


if __name__ == "__main__":
    import tempfile

    print("== test_browser_supervision ==")
    test_allowed_tool_executes()
    test_dangerous_tools_refused()
    test_bad_and_oversized_args()
    test_long_result_truncated()
    test_jarvis_tool_action_registered()
    test_jarvis_tool_action_calls_supervisor()
    test_record_browser_step_tracks_and_emits()
    with tempfile.TemporaryDirectory() as td:
        test_offline_run_writes_log_with_run_id(Path(td))
        test_browser_task_tool_shows_run_id(Path(td))
    test_doctor_supervision_lines()
    print(f"\nALL {PASS} CHECKS PASSED ✅")
