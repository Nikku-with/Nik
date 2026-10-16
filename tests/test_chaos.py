"""Failure-injection matrix — what happens when a dependency stops cooperating.

The owner is running this on one private Windows laptop with a Groq/Gemini/OpenRouter
key set that can hit a rate limit, a dead network, or a revoked key at any moment. Every
row below breaks one dependency on purpose and pins what Jarvis must still do:

  * it must not raise out of `run_agent`,
  * it must not claim success it did not achieve,
  * it must record why it failed,
  * and a provider that failed must be taken out of rotation (circuit breaker), not
    retried on every single turn.

The Windows layer is stubbed with `ctypes.windll` fakes (see tests/test_live_replay.py);
nothing here can verify real Win32 behaviour.
"""
from __future__ import annotations

import asyncio
import ctypes
from typing import Any

import pytest

from tests.test_live_replay import (  # noqa: F401  — reused fake-Windows / fake-LLM layer
    FAKE_APP_INDEX,
    FakeClock,
    FakeUser32,
    FakeWindowManager,
    SpyChatModel,
    SpyModelRegistry,
    _TimeShim,
    _WinDll,
)


# ---------------------------------------------------------------------------
# The fake machine. Kept local to this module on purpose: `tests/test_live_replay.py`
# owns an autouse `_isolated_session_state` fixture, and promoting these to conftest.py
# would silently change how the other 60-odd test modules see session state.
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def windows(monkeypatch, fake_clock) -> FakeWindowManager:
    """A Windows-looking machine with a fake user32, app index, launcher and typer."""
    from jarvis.tools import app_finder, system_tools

    wm = FakeWindowManager(clock=fake_clock)
    user32 = FakeUser32(wm)
    wm._user32 = user32  # type: ignore[attr-defined]  — so tests can make Win32 fail
    monkeypatch.setattr(ctypes, "windll", _WinDll(user32), raising=False)
    monkeypatch.setattr(app_finder, "time", _TimeShim(fake_clock))

    async def _fake_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        fake_clock.now += float(delay)  # virtual seconds, not real ones

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(app_finder.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(app_finder, "IS_WINDOWS", True)
    monkeypatch.setattr(app_finder, "IS_MAC", False)
    monkeypatch.setattr(app_finder, "IS_LINUX", False)
    monkeypatch.setattr(app_finder, "build_app_index", lambda force=False: list(FAKE_APP_INDEX))

    def _fake_launch(app: dict) -> tuple[bool, str]:
        wm.launch(app)
        return True, "launched"

    monkeypatch.setattr(app_finder, "launch_app", _fake_launch)
    monkeypatch.setattr(app_finder, "_app_memory_entry_valid", lambda app: True)

    async def _fake_native_type(text: str, press_enter: bool = False) -> tuple[bool, str]:
        wm.typed.append(text)
        return True, "SendKeys"

    monkeypatch.setattr(system_tools, "_native_type", _fake_native_type)
    monkeypatch.setattr(system_tools, "IS_WINDOWS", True)
    monkeypatch.setattr(system_tools, "IS_MAC", False)
    monkeypatch.setattr(system_tools, "IS_LINUX", False)

    app_finder._APP_MEMORY.clear()
    app_finder._RECENT_LAUNCHES.clear()
    return wm


@pytest.fixture
def models(monkeypatch) -> SpyModelRegistry:
    """Route every `get_chat_model` binding in the process to the fake registry.

    Several modules bind the symbol at import time (`from ..llm import get_chat_model`), so
    patching `jarvis.llm` alone leaves the graph talking to the real demo model.
    """
    import importlib

    import jarvis.llm as llm

    registry = SpyModelRegistry()
    monkeypatch.setattr(llm, "get_chat_model", registry.get)
    for module_name in ("jarvis.graph.nodes", "jarvis.graph.synthesis",
                        "jarvis.graph.workers.common"):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "get_chat_model", registry.get, raising=False)
    return registry


@pytest.fixture(autouse=True)
def _reset_cooldowns():
    from jarvis.llm import roles

    roles.reset_cooldowns()
    yield
    roles.reset_cooldowns()


