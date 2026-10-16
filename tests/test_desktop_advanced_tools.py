from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from jarvis.tools import desktop


class FakeBackend:
    def __init__(self):
        from PIL import Image

        self.Image = Image
        self.changed = False
        self.clicks = []
        self.drags = []
        self.keys = []
        self.locate = (30, 20)
        self.match = {"x": 25, "y": 15, "score": 0.96}

    def screenshot(self):
        return self.Image.new("RGB", (100, 50), (0, 0, 0) if not self.changed else (1, 1, 1))

    def screen_size(self):
        return (50, 25)

    def ocr(self, _image):
        return ("Ready" if not self.changed else "Saved", "ok")

    def active_window(self):
        return "Fake Window"

    def click(self, x, y, button="left"):
        self.clicks.append((x, y, button))
        self.changed = True
        return True

    def drag(self, x1, y1, x2, y2, button="left"):
        self.drags.append((x1, y1, x2, y2, button))
        self.changed = True
        return True

    def locate_text(self, _image, _text, _exact=False):
        return self.locate

    def find_image(self, _image, _path, _threshold):
        return self.match

    def key_down(self, key):
        self.keys.append(("down", key))
        return True

    def key_up(self, key):
        self.keys.append(("up", key))
        return True


def observe_result(raw):
    return json.loads(raw)


@pytest.fixture
def backend():
    value = FakeBackend()
    desktop.set_desktop_backend(value)
    yield value
    desktop.set_desktop_backend(None)


@pytest.mark.asyncio
async def test_drag_scales_coordinates_and_verifies_visible_change(backend):
    observed = observe_result(await desktop.desktop_observe.ainvoke({}))
    result = observe_result(await desktop.desktop_drag.ainvoke({
        "x1": 20, "y1": 10, "x2": 80, "y2": 40,
        "observation_id": observed["observation_id"], "action_summary": "move a window",
    }))
    assert result["ok"] and result["verified"]
    assert backend.drags == [(10, 5, 40, 20, "left")]


@pytest.mark.asyncio
async def test_click_text_uses_ocr_location_and_normal_click_verification(backend):
    observed = observe_result(await desktop.desktop_observe.ainvoke({}))
    result = observe_result(await desktop.click_text.ainvoke({
        "text": "Save", "observation_id": observed["observation_id"],
    }))
    assert result["ok"] and result["verified"]
    assert result["matched_text"] == "Save"
    assert result["locator"]["source"] == "backend"
    assert backend.clicks == [(15, 10, "left")]


@pytest.mark.asyncio
async def test_wait_for_text_and_find_image_report_confirmed_matches(backend, tmp_path):
    found = observe_result(await desktop.wait_for_text.ainvoke({"text": "Ready", "timeout": 0.01}))
    assert found["found"] and found["verified"]
    missed = observe_result(await desktop.wait_for_text.ainvoke({"text": "not present", "timeout": 0}))
    assert missed["ok"] and not missed["found"]

    template = tmp_path / "button.png"
    template.write_bytes(b"test image placeholder")
    image_match = observe_result(await desktop.find_image.ainvoke({"image_path": str(template), "threshold": 0.9}))
    assert image_match["found"] and image_match["x"] == 25 and image_match["score"] == 0.96


@pytest.mark.asyncio
async def test_describe_screen_gracefully_reports_missing_vision_role(monkeypatch, backend):
    import jarvis.llm as llm

    monkeypatch.setattr(llm, "resolve_role", lambda *_args, **_kwargs: SimpleNamespace(targets=[]))
    result = observe_result(await desktop.describe_screen.ainvoke({}))
    assert not result["ok"] and result["status"] == "unavailable"
    assert "No vision-role model" in result["message"]


@pytest.mark.asyncio
async def test_describe_screen_sends_screenshot_only_through_vision_role(monkeypatch, backend):
    import jarvis.llm as llm

    class FakeModel:
        calls = 0
        payload = None

        async def ainvoke(self, messages):
            self.calls += 1
            self.payload = messages
            return SimpleNamespace(content="A fake editor is open.")

    model = FakeModel()
    monkeypatch.setattr(llm, "resolve_role", lambda *_args, **_kwargs: SimpleNamespace(targets=["fake:vision-model"]))
    monkeypatch.setattr(llm, "get_chat_model", lambda *_args, **_kwargs: model)
    result = observe_result(await desktop.describe_screen.ainvoke({"question": "What is visible?"}))
    assert result["ok"] and result["status"] == "described"
    assert result["description"] == "A fake editor is open."
    assert model.calls == 1
    assert any("data:image/png;base64," in str(getattr(message, "content", "")) for message in model.payload)


@pytest.mark.asyncio
async def test_key_down_up_and_bounded_hold_always_release(backend):
    observed = observe_result(await desktop.desktop_observe.ainvoke({}))
    down = observe_result(await desktop.key_down.ainvoke({"key": "shift", "observation_id": observed["observation_id"]}))
    assert down["ok"] and down["pressed"]
    released = observe_result(await desktop.key_up.ainvoke({"key": "shift", "observation_id": down["observation_id"]}))
    assert released["ok"] and not released["pressed"]

    observed = observe_result(await desktop.desktop_observe.ainvoke({}))
    held = observe_result(await desktop.hold.ainvoke({
        "key": "ctrl", "duration": 0, "observation_id": observed["observation_id"],
    }))
    assert held["ok"] and held["verified"]
    assert backend.keys == [("down", "shift"), ("up", "shift"), ("down", "ctrl"), ("up", "ctrl")]


@pytest.mark.asyncio
async def test_drag_cancels_sensitive_drop_without_approval(monkeypatch, backend):
    from jarvis.security import gate
    from jarvis.security.permissions import use_surface

    observed = observe_result(await desktop.desktop_observe.ainvoke({}))
    broker = gate.ApprovalBroker(owner_id="desktop-test-owner", ttl_s=0.2)
    monkeypatch.setattr(gate, "BROKER", broker)
    gate.register_approval_handler("cli_owner", lambda _request: False)
    try:
        with use_surface("cli_owner"):
            result = observe_result(await desktop.desktop_drag.ainvoke({
                "x1": 10, "y1": 10, "x2": 80, "y2": 40,
                "observation_id": observed["observation_id"], "action_summary": "delete this file",
            }))
        assert not result["ok"] and "cancelled" in result["error"].lower()
        assert backend.drags == []
    finally:
        gate.register_approval_handler("cli_owner", None)
