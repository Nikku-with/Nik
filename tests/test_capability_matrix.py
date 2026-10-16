from __future__ import annotations

import sys
from types import ModuleType

import pytest

SURFACES = ("cli_owner", "api", "telegram_owner", "telegram_guest", "voice_owner", "automation")
REQUESTS = ("namaste", "What day is it today?")


class _NoopState:
    def add_task(self, *_args, **_kwargs):
        return "task"


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("request_text", REQUESTS)
@pytest.mark.asyncio
async def test_shared_runner_returns_answer_and_terminal_progress_on_every_surface(
    monkeypatch, surface: str, request_text: str,
):
    from jarvis.runtime import agent
    from jarvis.runtime.progress import use_sink
    from tests.support import CollectingSink

    monkeypatch.setattr(agent, "get_state", lambda: _NoopState())
    monkeypatch.setattr(agent, "_post_run", lambda *_args, **_kwargs: None)
    sink = CollectingSink()
    with use_sink(sink):
        result = await agent.run_agent(
            request_text, session_id=f"matrix:{surface}", surface=surface,
            memory_user_id=f"matrix-owner:{surface}", use_fast_path=True,
        )

    assert result.answer.strip(), (surface, request_text, result.as_dict())
    assert result.status in {"chat", "time"}
    assert not result.errors
    assert any(event.kind == "start" for event in sink.events)
    assert any(event.kind == "done" and event.status == "ok" for event in sink.events)


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.asyncio
async def test_registered_safe_datetime_tool_works_on_every_surface(monkeypatch, surface: str):
    from jarvis.security.permissions import check, use_surface
    from jarvis.tools import web_tools

    monkeypatch.setattr(web_tools, "TIMEZONE", "Asia/Kolkata")
    assert check("get_current_datetime", surface).allowed
    with use_surface(surface):
        result = await web_tools.get_current_datetime.ainvoke({})
    assert str(result).strip() and ("IST" in str(result) or "Asia/Kolkata" in str(result))


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.asyncio
async def test_dangerous_action_without_owner_approval_fails_friendly_on_every_surface(
    monkeypatch, surface: str,
):
    from jarvis.security import gate
    from jarvis.security.permissions import use_surface

    monkeypatch.setattr(gate, "BROKER", gate.ApprovalBroker(owner_id="matrix-owner", ttl_s=0.2))
    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    with gate.use_approval_handler(lambda _request: False), use_surface(surface):
        result = await gate.authorize_command("rm -rf /tmp/matrix", "/tmp", surface=surface)
    assert not result.allowed
    assert any(word in result.reason.lower() for word in ("cancel", "denied", "timeout", "timed out"))


# A small registry contract used by the status document; each surface must appear once.
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.asyncio
async def test_desktop_open_app_is_verified_or_friendly_blocked_on_each_surface(monkeypatch, surface: str):
    from jarvis.graph import nodes
    from jarvis.runtime import agent

    async def fake_execute(_action, _tools):
        return [{"name": "open_any_app", "ok": True, "output": "fake app launch verified"}]

    monkeypatch.setattr(nodes, "_execute_desktop_action", fake_execute)
    result = await agent._direct_app_request("notepad kholo", f"matrix:{surface}", surface, lambda *_a, **_k: None)

    if surface == "telegram_guest":
        assert result is not None and result.status == "blocked"
        assert "allowed nahi" in result.answer and not result.tool_evidence
    else:
        assert result is not None and result.status == "done"
        assert result.verified_execution is True
        assert "verified" in result.answer


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.asyncio
async def test_browser_delegated_tool_succeeds_or_returns_friendly_policy_error(monkeypatch, surface: str):
    import jarvis.tools as registry
    from jarvis.security.permissions import use_surface
    from jarvis.tools import browser_tools

    class FakeTool:
        name = "run_shell_command"

        def __init__(self):
            self.calls = []

        async def ainvoke(self, payload):
            self.calls.append(payload)
            return f"fake result: {payload['command']}"

    class FakeActionRegistry:
        def __init__(self):
            self.actions = {}

        def action(self, description):
            def register(function):
                self.actions[function.__name__] = (description, function)
                return function
            return register

    class FakeBrowserTools:
        def __init__(self):
            self.registry = FakeActionRegistry()

    fake_browser = ModuleType("browser_use")
    fake_browser.Tools = FakeBrowserTools
    monkeypatch.setitem(sys.modules, "browser_use", fake_browser)
    delegated = FakeTool()
    monkeypatch.setattr(registry, "ALL_TOOLS", [delegated])
    browser_registry = browser_tools._build_browser_tools()
    request_tool = browser_registry.registry.actions["jarvis_request_tool"][1]
    with use_surface(surface):
        result = await request_tool("run_shell_command", '{"command":"echo safe"}')

    if surface in {"telegram_guest", "api"}:
        assert "Permission blocked" in result
        assert not delegated.calls
    else:
        assert "fake result: echo safe" in result
        assert delegated.calls == [{"command": "echo safe"}]


@pytest.mark.parametrize("surface", ("api", "voice_owner", "automation"))
def test_owner_desktop_input_permissions_apply_on_all_execution_surfaces(surface: str):
    from jarvis.security.permissions import check

    for tool_name in ("open_any_app", "type_text", "press_hotkey", "desktop_drag", "click_text",
                      "wait_for_text", "find_image", "describe_screen", "key_down", "key_up", "hold"):
        decision = check(tool_name, surface)
        assert decision.allowed, (surface, tool_name, decision.reason)
        assert decision.level == "SYSTEM"
    assert not check("open_any_app", "telegram_guest").allowed


# A small registry contract used by the status document; each surface must appear once.
def capability_matrix() -> dict[str, tuple[str, ...]]:
    return {surface: REQUESTS for surface in SURFACES}