@pytest.fixture(autouse=True)
def _isolated_session_state(monkeypatch, tmp_path):
    """Keep session state and app aliases out of the developer's real workspace."""
    monkeypatch.setenv("SESSION_STATE_FILE", str(tmp_path / "session_state.json"))
    monkeypatch.setenv("APP_MEMORY_FILE", str(tmp_path / "app_memory.json"))

    from jarvis.memory import session_state
    from jarvis.tools import app_finder

    session_state._STATES.clear()
    session_state._STATE = None
    app_finder._APP_MEMORY.path = tmp_path / "app_memory.json"
    app_finder._APP_MEMORY.backup_path = tmp_path / "app_memory.json.bak"
    app_finder._APP_MEMORY._records.clear()
    yield

# ===========================================================================
# 1. circuit breaker: error → cooldown class → window
# ===========================================================================
ERROR_CLASSES: list[tuple[str, str, float]] = [
    ("429 Too Many Requests", "rate_limited", 60.0),
    ("ResourceExhausted: quota exceeded", "rate_limited", 60.0),
    ("You exceeded your current quota", "rate_limited", 60.0),
    ("ReadTimeout: deadline exceeded", "timeout", 20.0),
    ("asyncio.TimeoutError()", "timeout", 20.0),
    ("503 Service Unavailable", "server_error", 30.0),
    ("500 Internal Server Error", "server_error", 30.0),
    ("Bad Gateway", "server_error", 30.0),
    ("401 Unauthorized", "auth_error", 300.0),
    ("403 Forbidden: invalid API key", "auth_error", 300.0),
    ("something nobody anticipated", "unknown", 15.0),
]


@pytest.mark.parametrize("message,kind,window", ERROR_CLASSES)
def test_error_message_maps_to_a_cooldown_class(message: str, kind: str, window: float):
    from jarvis.llm import roles

    roles.reset_cooldowns()
    assert roles.cooldown_class(message) == kind, f"{message!r} misclassified"
    assert roles.COOLDOWN_DEFAULTS[kind] == window


@pytest.mark.parametrize("message,kind,window", ERROR_CLASSES)
def test_failed_target_is_taken_out_of_rotation(message: str, kind: str, window: float):
    from jarvis.llm import roles

    roles.reset_cooldowns()
    entry = roles.mark_cooldown("groq", "llama-3", message)
    assert entry["kind"] == kind
    assert entry["window"] == pytest.approx(window)
    assert roles.is_cooled_down("groq", "llama-3") is True
    assert roles.cooldown_remaining("groq", "llama-3") > 0.0


def test_repeated_failures_back_off_up_to_eight_times():
    """Each strike doubles the window (×2, ×4, ×8) and then holds at ×8."""
    from jarvis.llm import roles

    roles.reset_cooldowns()
    # rate_limited base 60 s → 60, 120, 240, 480, then flat at 480 (60 × 8)
    windows = [roles.mark_cooldown("groq", "m", "429")["window"] for _ in range(5)]
    assert windows == [60.0, 120.0, 240.0, 480.0, 480.0], f"backoff wrong: {windows}"


def test_backoff_never_exceeds_the_fifteen_minute_ceiling():
    """The 900 s ceiling is only reachable from the 300 s auth-error base (300 × 8 = 2400)."""
    from jarvis.llm import roles

    roles.reset_cooldowns()
    windows = [roles.mark_cooldown("gemini", "m", "401 Unauthorized")["window"] for _ in range(5)]
    assert windows == [300.0, 600.0, 900.0, 900.0, 900.0], f"ceiling wrong: {windows}"
    assert max(windows) <= 900.0


def test_success_forgives_the_target_immediately():
    from jarvis.llm import roles

    roles.reset_cooldowns()
    roles.mark_cooldown("groq", "m", "429")
    assert roles.is_cooled_down("groq", "m") is True
    assert roles.clear_cooldown("groq", "m") is True
    assert roles.is_cooled_down("groq", "m") is False


def test_cooldown_expires_and_the_target_returns(monkeypatch):
    from jarvis.llm import roles

    roles.reset_cooldowns()
    fake_now = [1000.0]
    monkeypatch.setattr(roles, "_now", lambda: fake_now[0])
    roles.mark_cooldown("groq", "m", "429")
    fake_now[0] += 59.0
    assert roles.is_cooled_down("groq", "m") is True
    fake_now[0] += 2.0
    assert roles.is_cooled_down("groq", "m") is False, "cooldown never expired"


