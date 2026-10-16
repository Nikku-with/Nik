from __future__ import annotations

import json

import pytest
from PIL import Image

from jarvis.security import gate
from jarvis.tools import desktop


class FakeDesktopBackend:
    def __init__(self):
        self.changed = False
        self.clicks = []
        self.moves = []
        self.scrolls = []
        self.typed = []
        self.keys = []
        self.clipboard = "hello"
        self.no_change_clicks = 0
        self.drags = []
        self.key_events = []
        self.located = (40, 30)
        self.image_match = {"x": 25, "y": 15, "score": 0.96}

    def screenshot(self):
        color = (20, 30, 40) if self.changed else (10, 20, 30)
        return Image.new("RGB", (200, 100), color)

    def screen_size(self):
        return 100, 50

    def ocr(self, _image):
        return ("Saved" if self.changed else "Ready"), "ok"

    def active_window(self):
        return "Fake editor"

    def click(self, x, y, button="left"):
        self.clicks.append((x, y, button))
        if self.no_change_clicks:
            self.no_change_clicks -= 1
            return True
        self.changed = True
        return True

    def move(self, x, y):
        self.moves.append((x, y))
        return True

    def scroll(self, amount, x, y):
        self.scrolls.append((amount, x, y))
        self.changed = True
        return True

    def drag(self, x1, y1, x2, y2, button="left"):
        self.drags.append((x1, y1, x2, y2, button))
        self.changed = True
        return True

    def key_down(self, key):
        self.key_events.append(("down", key))
        return True

    def key_up(self, key):
        self.key_events.append(("up", key))
        return True

    def locate_text(self, _image, text, _exact=False):
        return self.located

    def find_image(self, _image, _path, _threshold):
        return self.image_match

    def type_text(self, text):
        self.typed.append(text)
        self.changed = True
        return True

    def press(self, keys):
        self.keys.append(keys)
        self.changed = True
        return True

    def list_windows(self):
        return ["Fake editor", "Terminal"]

    def focus_window(self, title):
        return title.lower() in "fake editor"

    def clipboard_read(self):
        return self.clipboard

    def clipboard_write(self, text):
        self.clipboard = text
        return True


def _observe_payload(result: str) -> dict:
    return json.loads(result)


@pytest.fixture
def backend():
    value = FakeDesktopBackend()
    desktop.set_desktop_backend(value)
    yield value
    desktop.set_desktop_backend(None)


@pytest.mark.asyncio
async def test_desktop_observe_registry_and_dpi_scaled_click_verifies(backend):
    from jarvis.tools import DESKTOP_CONTROL_TOOLS, tool_names, tools_for

    expected = {
        "desktop_observe", "desktop_ocr", "desktop_click", "desktop_move", "desktop_scroll",
        "desktop_type", "desktop_press", "desktop_windows", "desktop_focus",
        "desktop_clipboard_read", "desktop_clipboard_write", "desktop_drag", "click_text",
        "wait_for_text", "find_image", "describe_screen", "key_down", "key_up", "hold",
    }
    assert expected.issubset(set(tool_names()))
    assert expected.issubset(set(tool_names(tools_for("desktop"))))
    assert {tool.name for tool in DESKTOP_CONTROL_TOOLS} == expected

    observed = _observe_payload(await desktop.desktop_observe.ainvoke({}))
    assert observed["ok"] and observed["ocr_text"] == "Ready"
    assert observed["active_window"] == "Fake editor"
    assert observed["screenshot_size"] == {"width": 200, "height": 100}
    assert observed["device_size"] == {"width": 100, "height": 50}
    assert observed["scale"] == {"x": 0.5, "y": 0.5}

    click = _observe_payload(await desktop.desktop_click.ainvoke({
        "x": 100, "y": 60, "observation_id": observed["observation_id"],
        "action_summary": "open a menu",
    }))
    assert click["ok"] and click["verified"]
    assert backend.clicks == [(50, 30, "left")]
    assert click["device_coordinates"] == {"x": 50, "y": 30}


