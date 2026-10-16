from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis.tools import app_finder


def _app(name: str, path: Path, *, kind: str = "exe", score: int = 100) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    return {"name": name, "path": str(path), "kind": kind, "score": score}


def _memory(monkeypatch, tmp_path: Path) -> app_finder.AppLaunchMemory:
    memory = app_finder.AppLaunchMemory(tmp_path / "app-launch.json")
    monkeypatch.setattr(app_finder, "_APP_MEMORY", memory)
    monkeypatch.setattr(app_finder, "_RECENT_LAUNCHES", {})
    return memory


@pytest.mark.asyncio
async def test_first_open_verifies_launch_before_saving_and_next_open_uses_memory(monkeypatch, tmp_path):
    memory = _memory(monkeypatch, tmp_path)
    chrome = _app("Google Chrome", tmp_path / "bin" / "chrome.exe")
    calls = {"search": 0, "launch": 0, "focus": 0}

    def search(query: str, limit: int = 5):
        calls["search"] += 1
        assert query == "chrome"
        return [chrome]

    async def focus(_name: str) -> bool:
        calls["focus"] += 1
        return calls["focus"] >= 2

    def launch(_app: dict) -> tuple[bool, str]:
        calls["launch"] += 1
        return True, "started"

    monkeypatch.setattr(app_finder, "find_apps", search)
    monkeypatch.setattr(app_finder, "launch_app", launch)
    monkeypatch.setattr(app_finder, "focus_window", focus)
    monkeypatch.setattr(app_finder, "window_exists", lambda _name: _false_async())

    first = await app_finder.open_any_app.ainvoke({"query": "chrome"})
    assert "active window verify hui" in first
    assert calls == {"search": 1, "launch": 1, "focus": 2}
    assert memory.lookup("chrome")["path"] == chrome["path"]
    assert json.loads(memory.path.read_text(encoding="utf-8"))["apps"]

    monkeypatch.setattr(app_finder, "find_apps", lambda *_args, **_kwargs: pytest.fail("index searched despite cache hit"))
    calls["focus"] = 0
    monkeypatch.setattr(app_finder, "focus_window", lambda _name: _true_async())
    second = await app_finder.open_any_app.ainvoke({"query": "Google Chrome"})
    assert "saved app mapping" in second
    assert calls["launch"] == 1


@pytest.mark.asyncio
async def test_stale_app_mapping_is_removed_then_relearned_from_fresh_search(monkeypatch, tmp_path):
    memory = _memory(monkeypatch, tmp_path)
    stale = {"name": "Old Editor", "path": str(tmp_path / "missing-editor"), "kind": "exe"}
    assert memory.remember("my editor", stale)
    fresh = _app("My Editor", tmp_path / "new-editor")
    events: list[str] = []

    def search(query: str, limit: int = 5):
        events.append("search")
        assert query == "my editor"
        return [fresh]

    focus_calls = 0

    async def focus(_name: str) -> bool:
        nonlocal focus_calls
        focus_calls += 1
        return focus_calls >= 2

    monkeypatch.setattr(app_finder, "find_apps", search)
    monkeypatch.setattr(app_finder, "launch_app", lambda _app: (events.append("launch") is None, "started"))
    monkeypatch.setattr(app_finder, "focus_window", focus)
    monkeypatch.setattr(app_finder, "window_exists", lambda _name: _false_async())

    result = await app_finder.open_any_app.ainvoke({"query": "my editor"})
    assert "active window verify hui" in result
    assert events == ["search", "launch"]
    learned = memory.lookup("my editor")
    assert learned["path"] == fresh["path"]
    assert learned["name"] == "My Editor"
    assert not Path(stale["path"]).exists()


@pytest.mark.asyncio
async def test_failed_or_unverified_launch_never_saves_mapping(monkeypatch, tmp_path):
    memory = _memory(monkeypatch, tmp_path)
    app = _app("Notepad", tmp_path / "notepad.exe")
    monkeypatch.setattr(app_finder, "find_apps", lambda *_args, **_kwargs: [app])
    monkeypatch.setattr(app_finder, "focus_window", lambda _name: _false_async())
    monkeypatch.setattr(app_finder, "window_exists", lambda _name: _false_async())
    monkeypatch.setattr(app_finder, "_claim_launch", lambda _name: True)
    monkeypatch.setattr(app_finder, "launch_app", lambda _app: (False, "OS launch error"))

    result = await app_finder.open_any_app.ainvoke({"query": "notepad"})
    assert "launch nahi hua" in result
    assert memory.lookup("notepad") is None
    assert memory.list() == []

    monkeypatch.setattr(app_finder, "launch_app", lambda _app: (True, "started"))
    monkeypatch.setattr(app_finder, "_verify_active_window", lambda *_args: _false_async())
    # Avoid the initial focus/window check using explicit async fakes.
    result = await app_finder.open_any_app.ainvoke({"query": "notepad"})
    assert "verify nahi hui" in result
    assert memory.lookup("notepad") is None