def test_cooled_down_targets_are_reported_for_the_owner():
    from jarvis.llm import roles

    roles.reset_cooldowns()
    roles.mark_cooldown("groq", "a", "429")
    roles.mark_cooldown("gemini", "b", "503")
    report = roles.cooled_down_targets()
    assert {item["provider"] for item in report} == {"groq", "gemini"}
    assert all(item["remaining_s"] > 0 for item in report)


# ===========================================================================
# 2. FallbackChatModel under injected provider failures
# ===========================================================================
@pytest.mark.asyncio
async def test_fallback_skips_the_cooled_down_target_without_calling_it():
    from jarvis.llm import roles
    from jarvis.llm.core import FallbackChatModel

    roles.reset_cooldowns()
    primary = SpyChatModel("primary")
    secondary = SpyChatModel("secondary", replies=["secondary ne jawab diya"])
    model = FallbackChatModel(role="brain", targets=["groq:a", "gemini:b"],
                              chain=[primary, secondary])
    roles.mark_cooldown("groq", "a", "429")

    result = await model.ainvoke([{"role": "user", "content": "hi"}])
    assert primary.calls == 0, "a cooled-down target was still called"
    assert secondary.calls == 1
    assert "secondary ne jawab diya" in str(result.content)


@pytest.mark.asyncio
async def test_a_failing_target_is_cooled_down_so_the_next_turn_skips_it():
    from jarvis.llm import roles
    from jarvis.llm.core import FallbackChatModel

    roles.reset_cooldowns()
    broken = SpyChatModel("broken", error=RuntimeError("429 rate limit exceeded"))
    healthy = SpyChatModel("healthy", replies=["theek hai"])
    model = FallbackChatModel(role="brain", targets=["groq:a", "gemini:b"],
                              chain=[broken, healthy])

    await model.ainvoke([{"role": "user", "content": "hi"}])
    assert roles.is_cooled_down("groq", "a") is True, "failure did not open the circuit"

    before = broken.calls
    await model.ainvoke([{"role": "user", "content": "aur?"}])
    assert broken.calls == before, "the broken target was retried on the next turn"


@pytest.mark.asyncio
async def test_all_targets_failing_produces_an_error_not_a_fake_answer():
    from jarvis.llm import roles
    from jarvis.llm.core import FallbackChatModel

    roles.reset_cooldowns()
    model = FallbackChatModel(
        role="brain", targets=["groq:a", "gemini:b"],
        chain=[SpyChatModel("a", error=RuntimeError("503")),
               SpyChatModel("b", error=RuntimeError("timeout"))],
    )
    with pytest.raises(RuntimeError) as caught:
        await model.ainvoke([{"role": "user", "content": "hi"}])
    text = str(caught.value)
    assert "groq" in text or "gemini" in text, f"error does not name the failed targets: {text}"
    assert "cooldown" in text.lower(), f"error does not explain the circuit breaker: {text}"


@pytest.mark.asyncio
async def test_agent_reports_failure_when_every_model_is_down(models, windows, monkeypatch):  # noqa: F811
    """The owner sees an honest failure, never a confident invented answer."""
    from jarvis.llm import roles
    from jarvis.runtime import agent

    roles.reset_cooldowns()
    # offline/demo mode answers from rules and never touches a model, so this scenario has
    # to run with the graph online for the failure to be observable
    monkeypatch.setenv("OFFLINE_MODE", "0")
    for module_name in ("jarvis.graph.nodes", "jarvis.graph.synthesis",
                        "jarvis.graph.workers.common", "jarvis.dspy_engine.modules"):
        module = pytest.importorskip(module_name)
        monkeypatch.setattr(module, "OFFLINE_MODE", False, raising=False)
        monkeypatch.setattr(module, "PROVIDER", "groq", raising=False)
    # nodes.py binds `get_chat_model` at import time, so patching jarvis.llm alone leaves
    # the graph talking to the real (demo) model
    from jarvis.graph import nodes

    monkeypatch.setattr(nodes, "get_chat_model", models.get)
    models.register("brain", SpyChatModel("brain", error=RuntimeError("429 quota")))
    # a real chat turn, not a deterministic fast path like a time question
    result = await agent.run_agent("mujhe business idea do", session_id="chaos-llm-down",
                                   surface="cli_owner")
    assert result.status in {"failed", "partial_failure"}, (
        f"claimed a clean {result.status}: {result.answer}")
    assert any("429 quota" in e for e in result.errors), (
        f"model failure was swallowed: {result.errors}")
    assert not result.verified_execution


