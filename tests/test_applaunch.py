"""
tests/test_applaunch.py — app-launcher memory (spec section 13)
================================================================
  pytest tests/test_applaunch.py
  python tests/test_applaunch.py

No apps actually launched. Covers: usage stats (count/last), recents order +
dedup + cap, most-used ranking, persistence, funnel wiring (open_app /
open_any_app record on success), recents hint on failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JARVIS_OFFLINE_MODE", "1")

import jarvis.memory.session_state as SS  # noqa: E402
import jarvis.tools.app_finder as AF  # noqa: E402
import jarvis.tools.system_tools as ST  # noqa: E402

try:
    import pytest  # type: ignore

    @pytest.fixture(autouse=True)
    def _auto_isolation(tmp_path):
        real = SS._STATE
        SS._STATE = SS.SessionState(path=tmp_path / "s.json")
        yield
        SS._STATE = real
except Exception:  # pragma: no cover
    pass


def _iso_state(tmp_path=None):
    import tempfile
    import uuid

    tmp = Path(tmp_path or tempfile.mkdtemp())
    st = SS.SessionState(path=tmp / f"s-{uuid.uuid4().hex[:8]}.json")
    real = SS._STATE
    SS._STATE = st
    return st, lambda: setattr(SS, "_STATE", real)


# ---------------------------------------------------------------------------
# 1) usage stats + recents
# ---------------------------------------------------------------------------
def test_note_app_opened_stats_and_order(tmp_path=None):
    st, restore = _iso_state(tmp_path)
    try:
        st.note_app_opened("Chrome")
        st.note_app_opened("Notepad")
        st.note_app_opened("Chrome")
        assert st.recent_apps(5) == ["Chrome", "Notepad"], st.recent_apps(5)
        top = dict(st.most_used_apps(5))
        assert top.get("Chrome") == 2 and top.get("Notepad") == 1, top
        assert st.recent_apps_hint() == "Haal me khole apps: Chrome, Notepad"
        st.note_app_opened("")
        assert st.recent_apps(5) == ["Chrome", "Notepad"], "khaali naam ignore"
    finally:
        restore()


def test_recents_cap_and_dedup(tmp_path=None):
    st, restore = _iso_state(tmp_path)
    try:
        for i in range(15):
            st.note_app_opened(f"App{i}")
        assert len(st.recent_apps(20)) == 10, "cap 10"
        assert st.recent_apps(1) == ["App14"]
        st.note_app_opened("App0")  # dobara → sabse upar, duplicate nahi
        rec = st.recent_apps(20)
        assert rec[0] == "App0" and rec.count("App0") == 1 and len(rec) == 10
    finally:
        restore()


def test_most_used_ranking(tmp_path=None):
    st, restore = _iso_state(tmp_path)
    try:
        for _ in range(3):
            st.note_app_opened("brave")
        st.note_app_opened("xterm")
        ranked = st.most_used_apps(2)
        assert ranked[0] == ("brave", 3) and ranked[1][1] == 1, ranked
    finally:
        restore()


def test_stats_persist_across_reload(tmp_path=None):
    import tempfile

    tmp = Path(tmp_path or tempfile.mkdtemp())
    path = tmp / "persist.json"
    s1 = SS.SessionState(path=path)
    s1.note_app_opened("Chrome")
    s1.note_app_opened("Chrome")
    s2 = SS.SessionState(path=path)
    assert s2.recent_apps(5) == ["Chrome"]
    assert dict(s2.most_used_apps(5)).get("Chrome") == 2


def test_context_block_shows_recents(tmp_path=None):
    st, restore = _iso_state(tmp_path)
    try:
        st.note_app_opened("MyEditor")
        assert "MyEditor" in st.context_block(), "planner ko recents dikhne chahiye"
    finally:
        restore()


# ---------------------------------------------------------------------------
# 2) funnel — success par record, failure par hint
# ---------------------------------------------------------------------------
def test_open_app_success_records_and_failure_hints(tmp_path=None):
    st, restore = _iso_state(tmp_path)
    real_launch = ST._launch
    real_focus = ST.focus_window
    real_resolve = ST._resolve_app
    ST._resolve_app = lambda title: "myeditor" if title == "myeditor" else None
    ST._launch = lambda target: _ok_launch(target)  # type: ignore[assignment]
    ST.focus_window = lambda title: _no_focus(title)  # type: ignore[assignment]

    async def _ok_launch(target):
        return True, target

    async def _no_focus(title):
        return False

    try:
        out = asyncio.run(ST.open_app.ainvoke({"app_title": "myeditor"}))
        assert "launch ho gaya" in out, out
        assert st.recent_apps(3) == ["myeditor"], st.recent_apps(3)
        assert ST.was_launched_by_agent("myeditor"), "ownership bhi record honi chahiye"
        # failure → recents hint
        ST._launch = lambda target: _never(target)  # type: ignore[assignment]

        async def _never(target):
            raise AssertionError("launch hona hi nahi chahiye")

        out2 = asyncio.run(ST.open_app.ainvoke({"app_title": "zzz-no-such-app-zzz"}))
        assert "launch nahi kar paya" in out2, out2
        assert "myeditor" in out2, f"recents hint missing: {out2}"
    finally:
        ST._launch = real_launch
        ST.focus_window = real_focus
        ST._resolve_app = real_resolve
        restore()


def test_open_any_app_records_and_hints(tmp_path=None):
    st, restore = _iso_state(tmp_path)
    real_find = AF.find_apps
    real_launch_app = AF.launch_app
    real_focus = AF.focus_window
    AF.find_apps = lambda q, limit=5: [{"name": "SuperEdit", "score": 95, "kind": "x", "path": "/x"}]
    AF.launch_app = lambda best: (True, "ok")
    AF.focus_window = lambda title: _no_focus(title)  # type: ignore[assignment]

    async def _no_focus(title):
        return True

    try:
        out = asyncio.run(AF.open_any_app.ainvoke({"query": "super edit"}))
        assert "SuperEdit" in out and "launch ho gaya" in out, out
        assert st.recent_apps(3) == ["SuperEdit"], st.recent_apps(3)
        assert ST.was_launched_by_agent("SuperEdit"), "finder launches bhi owned"
        # miss → hint
        AF.find_apps = lambda q, limit=5: []
        out2 = asyncio.run(AF.open_any_app.ainvoke({"query": "zzz-nope"}))
        assert "nahi mila" in out2 and "SuperEdit" in out2, out2
    finally:
        AF.find_apps = real_find
        AF.launch_app = real_launch_app
        AF.focus_window = real_focus
        restore()


def test_record_funnel_marks_usage_and_ownership(tmp_path=None):
    st, restore = _iso_state(tmp_path)
    try:
        ST.record_app_launch("FunnelApp")
        assert ST.was_launched_by_agent("FunnelApp")
        assert st.recent_apps(3) == ["FunnelApp"]
        assert dict(st.most_used_apps(3)).get("FunnelApp") == 1
    finally:
        restore()


# ---------------------------------------------------------------------------
# script runner (repo style)
# ---------------------------------------------------------------------------
def _run_all() -> int:
    import tempfile
    import traceback

    tmp = Path(tempfile.mkdtemp(prefix="applaunch_tests_"))
    fns = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    passed, failed = 0, []
    for name, fn in fns:
        real = SS._STATE
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
            SS._STATE = real
    print("=" * 66)
    print(
        f"  RESULT: {passed}/{passed + len(failed)} checks passed  "
        f"({'ALL GOOD 🚀' if not failed else 'kuch fail hue ⚠️'})"
    )
    print("=" * 66)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