@pytest.mark.asyncio
async def test_app_memory_management_tool_lists_forgets_and_clears_only_app_mappings(monkeypatch, tmp_path):
    memory = _memory(monkeypatch, tmp_path)
    browser = _app("Firefox", tmp_path / "firefox")
    editor = _app("Visual Studio Code", tmp_path / "code")
    assert memory.remember("browser", browser)
    assert memory.remember("vs code", editor)
    assert memory.lookup("vscode")["path"] == editor["path"]

    listed = await app_finder.manage_apps.ainvoke({"action": "list"})
    assert "Firefox" in listed and "Visual Studio Code" in listed
    forgotten = await app_finder.manage_apps.ainvoke({"action": "forget", "query": "browser"})
    assert "Mapping removed" in forgotten
    assert memory.lookup("browser") is None
    assert memory.lookup("vscode") is not None
    cleared = await app_finder.manage_apps.ainvoke({"action": "clear"})
    assert "cleared" in cleared
    assert memory.list() == []


@pytest.mark.asyncio
async def test_app_memory_is_atomic_reloadable_and_restores_backup(monkeypatch, tmp_path):
    memory = _memory(monkeypatch, tmp_path)
    first = _app("First", tmp_path / "first")
    second = _app("Second", tmp_path / "second")
    assert memory.remember("first", first)
    assert memory.remember("second", second)
    assert memory.backup_path.exists()
    assert not list(tmp_path.glob(".app-launch.json.*"))

    reloaded = app_finder.AppLaunchMemory(memory.path)
    assert {item["name"] for item in reloaded.list()} == {"First", "Second"}

    memory.path.write_text("not valid json", encoding="utf-8")
    restored = app_finder.AppLaunchMemory(memory.path)
    assert {item["name"] for item in restored.list()} == {"First"}
    assert "restored app mapping from backup" in restored.load_error


async def _false_async() -> bool:
    return False


async def _true_async() -> bool:
    return True


@pytest.mark.asyncio
async def test_app_memory_tools_are_registered_and_cli_apps_command_is_behavioral(monkeypatch):
    from jarvis.cli import app as cli_app
    from jarvis.tools import tool_names

    assert {"open_any_app", "manage_apps"}.issubset(tool_names())
    seen = []

    class FakeManageApps:
        async def ainvoke(self, payload):
            seen.append(payload)
            return "📱 app mappings"

    monkeypatch.setattr(app_finder, "manage_apps", FakeManageApps())
    monkeypatch.setattr(cli_app.ui, "cprint_safe", lambda *_args, **_kwargs: None)
    assert await cli_app.handle_command("/apps forget chrome", "app-test")
    assert seen == [{"action": "forget", "query": "chrome"}]


@pytest.mark.asyncio
async def test_api_apps_endpoints_list_and_manage_app_mappings(monkeypatch, tmp_path):
    import json

    from jarvis.api import server

    memory = _memory(monkeypatch, tmp_path)
    app = _app("Firefox", tmp_path / "firefox")
    assert memory.remember("browser", app)
    get_endpoint = next(route.endpoint for route in server.app.routes
                        if getattr(route, "path", "") == "/apps" and "GET" in route.methods)
    post_endpoint = next(route.endpoint for route in server.app.routes
                         if getattr(route, "path", "") == "/apps" and "POST" in route.methods)
    listed = json.loads((await get_endpoint()).body)
    assert listed["apps"][0]["name"] == "Firefox"
    result = json.loads((await post_endpoint({"action": "forget", "query": "browser"})).body)
    assert result["ok"] and "Mapping removed" in result["result"]
    assert memory.list() == []
    invalid = await post_endpoint({"action": "forget"})
    assert invalid.status_code == 400 and not json.loads(invalid.body)["ok"]


@pytest.mark.asyncio
async def test_telegram_apps_command_is_owner_only_and_uses_app_mapping_manager(monkeypatch):
    pytest.importorskip("telegram")
    from types import SimpleNamespace

    from jarvis.config import settings as settings_module
    from jarvis.telegram.bot import build_application

    monkeypatch.setattr(settings_module, "TELEGRAM_BOT_TOKEN", "123456789:" + "x" * 35)
    monkeypatch.setattr(settings_module, "TELEGRAM_OWNER_ID", "999")
    monkeypatch.setattr(settings_module, "TELEGRAM_ALLOWED_USERS", ["123"])
    monkeypatch.setattr(settings_module, "TELEGRAM_ALLOWED_CHAT_IDS", [])
    app = build_application()
    handler = next(handler for handler in app.handlers[0]
                   if handler.__class__.__name__ == "CommandHandler" and "apps" in handler.commands)
    calls = []

    class FakeManageApps:
        async def ainvoke(self, payload):
            calls.append(payload)
            return "📱 app mappings"

    monkeypatch.setattr(app_finder, "manage_apps", FakeManageApps())

    class FakeMessage:
        def __init__(self):
            self.texts = []

        async def reply_text(self, text, **_kwargs):
            self.texts.append(text)

    owner_message = FakeMessage()
    owner = SimpleNamespace(effective_user=SimpleNamespace(id=999), effective_chat=SimpleNamespace(id=777),
                            effective_message=owner_message, message=owner_message)
    await handler.callback(owner, SimpleNamespace(args=["forget", "chrome"]))
    assert calls == [{"action": "forget", "query": "chrome"}]
    assert "app mappings" in owner_message.texts[-1]

    guest_message = FakeMessage()
    guest = SimpleNamespace(effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=777),
                            effective_message=guest_message, message=guest_message)
    await handler.callback(guest, SimpleNamespace(args=["list"]))
    assert len(calls) == 1
    assert "sirf owner" in guest_message.texts[-1]