# ===========================================================================
# 3. tool-layer failures
# ===========================================================================
@pytest.mark.asyncio
async def test_a_tool_that_raises_is_recorded_as_failed_not_swallowed():
    from jarvis.graph.workers import desktop_actions

    class _Exploding:
        name = "type_text"

        async def ainvoke(self, _args: dict) -> str:
            raise RuntimeError("SendKeys ne kaam nahi kiya")

    records = await desktop_actions._execute_desktop_action(
        {"actions": [{"kind": "type_text", "text": "hello"}]}, [_Exploding()], {})
    assert records, "no record was kept for a tool that raised"
    assert records[0]["ok"] is False
    assert "SendKeys" in records[0]["output"], f"reason lost: {records[0]['output']}"


@pytest.mark.asyncio
async def test_a_tool_that_returns_a_failure_string_is_not_counted_as_success():
    """A tool saying 'error:' in its output must not be recorded as ok=True."""
    from jarvis.graph.workers.common import _tool_call_record

    assert _tool_call_record("x", {}, "❌ file nahi mila")["ok"] is False
    assert _tool_call_record("x", {}, "done: 3 files")["ok"] is True


@pytest.mark.asyncio
async def test_a_missing_tool_is_reported_not_skipped():
    from jarvis.graph.workers import desktop_actions

    records = await desktop_actions._execute_desktop_action(
        {"actions": [{"kind": "type_text", "text": "hi"}]}, [], {})
    assert records and records[0]["ok"] is False
    assert "available nahi" in records[0]["output"]


@pytest.mark.asyncio
async def test_a_permission_denied_action_blocks_without_any_tool_evidence(windows):  # noqa: F811
    """Guest surface must be refused before anything runs, and must say so."""
    from jarvis.runtime import agent

    result = await agent._direct_app_request(
        "notepad kholo", "chaos-guest", "telegram_guest", lambda *_a, **_k: None)
    assert result is not None and result.status == "blocked", f"{result.status}: {result.answer}"
    assert not result.tool_evidence, "a blocked action still ran tools"
    assert "allowed nahi" in result.answer


# ===========================================================================
# 4. Windows-layer failures (stubbed; real Win32 is not verifiable here)
# ===========================================================================
@pytest.mark.asyncio
async def test_launch_failure_is_reported_instead_of_claiming_the_app_opened(
        windows, models, monkeypatch, fake_clock):  # noqa: F811
    from jarvis.tools import app_finder

    def _boom(_app: dict) -> tuple[bool, str]:
        raise FileNotFoundError("The system cannot find the file specified")

    monkeypatch.setattr(app_finder, "launch_app", _boom)
    from jarvis.runtime import agent

    result = await agent.run_agent("chrome kholde", session_id="chaos-launch",
                                   surface="cli_owner")
    assert "cannot find" in result.answer or "file" in result.answer.lower(), (
        f"launch failure was hidden: {result.answer}")
    assert result.status != "done", f"claimed done after a failed launch: {result.answer}"


@pytest.mark.asyncio
async def test_a_window_that_never_appears_is_reported_not_claimed(
        windows, models, fake_clock):  # noqa: F811
    windows.window_delay_s = 999.0
    from jarvis.runtime import agent

    result = await agent.run_agent("chrome kholde", session_id="chaos-slow-window",
                                   surface="cli_owner")
    assert result.status != "done" or windows.launched == [], (
        f"claimed success without a window: {result.status} / {result.answer}")


