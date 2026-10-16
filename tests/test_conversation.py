"""Conversation limits, the Rich UI, memory behaviour and the Telegram surface.

Scope note: this is a "does it hold up over a long session" file, not a Windows test.
The Win32 layer is stubbed (tests/test_live_replay.py fakes) and the Telegram network
layer is never contacted — only the pure functions (`authorize`, `html_chunks`,
`chunk_text`) and the surface mapping are exercised.
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

CASUAL_MESSAGES: list[str] = [
    "hy", "kya haal hai", "aur batao", "thik hun", "kaam chal raha hai",
    "acha", "hmm", "samajh gaya", "theek hai", "ok",
    "kal kya karna hai", "soch raha hun", "boring ho raha hai", "kuch naya batao",
    "hanji", "naah", "pakka?", "kyun?", "phir?", "aur?",
    "acha chalo", "bye", "good night", "subah milte hain", "ok done",
]


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def windows(monkeypatch, fake_clock) -> FakeWindowManager:
    from jarvis.tools import app_finder
    from jarvis.tools import system_tools

    wm = FakeWindowManager(clock=fake_clock)
    monkeypatch.setattr(ctypes, "windll", _WinDll(FakeUser32(wm)), raising=False)
    monkeypatch.setattr(app_finder, "time", _TimeShim(fake_clock))

    async def _fake_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        fake_clock.now += float(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(app_finder.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(app_finder, "IS_WINDOWS", True)
    monkeypatch.setattr(app_finder, "build_app_index", lambda force=False: list(FAKE_APP_INDEX))
    monkeypatch.setattr(app_finder, "launch_app", lambda app: (wm.launch(app), "launched")[0:1] + ("launched",))
    monkeypatch.setattr(app_finder, "_app_memory_entry_valid", lambda app: True)

    async def _fake_native_type(text: str, press_enter: bool = False) -> tuple[bool, str]:
        wm.typed.append(text)
        return True, "SendKeys"

    monkeypatch.setattr(system_tools, "_native_type", _fake_native_type)
    app_finder._APP_MEMORY.clear()
    app_finder._RECENT_LAUNCHES.clear()
    return wm


@pytest.fixture
def models(monkeypatch) -> SpyModelRegistry:
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
# 1. a long casual conversation costs one model call per turn
# ===========================================================================
@pytest.mark.asyncio
async def test_twenty_five_casual_messages_cost_one_llm_call_each(models, windows):  # noqa: F811
    """The old build re-planned and re-criticised every 'acha'; this path must not."""
    from jarvis.runtime import agent

    models.register("brain", SpyChatModel("brain"))
    for index, text in enumerate(CASUAL_MESSAGES):
        result = await agent.run_agent(text, session_id="casual", surface="cli_owner")
        assert result.status in {"chat", "failed"}, f"{text!r} → {result.status}: {result.answer}"
        assert not result.tool_evidence, f"{text!r} ran tools: {result.tool_evidence}"
    assert models.total_calls == len(CASUAL_MESSAGES), (
        f"{len(CASUAL_MESSAGES)} casual turns cost {models.total_calls} model calls")


@pytest.mark.asyncio
async def test_the_prompt_does_not_grow_unboundedly_over_a_long_session(models, windows):  # noqa: F811
    """25 turns of history must not inflate turn 26's prompt — that is how costs creep."""
    from jarvis.runtime import agent

    brain = SpyChatModel("brain")
    models.register("brain", brain)

    sizes: list[int] = []
    for text in CASUAL_MESSAGES:
        await agent.run_agent(text, session_id="casual", surface="cli_owner")
        if brain.messages:
            sizes.append(sum(len(str(getattr(m, "content", m))) for m in brain.messages))

    assert sizes, "no prompt was captured"
    assert max(sizes) < 4 * min(sizes) + 2000, (
        f"prompt grew from {min(sizes)} to {max(sizes)} chars over 25 turns")


@pytest.mark.asyncio
async def test_a_casual_turn_never_waits_on_a_slow_tool(models, windows):  # noqa: F811
    from jarvis.runtime import agent

    models.register("brain", SpyChatModel("brain", delay_s=0.0))
    result = await agent.run_agent("acha", session_id="casual", surface="cli_owner")
    assert result.status == "chat", f"{result.status}: {result.answer}"
    assert result.seconds < 5.0, f"a one-call chat turn took {result.seconds:.1f}s"


# ===========================================================================
# 2. the Rich dashboard survives real-world text
# ===========================================================================
HOSTILE_STRINGS = [
    "[Errno 30] Read-only file system",
    "[/bold]",
    "[foo]bar",
    "a [/] b",
    "[link=https://x]click[/link]",
    "90% [/] done",
    "{'rows': [1, 2, 3]}",
    "markdown [b]bold[/b] from a web page",
    "🚀 emoji with [brackets]",
    "x" * 5000,
]


@pytest.mark.parametrize("text", HOSTILE_STRINGS)
def test_dashboard_renders_any_tool_output_without_crashing(text: str):
    """Regression: `Text.from_markup` raised MarkupError on any '[' in tool output."""
    from jarvis.cli.pro_ui import Dashboard, ProgressEvent

    dashboard = Dashboard(header={"name": "NIK[/]HIL", "model": "groq[/]llama",
                                  "provider": "[demo]", "status": "DE[/]MO"})
    dashboard.plan = ["step [1] do the [thing]"]
    dashboard.tools_used = [("tool[/]name", "ok", 12)]
    dashboard.models_used = [("model[0]", "fail", 34)]
    for kind in ("start", "thinking", "tool_start", "tool_end", "model_start",
                 "model_end", "warn", "error", "done", "plan"):
        dashboard.on_event(ProgressEvent(kind=kind, text=text, tool=text[:20],
                                         status="ok", ms=7, detail=text))
    dashboard.render()  # must not raise
    dashboard._stream()
    dashboard._side()
    dashboard._header_bar()