@pytest.mark.asyncio
async def test_click_needs_latest_screenshot_and_aborts_when_screen_changes(backend):
    observed = _observe_payload(await desktop.desktop_observe.ainvoke({}))
    backend.changed = True
    result = _observe_payload(await desktop.desktop_click.ainvoke({
        "x": 20, "y": 20, "observation_id": observed["observation_id"],
    }))
    assert result["ok"] is False
    assert "Screen changed" in result["error"]
    assert backend.clicks == []

    stale = _observe_payload(await desktop.desktop_click.ainvoke({
        "x": 20, "y": 20, "observation_id": "wrong-observation",
    }))
    assert stale["ok"] is False and "mismatched" in stale["error"]
    assert backend.clicks == []


@pytest.mark.asyncio
async def test_nondestructive_click_retries_once_and_destructive_click_requires_human(monkeypatch, backend):
    from jarvis.security.permissions import use_surface

    observed = _observe_payload(await desktop.desktop_observe.ainvoke({}))
    backend.no_change_clicks = 1
    retried = _observe_payload(await desktop.desktop_click.ainvoke({
        "x": 5, "y": 6, "observation_id": observed["observation_id"],
        "action_summary": "open a menu",
    }))
    assert retried["retried"] is True and retried["verified"] is True
    assert len(backend.clicks) == 2

    backend.changed = False
    backend.clicks.clear()
    observed = _observe_payload(await desktop.desktop_observe.ainvoke({}))
    broker = gate.ApprovalBroker(owner_id="desktop-owner", ttl_s=0.3)
    monkeypatch.setattr(gate, "BROKER", broker)
    gate.register_approval_handler("cli_owner", lambda _request: False)
    try:
        with use_surface("cli_owner"):
            denied = _observe_payload(await desktop.desktop_click.ainvoke({
                "x": 50, "y": 25, "observation_id": observed["observation_id"],
                "action_summary": "delete the selected record",
            }))
        assert denied["ok"] is False
        assert "cancelled" in denied["error"].lower()
        assert backend.clicks == []
    finally:
        gate.register_approval_handler("cli_owner", None)


@pytest.mark.asyncio
async def test_human_approved_destructive_click_still_revalidates_screen(monkeypatch, backend):
    from jarvis.security.permissions import use_surface

    broker = gate.ApprovalBroker(owner_id="desktop-owner", ttl_s=0.3)
    monkeypatch.setattr(gate, "BROKER", broker)
    observed = _observe_payload(await desktop.desktop_observe.ainvoke({}))

    async def approve_and_change_screen(_request):
        backend.changed = True
        return True

    gate.register_approval_handler("cli_owner", approve_and_change_screen)
    try:
        with use_surface("cli_owner"):
            result = _observe_payload(await desktop.desktop_click.ainvoke({
                "x": 60, "y": 20, "observation_id": observed["observation_id"],
                "action_summary": "send this message",
            }))
        assert result["ok"] is False
        assert "changed while approval" in result["error"]
        assert backend.clicks == []
    finally:
        gate.register_approval_handler("cli_owner", None)


@pytest.mark.asyncio
async def test_keyboard_windows_clipboard_and_ocr_tools_use_typed_backend(backend):
    observed = _observe_payload(await desktop.desktop_observe.ainvoke({}))
    typed = _observe_payload(await desktop.desktop_type.ainvoke({
        "text": "save", "observation_id": observed["observation_id"],
    }))
    assert typed["ok"] and typed["verified"] and backend.typed == ["save"]
    assert json.loads(await desktop.desktop_clipboard_write.ainvoke({"text": "updated"}))["ok"]
    assert json.loads(await desktop.desktop_clipboard_read.ainvoke({}))["text"] == "updated"
    assert json.loads(await desktop.desktop_windows.ainvoke({}))["windows"] == ["Fake editor", "Terminal"]
    assert json.loads(await desktop.desktop_focus.ainvoke({"window_title": "Fake editor"}))["ok"]
    assert json.loads(await desktop.desktop_ocr.ainvoke({}))["text"] == "Saved"


def test_coordinate_translation_handles_hidpi_and_headless_status(monkeypatch):
    assert desktop._coordinate_to_device(1000, 750, (2000, 1500), (1000, 750)) == (500, 375)
    desktop.set_desktop_backend(None)
    monkeypatch.setattr(desktop, "DESKTOP_ENABLED", True)
    monkeypatch.setattr(desktop.platform, "system", lambda: "Linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    status = desktop.desktop_status()
    assert status["headless"] is True
    assert status["available"] is False
    assert "attach a graphical desktop session" in status["detail"]