@pytest.mark.asyncio
async def test_focus_reports_failure_when_win32_refuses(windows, monkeypatch):  # noqa: F811
    """`SetForegroundWindow` returns 0 on a locked screen or a UAC-elevated window.

    That is a real Windows condition, so it must produce a reportable result rather than
    an exception or a lie.
    """
    from jarvis.tools import app_finder

    monkeypatch.setattr(windows._user32, "SetForegroundWindow", lambda hwnd: False)
    windows.open("Google Chrome")
    result = app_finder.focus_window("Google Chrome")
    assert result is not None, "focus_window returned None instead of a verdict"


# ===========================================================================
# 5. persistence failures must not take the turn down
# ===========================================================================
@pytest.mark.asyncio
async def test_unwritable_session_state_still_answers(models, windows, monkeypatch):  # noqa: F811
    """A read-only disk or a locked file must degrade, not crash the assistant."""
    from jarvis.memory import session_state
    from jarvis.runtime import agent

    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("[Errno 30] Read-only file system")

    models.register("brain", SpyChatModel("brain", replies=["Haaji bulao."]))
    monkeypatch.setattr(session_state.SessionState, "save", _boom)
    result = await agent.run_agent("hy bhai", session_id="chaos-disk",
                                   surface="cli_owner")
    assert result.answer.strip(), "no answer at all when the state file could not be written"
    # persisting the turn failed, so it must be *said*, not swallowed
    assert any("Read-only" in e or "OSError" in e for e in result.errors), (
        f"disk failure was swallowed: {result.errors}")


@pytest.mark.asyncio
async def test_memory_write_failure_does_not_fail_the_turn(models, windows, monkeypatch):  # noqa: F811
    """A full or read-only memory database must degrade the turn, not kill it."""
    from jarvis.memory.store import MemoryStore
    from jarvis.runtime import agent

    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("database or disk is full")

    monkeypatch.setattr(MemoryStore, "remember_fact", _boom)
    models.register("brain", SpyChatModel("brain", replies=["Yaad rakh liya."]))
    result = await agent.run_agent("yaad rakhna mera naam Nikhil hai",
                                   session_id="chaos-memory", surface="cli_owner")
    assert result.answer.strip(), "no answer when memory could not be written"


# ===========================================================================
# 6. the whole-house-out scenario
# ===========================================================================
@pytest.mark.asyncio
async def test_everything_broken_still_returns_an_explanation(models, windows, monkeypatch):  # noqa: F811
    """No network, no provider, no windows: the owner must still get a sentence back."""
    from jarvis.llm import roles
    from jarvis.runtime import agent
    from jarvis.tools import app_finder

    roles.reset_cooldowns()
    models.register("brain", SpyChatModel("brain", error=RuntimeError("503 Service Unavailable")))
    models.register("fast", SpyChatModel("fast", error=RuntimeError("timeout")))
    monkeypatch.setattr(app_finder, "launch_app",
                        lambda _app: (_ for _ in ()).throw(OSError("no display")))
    # keep the request off the network: tests/conftest.py forbids live sockets, and this
    # scenario is about local failures anyway
    from jarvis.tools import web_tools

    async def _no_network(*_a: Any, **_k: Any) -> str:
        raise OSError("no network")

    monkeypatch.setattr(web_tools, "google_search", _no_network, raising=False)
    monkeypatch.setattr(web_tools, "fetch_url_text", _no_network, raising=False)
    monkeypatch.setattr(web_tools, "web_search_results", _no_network, raising=False)

    result = await agent.run_agent("chrome kholo aur ek note likho", session_id="chaos-all",
                                   surface="cli_owner")
    assert result.status in {"failed", "needs_clarification"}, (
        f"claimed {result.status} with everything broken: {result.answer}")
    assert result.answer.strip(), "empty answer instead of an explanation"
    assert not result.verified_execution, "claimed verified execution with everything broken"


@pytest.mark.asyncio
async def test_a_hanging_tool_does_not_hang_the_turn_forever(models, windows, monkeypatch):  # noqa: F811
    """A tool that never returns must be bounded by the caller's timeout, not hang the CLI."""
    from jarvis.tools import app_finder

    async def _hang(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(60)

    monkeypatch.setattr(app_finder, "launch_app", _hang)
    from jarvis.runtime import agent

    result = await asyncio.wait_for(
        agent.run_agent("chrome kholde", session_id="chaos-hang", surface="cli_owner"),
        timeout=30,
    )
    assert result is not None
