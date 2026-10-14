"""
tests/test_desktop.py — keyboard/mouse/screen-vision (spec section 12)
=======================================================================
  pytest tests/test_desktop.py
  python tests/test_desktop.py

No display needed. Covers: click_at validation + backends (stub pyautogui,
fake xdotool, honest headless failure), take_screenshot refactor, vision
screen reading (demo model + failure path), SYSTEM permissions, NL intents.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JARVIS_OFFLINE_MODE", "1")

import jarvis.tools.system_tools as ST  # noqa: E402
from jarvis.graph.nodes import _execute_desktop_action, detect_desktop_action  # noqa: E402
from jarvis.security.permissions import check  # noqa: E402

# 1x1 PNG (real bytes — PIL bhi khol lega)
TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

try:
    import pytest  # type: ignore

    @pytest.fixture(autouse=True)
    def _auto_isolation():
        yield
except Exception:  # pragma: no cover
    pass


def _no_pyautogui():
    saved = sys.modules.get("pyautogui")
    sys.modules["pyautogui"] = None  # type: ignore[assignment]
    return lambda: sys.modules.pop("pyautogui", None) if saved is None else sys.modules.__setitem__("pyautogui", saved)


# ---------------------------------------------------------------------------
# 1) click_at validation (koi click nahi — sirf guards)
# ---------------------------------------------------------------------------
def test_click_validation():
    # galat type schema par hi reject (pydantic) — tool tak pahunchta hi nahi
    try:
        asyncio.run(ST.click_at.ainvoke({"x": "abc", "y": 10}))
    except Exception as exc:
        assert "valid integer" in str(exc), exc
    else:
        raise AssertionError("non-int x accept ho gaya?!")
    assert "0..10000" in asyncio.run(ST.click_at.ainvoke({"x": -5, "y": 10}))
    assert "0..10000" in asyncio.run(ST.click_at.ainvoke({"x": 10, "y": 99999}))
    assert "left/right/middle" in asyncio.run(ST.click_at.ainvoke({"x": 10, "y": 10, "button": "side"}))


def test_click_via_stub_pyautogui():
    calls: list[dict] = []
    stub = types.ModuleType("pyautogui")
    stub.click = lambda **kw: calls.append(kw)
    saved = sys.modules.get("pyautogui")
    sys.modules["pyautogui"] = stub
    try:
        out = asyncio.run(ST.click_at.ainvoke({"x": 500, "y": 300}))
        assert "Click (500,300)" in out and "pyautogui" in out, out
        assert calls == [{"x": 500, "y": 300, "button": "left", "clicks": 1}], calls
        out2 = asyncio.run(ST.click_at.ainvoke({"x": 1, "y": 2, "button": "right", "clicks": 2}))
        assert "right x2" in out2, out2
    finally:
        if saved is None:
            sys.modules.pop("pyautogui", None)
        else:
            sys.modules["pyautogui"] = saved


def test_click_headless_honest_failure(tmp_path=None):
    import tempfile

    restore = _no_pyautogui()
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(Path(tmp_path or tempfile.mkdtemp()))  # khaali dir — koi tool nahi
    try:
        if not ST.IS_LINUX:
            return
        out = asyncio.run(ST.click_at.ainvoke({"x": 10, "y": 10}))
        assert out.startswith("❌ click fail"), out
        assert "xdotool" in out or "pyautogui" in out, out
    finally:
        os.environ["PATH"] = old_path
        restore()


def test_native_click_xdotool_path(tmp_path=None):
    import stat
    import tempfile

    if not ST.IS_LINUX:
        return
    tmp = Path(tmp_path or tempfile.mkdtemp())
    fake = tmp / "xdotool"
    logf = tmp / "args.log"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {logf}\nexit 0\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{tmp}{os.pathsep}{old_path}"
    try:
        ok, detail = asyncio.run(ST._native_click(500, 300, "left", 1))
        assert ok and detail == "xdotool", (ok, detail)
        logged = logf.read_text(encoding="utf-8")
        assert "mousemove 500 300" in logged and "click 1" in logged, logged
        ok2, _ = asyncio.run(ST._native_click(1, 2, "right", 2))
        assert ok2
        assert logf.read_text(encoding="utf-8").count("click 3") == 2
    finally:
        os.environ["PATH"] = old_path


# ---------------------------------------------------------------------------
# 2) screenshots — refactor intact + headless honesty
# ---------------------------------------------------------------------------
def test_take_screenshot_via_stub_pyautogui(tmp_path=None):
    import tempfile

    tmp = Path(tmp_path or tempfile.mkdtemp())
    real_dir = ST.SCREENSHOTS_DIR
    ST.SCREENSHOTS_DIR = tmp

    class FakeImg:
        def save(self, path):
            Path(path).write_bytes(b"fakepng-bytes-here")

    stub = types.ModuleType("pyautogui")
    stub.screenshot = lambda: FakeImg()
    saved = sys.modules.get("pyautogui")
    sys.modules["pyautogui"] = stub
    try:
        out = asyncio.run(ST.take_screenshot.ainvoke({"name": "probe shot"}))
        assert "Screenshot saved" in out and "probe_shot.png" in out, out
        assert (tmp / "probe_shot.png").exists()
    finally:
        ST.SCREENSHOTS_DIR = real_dir
        if saved is None:
            sys.modules.pop("pyautogui", None)
        else:
            sys.modules["pyautogui"] = saved


def test_take_screenshot_headless_honest(tmp_path=None):
    import tempfile

    restore = _no_pyautogui()
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(Path(tmp_path or tempfile.mkdtemp()))
    try:
        if not ST.IS_LINUX:
            return
        out = asyncio.run(ST.take_screenshot.ainvoke({"name": "x"}))
        assert out.startswith("❌ Screenshot nahi ho paya"), out
    finally:
        os.environ["PATH"] = old_path
        restore()


# ---------------------------------------------------------------------------
# 3) describe_screen — vision reading
# ---------------------------------------------------------------------------
def test_describe_screen_demo_vision(tmp_path=None):
    import base64
    import tempfile

    tmp = Path(tmp_path or tempfile.mkdtemp())
    real_dir = ST.SCREENSHOTS_DIR
    ST.SCREENSHOTS_DIR = tmp
    real_cap = ST._capture_screenshot

    async def fake_cap(path):
        Path(path).write_bytes(base64.b64decode(TINY_PNG_B64))
        return True, "stub"

    ST._capture_screenshot = fake_cap  # type: ignore[assignment]
    try:
        out = asyncio.run(ST.describe_screen.ainvoke({"question": "kya dikh raha hai?"}))
        assert out.startswith("👁️"), out[:200]
        assert "screen_describe.png" in out, out[-200:]
        assert (tmp / "screen_describe.png").exists()
    finally:
        ST._capture_screenshot = real_cap
        ST.SCREENSHOTS_DIR = real_dir


def test_describe_screen_capture_fail_and_model_fail(tmp_path=None):
    import tempfile

    tmp = Path(tmp_path or tempfile.mkdtemp())
    real_dir = ST.SCREENSHOTS_DIR
    ST.SCREENSHOTS_DIR = tmp
    real_cap = ST._capture_screenshot
    ST._capture_screenshot = lambda path: _fail_cap()  # type: ignore[assignment]

    async def _fail_cap():
        return False, "no display"

    try:
        out = asyncio.run(ST.describe_screen.ainvoke({}))
        assert "screenshot fail" in out, out
    finally:
        ST._capture_screenshot = real_cap

    # capture ok, model down → warning + path (screenshot bekaar nahi jaata)
    import base64

    async def fake_cap2(path):
        Path(path).write_bytes(base64.b64decode(TINY_PNG_B64))
        return True, "stub"

    ST._capture_screenshot = fake_cap2  # type: ignore[assignment]
    import jarvis.llm as LLM

    real_model = LLM.get_chat_model
    LLM.get_chat_model = lambda role="vision": (_ for _ in ()).throw(RuntimeError("vision down"))
    try:
        out = asyncio.run(ST.describe_screen.ainvoke({}))
        assert out.startswith("⚠️ Vision model fail") and "screen_describe.png" in out, out[:250]
    finally:
        LLM.get_chat_model = real_model
        ST._capture_screenshot = real_cap
        ST.SCREENSHOTS_DIR = real_dir


# ---------------------------------------------------------------------------
# 4) permissions — owner-only (SYSTEM)
# ---------------------------------------------------------------------------
def test_desktop_tools_owner_only():
    for tool in ("click_at", "describe_screen", "take_screenshot", "type_text"):
        assert check(tool, "telegram_guest").allowed is False, tool
        assert check(tool, "telegram_owner").allowed is True, tool
        assert check(tool, "cli_owner").allowed is True, tool
    # browser screenshot EXECUTE hi raha (regression: SYSTEM keyword order)
    assert check("browser_screenshot", "automation").allowed is True


# ---------------------------------------------------------------------------
# 5) NL intents — click + describe (+ purane intact)
# ---------------------------------------------------------------------------
def test_detect_click_intents():
    d = detect_desktop_action("500 300 par click karo")
    assert d["actions"][0] == {"kind": "click", "x": 500, "y": 300, "button": "left", "clicks": 1}, d
    d = detect_desktop_action("click at 100,200 right double")
    assert d["actions"][0]["button"] == "right" and d["actions"][0]["clicks"] == 2, d
    d = detect_desktop_action("(42, 24) dabao")
    assert (d["actions"][0]["x"], d["actions"][0]["y"]) == (42, 24), d


def test_detect_describe_intents():
    for text in (
        "screen par kya hai",
        "screen par kya dikh raha hai",
        "screen describe karo",
        "display me kya likha hai bata",
    ):
        d = detect_desktop_action(text)
        kinds = [a["kind"] for a in d.get("actions", [])]
        assert "describe_screen" in kinds, (text, d)


def test_detect_old_intents_intact():
    assert detect_desktop_action("notepad kholo")["actions"][0]["kind"] == "open_app"
    assert "screenshot" in [a["kind"] for a in detect_desktop_action("screenshot lo").get("actions", [])]
    d = detect_desktop_action("hello world likho")
    assert d["actions"][0]["kind"] == "type_text" and "hello world" in d["actions"][0]["text"]
    assert detect_desktop_action("aaj ka mausam batao") == {}


def test_execute_new_kinds(tmp_path=None):
    import base64
    import tempfile

    tmp = Path(tmp_path or tempfile.mkdtemp())
    real_dir = ST.SCREENSHOTS_DIR
    ST.SCREENSHOTS_DIR = tmp
    real_cap = ST._capture_screenshot

    async def fake_cap(path):
        Path(path).write_bytes(base64.b64decode(TINY_PNG_B64))
        return True, "stub"

    ST._capture_screenshot = fake_cap  # type: ignore[assignment]
    clicks: list[dict] = []
    stub = types.ModuleType("pyautogui")
    stub.click = lambda **kw: clicks.append(kw)
    saved = sys.modules.get("pyautogui")
    sys.modules["pyautogui"] = stub
    try:
        recs = asyncio.run(
            _execute_desktop_action(
                {"actions": [{"kind": "click", "x": 7, "y": 9, "button": "left", "clicks": 1}]}, [ST.click_at]
            )
        )
        assert len(recs) == 1 and recs[0]["ok"] and recs[0]["source"] == "deterministic", recs
        assert clicks == [{"x": 7, "y": 9, "button": "left", "clicks": 1}]
        recs = asyncio.run(
            _execute_desktop_action(
                {"actions": [{"kind": "describe_screen", "question": "kya hai?"}]}, [ST.describe_screen]
            )
        )
        assert len(recs) == 1 and recs[0]["ok"], recs
    finally:
        ST._capture_screenshot = real_cap
        ST.SCREENSHOTS_DIR = real_dir
        if saved is None:
            sys.modules.pop("pyautogui", None)
        else:
            sys.modules["pyautogui"] = saved


# ---------------------------------------------------------------------------
# script runner (repo style)
# ---------------------------------------------------------------------------
def _run_all() -> int:
    import tempfile
    import traceback

    tmp = Path(tempfile.mkdtemp(prefix="desktop_tests_"))
    fns = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    passed, failed = 0, []
    for name, fn in fns:
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
    print("=" * 66)
    print(
        f"  RESULT: {passed}/{passed + len(failed)} checks passed  "
        f"({'ALL GOOD 🚀' if not failed else 'kuch fail hue ⚠️'})"
    )
    print("=" * 66)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
