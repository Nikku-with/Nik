"""
tests/test_automation.py — automation anywhere (spec section 8)
================================================================
  pytest tests/test_automation.py
  python tests/test_automation.py

No network. Covers: NL schedule parsing, store lifecycle (isolated file),
channel capture, Telegram delivery (mocked), dangerous-step gating on the
automation surface, ensure_scheduler idempotency, background firing.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JARVIS_OFFLINE_MODE", "1")

from jarvis.automation import scheduler as sch  # noqa: E402
from jarvis.automation import tools as AT  # noqa: E402
from jarvis.config import settings as S  # noqa: E402
from jarvis.security import gate as G  # noqa: E402
from jarvis.security.gate import check_shell, gate_context  # noqa: E402

try:
    import pytest  # type: ignore

    @pytest.fixture(autouse=True)
    def _auto_isolation(tmp_path):
        G.reset_broker_for_tests()
        sch.stop_background()
        time.sleep(0.05)
        yield
        sch.stop_background()
        G.reset_broker_for_tests()
except Exception:  # pragma: no cover
    pass


def _iso_store(tmp_path=None):
    import tempfile
    import uuid

    tmp = Path(tmp_path or tempfile.mkdtemp())
    # har test ko apni file (script-runner me tmp dir shared hota hai)
    return sch.AutomationStore(path=tmp / f"automations-{uuid.uuid4().hex[:8]}.json")


# ---------------------------------------------------------------------------
# 1) NL schedule parsing
# ---------------------------------------------------------------------------
def test_parse_schedule_matrix():
    assert sch.parse_schedule("har subah 8 baje mausam batao")["schedule"] == "daily 08:00"
    assert sch.parse_schedule("daily 18:30 backup lo")["schedule"] == "daily 18:30"
    assert sch.parse_schedule("every 2h check karo")["schedule"] == "every 2 h"
    assert sch.parse_schedule("har 30 minute ye karo")["schedule"] == "every 30 min"
    assert sch.parse_schedule("har ghante report bhejo")["kind"] == "interval"
    once = sch.parse_schedule("kal 6 PM par ye karna")
    assert once["kind"] == "once" and once["at"] > time.time()
    assert sch.parse_schedule("6 PM par ye task chala do")["kind"] == "once"
    bad = sch.parse_schedule("kabhi kabhi jab man kare")
    assert bad["kind"] == "" and "error" in bad


def test_parse_schedule_hinglish_meridiem():
    assert sch.parse_schedule("har shaam 6 baje chai yaad dilao")["schedule"] == "daily 18:00"
    assert sch.parse_schedule("roz subah 7:30 baje utha do")["schedule"] == "daily 07:30"
    assert sch.parse_schedule("har raat 11 baje backup")["schedule"] == "daily 23:00"


# ---------------------------------------------------------------------------
# 2) store lifecycle (isolated file)
# ---------------------------------------------------------------------------
def test_store_create_list_toggle_cancel(tmp_path=None):
    store = _iso_store(tmp_path)
    assert store.list() == []
    res = store.create("mausam batao", "har subah 8 baje", channel="telegram_owner")
    assert res["ok"], res
    auto = res["automation"]
    assert auto["channel"] == "telegram_owner" and auto["enabled"]
    assert auto["next_run"] > time.time()
    assert len(store.list()) == 1
    ident = auto["id"][:8]
    assert store.get(ident).id == auto["id"]
    assert store.get("mausam").id == auto["id"], "task-text se bhi milna chahiye"
    off = store.set_enabled(ident, False)
    assert off["ok"] and store.get(ident).enabled is False
    assert store.list(enabled_only=True) == []
    store.set_enabled(ident, True)
    assert store.list(enabled_only=True) != []
    gone = store.cancel(ident)
    assert gone["ok"] and store.list() == []
    assert store.get(ident) is None
    assert store.cancel("nope")["ok"] is False


def test_store_mark_run_transitions(tmp_path=None):
    store = _iso_store(tmp_path)
    once = store.create("x", "kal 6 PM", channel="cli")["automation"]
    a = store.get(once["id"])
    store.mark_run(a, "ho gaya")
    assert a.enabled is False and a.next_run == 0.0 and a.runs == 1, "once → disable"
    assert a.last_result == "ho gaya"
    daily = store.create("y", "har subah 8 baje")["automation"]
    b = store.get(daily["id"])
    first_next = b.next_run
    store.mark_run(b, "ok")
    assert b.enabled and abs(b.next_run - first_next - 86400) < 5, "daily → +1 din"
    iv = store.create("z", "every 2h")["automation"]
    c = store.get(iv["id"])
    store.mark_run(c, "ok")
    assert abs(c.next_run - (time.time() + 7200)) < 120, "interval → +2h"
    store.mark_run(c, "", error="boom")
    assert c.errors == 1 and c.runs == 2


def test_store_due_and_persistence(tmp_path=None):
    import tempfile

    tmp = Path(tmp_path or tempfile.mkdtemp())
    path = tmp / "a.json"
    s1 = sch.AutomationStore(path=path)
    r = s1.create("now karo", "every 1h")
    a = s1.get(r["automation"]["id"])
    a.next_run = time.time() - 5  # overdue
    s1.save()
    s2 = sch.AutomationStore(path=path)  # dobara load
    assert len(s2.list()) == 1
    assert len(s2.due()) == 1, "overdue automation due honi chahiye"
    s2.set_enabled(a.id, False)
    assert s2.due() == [], "OFF automation due nahi"


# ---------------------------------------------------------------------------
# 3) channel capture + Telegram delivery
# ---------------------------------------------------------------------------
def test_create_tool_captures_surface_channel(tmp_path=None):
    store = _iso_store(tmp_path)
    real_get_store = sch.get_store
    sch.get_store = lambda: store
    try:
        with gate_context("telegram_owner"):
            out = asyncio.run(AT.create_automation.ainvoke({"task": "mausam batao", "schedule": "har subah 8 baje"}))
        assert "Automation set" in out, out
        items = store.list()
        assert len(items) == 1 and items[0]["channel"] == "telegram_owner", items
    finally:
        sch.get_store = real_get_store


def test_execute_delivers_to_telegram_and_cli_silent(tmp_path=None):
    from jarvis.tools import telegram_tools as TT

    store = _iso_store(tmp_path)
    real_get_store, real_runner = sch.get_store, sch.RUNNER
    sch.get_store = lambda: store
    sch.RUNNER = lambda task, aid: f"result-of:{task}"
    posted: list[dict] = []
    orig_post = TT._api_post
    TT._api_post = lambda token, method, payload: (posted.append({"token": token, "payload": payload}), {"ok": True})[1]
    restore = _patch_telegram("TOK", "111")
    try:
        tg = store.create("mausam batao", "har subah 8 baje", channel="telegram_owner")
        out = sch.run_now(tg["automation"]["id"][:8])
        assert out["ok"] and "result-of:mausam batao" in out["message"], out
        assert len(posted) == 1, "Telegram delivery honi chahiye thi"
        assert posted[0]["payload"]["chat_id"] == "111"
        assert "mausam batao" in posted[0]["payload"]["text"]
        # cli channel → koi push nahi
        cli = store.create("files gin lo", "every 1h", channel="cli")
        sch.run_now(cli["automation"]["id"][:8])
        assert len(posted) == 1, "cli par push nahi hona chahiye"
    finally:
        TT._api_post = orig_post
        sch.get_store, sch.RUNNER = real_get_store, real_runner
        restore()


def _patch_telegram(token, owner):
    before = (S.TELEGRAM_BOT_TOKEN, S.TELEGRAM_OWNER_ID)
    S.TELEGRAM_BOT_TOKEN, S.TELEGRAM_OWNER_ID = token, owner
    return lambda: setattr(S, "TELEGRAM_BOT_TOKEN", before[0]) or setattr(S, "TELEGRAM_OWNER_ID", before[1])


def test_delivery_failure_does_not_fail_run(tmp_path=None):
    from jarvis.tools import telegram_tools as TT

    store = _iso_store(tmp_path)
    real_get_store, real_runner = sch.get_store, sch.RUNNER
    sch.get_store = lambda: store
    sch.RUNNER = lambda task, aid: "fine"
    orig_post = TT._api_post
    TT._api_post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net down"))
    restore = _patch_telegram("TOK", "111")
    try:
        tg = store.create("x karo", "har subah 8 baje", channel="telegram_owner")
        auto = store.get(tg["automation"]["id"])
        out = sch._execute(auto)
        assert "fine" in out and "notify fail" in out, out
        assert auto.runs == 1 and auto.errors == 0, "notify fail ≠ run fail"
    finally:
        TT._api_post = orig_post
        sch.get_store, sch.RUNNER = real_get_store, real_runner
        restore()


# ---------------------------------------------------------------------------
# 4) dangerous automation steps gated (automation surface, never auto-approve)
# ---------------------------------------------------------------------------
def test_automation_surface_gates_dangerous(tmp_path=None):
    from jarvis.config import runtime as R

    store = _iso_store(tmp_path)
    real_get_store, real_runner = sch.get_store, sch.RUNNER
    sch.get_store = lambda: store
    sch.RUNNER = lambda task, aid: check_shell(task, timeout_s=3).message
    before = R.get("exec_approval", None)
    R.set_many(exec_approval="risky")
    try:
        tg = store.create("placeholder", "every 1h", channel="cli")
        auto = store.get(tg["automation"]["id"])
        auto.task = "rm -rf /tmp/auto-danger-probe"  # dangerous shell as task
        out = sch._execute(auto)
        assert "Cancel" in out, out  # no asker on automation → deny
        assert auto.runs == 1
    finally:
        sch.get_store, sch.RUNNER = real_get_store, real_runner
        if before:
            R.set_many(exec_approval=before)
        else:
            R.reset("exec_approval")


def test_automation_runner_none_message(tmp_path=None):
    store = _iso_store(tmp_path)
    real_get_store, real_runner = sch.get_store, sch.RUNNER
    sch.get_store, sch.RUNNER = lambda: store, None
    try:
        r = store.create("x", "every 1h")
        out = sch._execute(store.get(r["automation"]["id"]))
        assert "runner set nahi" in out, out
    finally:
        sch.get_store, sch.RUNNER = real_get_store, real_runner


# ---------------------------------------------------------------------------
# 5) ensure_scheduler + background firing
# ---------------------------------------------------------------------------
def test_ensure_scheduler_idempotent():
    real_runner = sch.RUNNER
    sch.stop_background()
    time.sleep(0.05)
    try:
        sch.RUNNER = None
        # skar: stop thread state reset ke liye
        import jarvis.automation.scheduler as S2

        S2._THREAD = None
        st1 = sch.ensure_scheduler(runner=lambda t, a: "ok", interval=60)
        assert st1["running"] and st1["started_now"] is True
        st2 = sch.ensure_scheduler(interval=60)
        assert st2["running"] and st2["started_now"] is False, "dobara thread nahi"
        assert sch.RUNNER("t", "a") == "ok"
    finally:
        sch.stop_background()
        sch.RUNNER = real_runner


def test_background_loop_fires_due(tmp_path=None):
    store = _iso_store(tmp_path)
    real_get_store, real_runner = sch.get_store, sch.RUNNER
    sch.get_store = lambda: store
    fired: list[str] = []
    sch.RUNNER = lambda task, aid: (fired.append(aid), "done")[1]
    sch.stop_background()
    time.sleep(0.05)
    import jarvis.automation.scheduler as S2

    S2._THREAD = None
    try:
        r = store.create("fire me", "every 1h", channel="cli")
        auto = store.get(r["automation"]["id"])
        auto.next_run = time.time() - 1  # due now
        sch.start_background(interval=0.1)
        for _ in range(100):
            if fired:
                break
            time.sleep(0.05)
        assert fired == [auto.id], "due automation fire honi chahiye thi"
        assert auto.runs == 1
    finally:
        sch.stop_background()
        sch.get_store, sch.RUNNER = real_get_store, real_runner


def test_default_runner_uses_agent_fast_path():
    out = sch._default_runner("time kya hai", "probe-1")
    assert "🕒" in out or "UTC" in out, out[:200]


# ---------------------------------------------------------------------------
# script runner (repo style)
# ---------------------------------------------------------------------------
def _run_all() -> int:
    import tempfile
    import traceback

    tmp = Path(tempfile.mkdtemp(prefix="auto_tests_"))
    fns = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    passed, failed = 0, []
    for name, fn in fns:
        G.reset_broker_for_tests()
        sch.stop_background()
        try:
            import inspect

            if "tmp_path" in inspect.signature(fn).parameters:
                fn(tmp_path=tmp)
            else:
                fn()
            print(f"  ✅ {name}")
            passed += 1
        except Exception as exc:
            print(f"  ❌ {name} — {exc}")
            traceback.print_exc(limit=4)
            failed.append(name)
        finally:
            sch.stop_background()
    G.reset_broker_for_tests()
    print("=" * 66)
    print(
        f"  RESULT: {passed}/{passed + len(failed)} checks passed  "
        f"({'ALL GOOD 🚀' if not failed else 'kuch fail hue ⚠️'})"
    )
    print("=" * 66)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