def test_dashboard_event_history_is_bounded():
    """A 10-minute automation must not grow the in-memory event list without limit."""
    from jarvis.cli.pro_ui import Dashboard, ProgressEvent

    dashboard = Dashboard()
    for index in range(2000):
        dashboard.on_event(ProgressEvent(kind="thinking", text=f"step {index}"))
    assert len(dashboard.events) <= 400, f"{len(dashboard.events)} events retained"


def test_dashboard_renders_with_nothing_to_show():
    from jarvis.cli.pro_ui import Dashboard

    dashboard = Dashboard()
    assert dashboard.render() is not None
    assert dashboard.events_trace() == []


# ===========================================================================
# 3. memory behaviour
# ===========================================================================
@pytest.mark.asyncio
async def test_remember_then_recall_round_trips(models, windows):  # noqa: F811
    from jarvis.memory.manager import get_manager

    mgr = get_manager()
    saved = mgr.remember("Nikhil ka favourite colour blue hai")
    assert saved.get("ok"), f"remember failed: {saved}"
    hits = mgr.recall("favourite colour")
    assert hits, "a fact that was just saved could not be recalled"


@pytest.mark.asyncio
async def test_saving_the_same_fact_twice_does_not_duplicate_it(models, windows):  # noqa: F811
    from jarvis.memory.manager import get_manager

    mgr = get_manager()
    first = mgr.remember("Nikhil Jaipur mein rehta hai")
    second = mgr.remember("Nikhil Jaipur mein rehta hai")
    assert first.get("ok") and second.get("ok")
    assert second.get("action") in {"duplicate", "updated"}, (
        f"second save was treated as new: {second}")


@pytest.mark.asyncio
async def test_a_sensitive_fact_waits_for_confirmation(models, windows):  # noqa: F811
    """Bank/card details must be staged, not silently written."""
    from jarvis.memory.manager import get_manager

    mgr = get_manager()
    result = mgr.remember("mera card number 4111 1111 1111 1111 hai")
    assert result.get("ok") is False, f"sensitive fact saved without confirmation: {result}"
    assert result.get("action") == "pending_confirmation"


@pytest.mark.asyncio
async def test_guest_surface_cannot_change_memory(models, windows):  # noqa: F811
    from jarvis.security.permissions import check

    for name in ("remember_fact", "memory_compact", "forget_memory"):
        assert check(name, "telegram_guest").allowed is False, f"{name} is allowed for a guest"


# ===========================================================================
# 4. Telegram surface (pure functions only — no network)
# ===========================================================================
def test_telegram_authorize_fails_closed_when_unconfigured(monkeypatch):
    """An unconfigured bot must never promote its first random caller to owner."""
    from jarvis.config import settings as S
    from jarvis.telegram.bot import authorize

    monkeypatch.setattr(S, "TELEGRAM_OWNER_ID", "")
    monkeypatch.setattr(S, "TELEGRAM_ALLOWED_CHAT_IDS", [])
    monkeypatch.setattr(S, "TELEGRAM_ALLOWED_USERS", [])
    allowed, role = authorize(12345, 67890)
    assert allowed is False and role == "unconfigured", f"{allowed}/{role}"


def test_telegram_authorize_maps_owner_and_guest(monkeypatch):
    from jarvis.config import settings as S
    from jarvis.telegram.bot import authorize, surface_for

    monkeypatch.setattr(S, "TELEGRAM_OWNER_ID", "111")
    monkeypatch.setattr(S, "TELEGRAM_ALLOWED_USERS", ["222"])
    monkeypatch.setattr(S, "TELEGRAM_ALLOWED_CHAT_IDS", [])
    assert authorize(111, 1) == (True, "owner")
    assert surface_for("owner") == "telegram_owner"
    assert authorize(222, 1) == (True, "guest")
    assert surface_for("guest") == "telegram_guest"
    assert authorize(999, 1)[0] is False


def test_telegram_html_chunks_stay_inside_the_message_limit():
    """Telegram rejects anything over 4096 bytes; long answers must be split, not sent."""
    from jarvis.telegram.md import html_chunks

    body = ("**bold** and `code` and a [link](https://example.com) " * 400)
    chunks = html_chunks(body, limit=3900)
    assert chunks, "no chunks produced"
    assert all(len(chunk) <= 3900 for chunk in chunks), (
        f"chunk over the limit: {[len(c) for c in chunks]}")
    assert "".join(chunks).count("bold") >= 400 - 20, "content was lost while chunking"


def test_telegram_html_escapes_angle_brackets():
    """Unescaped '<' in a reply makes Telegram reject the whole message."""
    from jarvis.telegram.md import md_to_html

    rendered = md_to_html("use <script>alert(1)</script> carefully")
    assert "<script>" not in rendered, f"raw HTML survived: {rendered}"
    assert "&lt;script&gt;" in rendered


def test_telegram_chunking_splits_on_a_safe_boundary():
    from jarvis.telegram.md import chunk_text

    chunks = chunk_text("\n\n".join(f"para {i}" for i in range(500)), limit=500)
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
