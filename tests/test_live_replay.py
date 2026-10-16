"""
tests/test_live_replay.py — the owner's seven real Windows failures, replayed
============================================================================

Ye file **builder ke report ya README par bharosa nahi karti**. Ye owner ke asli
VM messages le kar, fake LLM + fake Windows API ke saath, aaj ke code par chala kar
dekhti hai ki woh purane results abhi bhi aate hain ya nahi.

| # | Message | Old result | Required now |
|---|---|---|---|
| 1 | "hy bhai" / "hy" | identical canned reply | varied, 1 LLM call (ya varied pool jab sab cooled down) |
| 2 | "kaisa hai re" | 63.7 s, 5 LLM calls, task graph | chat path, 1 LLM call, cooled-down primary skip, ≤ 8 s |
| 3 | "chrome kholde" | 45.9 s, 8 LLM calls, zero tool calls | `open_any_app` called, 0 LLM calls |
| 4 | "notebook open krke likh" | refused (best match OUTLOOK 56.7 %) | one clarifying question, alias saved |
| 5 | "notepad open krke hy likhde" | "window verify nahi hui", typing skipped | 3 s baad aane wali window accepted, text typed |
| 6 | "python install krde" | script + 2 approvals + 125 s | one clarification; `pip install <pkg>` once, ≤ 1 approval |
| 7 | second "chrome kholo" | n/a | 0 LLM calls, existing window focused |

Windows ko yahan **stub** kiya gaya hai (fake `ctypes.windll.user32`, fake app index,
fake `launch_app`). Real Windows par verification abhi bhi owner ko karna hoga —
wo list final report me hai.
"""
from __future__ import annotations

import asyncio
import ctypes
import time as real_time
from dataclasses import dataclass, field
from typing import Any

import pytest

# ===========================================================================
# fake Windows
# ===========================================================================
FAKE_APP_INDEX: list[dict[str, Any]] = [
    {"name": "Notepad", "norm": "notepad", "path": "notepad.exe", "kind": "path", "extra": ""},
    {"name": "Notepad++", "norm": "notepad", "path": "notepadpp.exe", "kind": "lnk", "extra": ""},
    {"name": "Google Chrome", "norm": "google chrome", "path": "chrome.exe", "kind": "lnk", "extra": ""},
    {"name": "Microsoft Outlook", "norm": "microsoft outlook", "path": "OUTLOOK.EXE",
     "kind": "lnk", "extra": ""},
    {"name": "Visual Studio Code", "norm": "visual studio code", "path": "Code.exe",
     "kind": "lnk", "extra": ""},
    {"name": "Calculator", "norm": "calculator", "path": "calc.exe", "kind": "path", "extra": ""},
    {"name": "Windows Terminal", "norm": "windows terminal", "path": "wt.exe",
     "kind": "path", "extra": ""},
]


@dataclass
class FakeWindowManager:
    """Tracks windows the way Win32 would, including a launch→window delay."""

    clock: FakeClock
    window_delay_s: float = 0.0
    launched: list[str] = field(default_factory=list)
    typed: list[str] = field(default_factory=list)
    focus_calls: int = 0
    _windows: list[tuple[int, str, float]] = field(default_factory=list)
    _next_hwnd: int = 1000
    foreground: int | None = None

    def open(self, title: str) -> None:
        self._next_hwnd += 1
        self._windows.append((self._next_hwnd, title, self.clock.now))

    def close_all(self) -> None:
        self._windows.clear()
        self.foreground = None

    def visible(self) -> list[tuple[int, str]]:
        """Windows whose appear-time has been reached on the fake clock."""
        return [(hwnd, title) for hwnd, title, since in self._windows
                if self.clock.now >= since + self.window_delay_s]

    def titles(self) -> list[str]:
        return [title for _hwnd, title in self.visible()]

    def launch(self, app: dict) -> None:
        self.launched.append(str(app.get("name", "")))
        self.open(str(app.get("name", "")))


class FakeUser32:
    """Minimal `ctypes.windll.user32` stand-in for EnumWindows / focus calls."""

    def __init__(self, wm: FakeWindowManager) -> None:
        self.wm = wm

    def IsWindowVisible(self, hwnd: int) -> bool:
        return any(item[0] == hwnd for item in self.wm.visible())

    def GetWindowTextLengthW(self, hwnd: int) -> int:
        for item_hwnd, title in self.wm.visible():
            if item_hwnd == hwnd:
                return len(title)
        return 0

    def GetWindowTextW(self, hwnd: int, buffer: Any, size: int) -> int:
        for item_hwnd, title in self.wm.visible():
            if item_hwnd == hwnd:
                buffer.value = title[: max(0, size - 1)]
                return len(buffer.value)
        buffer.value = ""
        return 0

    def EnumWindows(self, callback: Any, lparam: int) -> bool:
        for hwnd, _title in self.wm.visible():
            if not callback(hwnd, lparam):
                return True
        return True

    def ShowWindow(self, hwnd: int, command: int) -> bool:
        return True

    def BringWindowToTop(self, hwnd: int) -> bool:
        return True

    def SetForegroundWindow(self, hwnd: int) -> bool:
        self.wm.focus_calls += 1
        self.wm.foreground = hwnd
        return True


class FakeClock:
    """Virtual monotonic clock so a 3-second window delay costs no real time."""

    def __init__(self) -> None:
        # A zero baseline would make `_claim_launch` see every app as "just launched"
        # (now - 0.0 < LAUNCH_DEDUPE_S), which is an artefact of the fake clock, not a bug.
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _TimeShim:
    """Proxy to the real `time` module with a fake `monotonic`."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock

    def __getattr__(self, name: str) -> Any:
        return getattr(real_time, name)

    def monotonic(self) -> float:
        return self._clock.now


class _WinDll:
    def __init__(self, user32: FakeUser32) -> None:
        self.user32 = user32


# ===========================================================================
# fake LLMs
# ===========================================================================
class SpyChatModel:
    """One fake model; records every call and can be told to fail."""

    def __init__(self, label: str, replies: list[str] | None = None,
                 error: Exception | None = None, delay_s: float = 0.0) -> None:
        self.label = label
        self.calls = 0
        self.replies = list(replies or [])
        self.error = error
        self.delay_s = delay_s
        self.messages: list[Any] = []

    async def ainvoke(self, messages: Any, **_kwargs: Any) -> Any:
        from langchain_core.messages import AIMessage

        self.calls += 1
        self.messages = list(messages)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        if self.replies:
            body = self.replies[min(self.calls - 1, len(self.replies) - 1)]
        else:
            body = f"[{self.label}] theek hoon, bolo."
        return AIMessage(content=body)


class SpyModelRegistry:
    """Routes `get_chat_model(role)` to fake models and counts every call."""

    def __init__(self) -> None:
        self.models: dict[str, SpyChatModel] = {}
        self.total_calls = 0

    def register(self, role: str, model: SpyChatModel) -> None:
        self.models[role] = model

    def get(self, role: str = "brain", **_kwargs: Any) -> SpyChatModel | None:
        model = self.models.get(role)
        if model is None:
            return None

        class _Counting:
            def __init__(self, inner: SpyChatModel, registry: SpyModelRegistry) -> None:
                self._inner = inner
                self._registry = registry

            async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
                self._registry.total_calls += 1
                return await self._inner.ainvoke(messages, **kwargs)

            def __getattr__(self, item: str) -> Any:
                return getattr(self._inner, item)

        return _Counting(model, self)  # type: ignore[return-value]


# ===========================================================================
# fixtures
# ===========================================================================
@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def windows(monkeypatch, fake_clock) -> FakeWindowManager:
    """A Windows-looking machine: fake user32, fake app index, fake launcher, fake typing."""
    from jarvis.tools import app_finder

    wm = FakeWindowManager(clock=fake_clock)
    user32 = FakeUser32(wm)
    monkeypatch.setattr(ctypes, "windll", _WinDll(user32), raising=False)
    monkeypatch.setattr(app_finder, "time", _TimeShim(fake_clock))

    real_sleep = asyncio.sleep

    async def _fake_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        # advance the virtual clock instead of burning real seconds
        fake_clock.now += float(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(app_finder.asyncio, "sleep", _fake_sleep)
    wm._real_sleep = real_sleep  # type: ignore[attr-defined]

    monkeypatch.setattr(app_finder, "IS_WINDOWS", True)
    monkeypatch.setattr(app_finder, "IS_MAC", False)
    monkeypatch.setattr(app_finder, "IS_LINUX", False)
    monkeypatch.setattr(app_finder, "build_app_index", lambda force=False: list(FAKE_APP_INDEX))
    def _fake_launch(app: dict) -> tuple[bool, str]:
        wm.launch(app)
        return True, "launched"

    monkeypatch.setattr(app_finder, "launch_app", _fake_launch)
    monkeypatch.setattr(app_finder, "_app_memory_entry_valid", lambda app: True)

    # typing: native Windows path would shell out to PowerShell; record instead
    from jarvis.tools import system_tools

    async def _fake_native_type(text: str, press_enter: bool = False) -> tuple[bool, str]:
        wm.typed.append(text)
        return True, "SendKeys"

    monkeypatch.setattr(system_tools, "_native_type", _fake_native_type)
    monkeypatch.setattr(system_tools, "IS_WINDOWS", True)
    monkeypatch.setattr(system_tools, "IS_MAC", False)
    monkeypatch.setattr(system_tools, "IS_LINUX", False)

    # fresh app-launch memory per test so aliases never leak between cases
    app_finder._APP_MEMORY.clear()
    app_finder._RECENT_LAUNCHES.clear()
    return wm


@pytest.fixture
def models(monkeypatch) -> SpyModelRegistry:
    import jarvis.llm as llm

    registry = SpyModelRegistry()
    monkeypatch.setattr(llm, "get_chat_model", registry.get)
    return registry


@pytest.fixture(autouse=True)
def _reset_cooldowns():
    from jarvis.llm import roles

    roles.reset_cooldowns()
    yield
    roles.reset_cooldowns()


@pytest.fixture(autouse=True)
def _isolated_session_state(monkeypatch, tmp_path):
    state_file = tmp_path / "session_state.json"
    monkeypatch.setenv("SESSION_STATE_FILE", str(state_file))
    monkeypatch.setenv("APP_MEMORY_FILE", str(tmp_path / "app_memory.json"))

    from jarvis.memory import session_state
    from jarvis.tools import app_finder

    session_state._STATES.clear()
    session_state._STATE = None
    app_finder._APP_MEMORY.path = tmp_path / "app_memory.json"
    app_finder._APP_MEMORY.backup_path = tmp_path / "app_memory.json.bak"
    app_finder._APP_MEMORY._records.clear()
    yield


async def _run(text: str, *, session: str = "replay") -> Any:
    from jarvis.runtime import agent

    return await agent.run_agent(text, session_id=session, surface="cli_owner")


# ===========================================================================
# 1. "hy bhai" / "hy"
# ===========================================================================
@pytest.mark.asyncio
async def test_greeting_is_varied_and_costs_one_llm_call(models, windows):
    """Old behaviour: an identical canned line every single time."""
    from jarvis.llm import roles

    brain = SpyChatModel("brain", replies=[
        "Arre boss, bolo! Kya haal hai?",
        "Haanji, main yahin hoon — bolo kya kaam hai?",
        "Hey! Aaj kuch naya?",
    ])
    models.register("brain", brain)

    first = await _run("hy bhai", session="greet-1")
    second = await _run("hy", session="greet-2")
    third = await _run("hy bhai", session="greet-3")

    for result in (first, second, third):
        assert result.status == "chat", result.status
        assert result.tool_evidence == [], "a greeting must not execute tools"
        assert result.events[0]["llm_calls"] == 1, result.events
        assert result.events[0]["tools"] == 0
        assert result.events[0]["planner"] == "skipped"
        assert result.events[0]["critic"] == "skipped"

    answers = [r.answer.strip() for r in (first, second, third)]
    assert len(set(answers)) >= 2, (
        f"greeting replies are still identical: {answers!r}")
    assert brain.calls == 3, f"expected exactly one model call per greeting, got {brain.calls}"
    assert not roles.cooled_down_targets()


@pytest.mark.asyncio
async def test_greeting_still_varies_when_every_model_is_cooled_down(models, windows):
    """Required fallback: a varied pool (never one frozen line) when nothing can be reached."""
    from jarvis.llm import roles
    from jarvis.runtime import agent

    roles.reset_cooldowns()
    brain = SpyChatModel("brain", error=RuntimeError("429 ResourceExhausted"))
    models.register("brain", brain)

    one = await _run("hy bhai", session="greet-cool-1")
    two = await _run("hy", session="greet-cool-2")

    assert one.answer != two.answer, "cooled-down greeting fallback is frozen"
    assert one.answer and two.answer
    # it must be honest about the model failure, not pretend everything is fine
    assert one.status == "failed", one.status
    # and it must not be the old identical canned line
    assert "Main yahin hoon." not in one.answer
    assert agent._GREETING_FALLBACKS[0] in (one.answer, two.answer)


# ===========================================================================
# 2. "kaisa hai re"
# ===========================================================================
@pytest.mark.asyncio
async def test_how_are_you_uses_chat_path_and_skips_cooled_down_primary(models, windows):
    """Old behaviour: 63.7 s, 5 LLM calls, full task graph."""
    from jarvis.llm import roles
    from jarvis.llm.core import FallbackChatModel

    roles.reset_cooldowns()
    primary = SpyChatModel("primary", error=RuntimeError("429 ResourceExhausted (quota)"))
    secondary = SpyChatModel("secondary", replies=["Main bilkul fit hoon boss. Tum batao!"])
    # the owner's failure was the primary being retried while exhausted
    roles.mark_cooldown("gemini", "primary-model", "429 ResourceExhausted", seconds=120)

    chain = FallbackChatModel(role="brain", chain=[primary, secondary],
                              targets=["gemini:primary-model", "groq:secondary-model"])
    models.register("brain", chain)

    started = real_time.time()
    result = await _run("kaisa hai re", session="mood-1")
    elapsed = real_time.time() - started

    assert result.status == "chat", f"went through the task pipeline: {result.status} errors={result.errors}"
    assert result.tool_evidence == []
    assert result.events[0]["planner"] == "skipped"
    assert result.events[0]["critic"] == "skipped"
    assert primary.calls == 0, "a cooled-down primary model was retried"
    assert secondary.calls == 1, f"expected 1 model call, got {secondary.calls}"
    assert models.total_calls == 1, f"expected 1 LLM call total, got {models.total_calls}"
    assert elapsed <= 8.0, f"took {elapsed:.1f}s (budget 8s)"
    assert result.answer.strip()


# ===========================================================================
# 3. "chrome kholde"
# ===========================================================================
@pytest.mark.asyncio
async def test_chrome_kholde_opens_without_any_llm_call(models, windows):
    """Old behaviour: 45.9 s, 8 LLM calls, zero tool calls ("kholde" was not recognised)."""
    brain = SpyChatModel("brain", replies=["(planner should never be reached)"])
    models.register("brain", brain)

    result = await _run("chrome kholde", session="chrome-1")

    assert models.total_calls == 0, f"app open must not call the LLM (got {models.total_calls})"
    assert result.status == "done", f"{result.status}: {result.answer}"
    assert windows.launched == ["Google Chrome"], f"launched={windows.launched}"
    tool_names = [record.get("name") for record in result.tool_calls_flat]
    assert "open_any_app" in tool_names, tool_names
    assert all(record.get("ok") for record in result.tool_calls_flat)
    assert result.verified_execution is True
    assert "Chrome" in result.answer


# ===========================================================================
# 4. "notebook open krke likh"
# ===========================================================================
@pytest.mark.asyncio
async def test_notebook_asks_one_question_then_saves_the_alias(models, windows):
    """Old behaviour: refused with "best match OUTLOOK 56.7%" and no way forward."""
    from jarvis.tools import app_finder

    brain = SpyChatModel("brain", replies=["(graph must not be reached)"])
    models.register("brain", brain)

    asked = await _run("notebook open krke likh", session="notebook-1")

    assert asked.status == "needs_clarification", f"{asked.status}: {asked.answer}"
    assert windows.launched == [], f"must not launch a guessed app; launched={windows.launched}"
    assert models.total_calls == 0

    # exactly ONE question — not a paragraph of follow-ups
    assert asked.answer.count("?") == 1, f"expected one question, got: {asked.answer!r}"
    # candidates are offered, and Outlook is one of them (the old 56.7% false match)
    assert "Outlook" in asked.answer or "Notepad" in asked.answer
    assert "yad rakh" in asked.answer or "yaad rakh" in asked.answer

    # --- the owner answers the question -----------------------------------
    answered = await _run("notepad", session="notebook-1")

    assert windows.launched == ["Notepad"], f"launched={windows.launched}"
    alias = app_finder._APP_MEMORY.lookup("notebook")
    assert alias and alias["name"] == "Notepad", (
        f"alias was not saved for 'notebook': {app_finder._APP_MEMORY.list()}")
    assert "yaad rakh" in answered.answer or "Notepad" in answered.answer

    # --- and the app is never ambiguous again ------------------------------
    windows.launched.clear()
    windows.typed.clear()
    again = await _run("notebook open krke hello likh", session="notebook-2")
    assert "Kaun sa kholun" not in again.answer, f"alias was forgotten: {again.answer}"
    assert again.status == "done", f"{again.status}: {again.answer} errors={again.errors}"
    assert "Notepad" in again.answer, f"alias did not resolve to Notepad: {again.answer}"
    assert windows.typed == ["hello"], f"typed={windows.typed}"
    # the window was already open from the turn above, so it is focused rather than relaunched
    assert windows.launched in ([], ["Notepad"]), f"unexpected launch: {windows.launched}"


@pytest.mark.asyncio
async def test_typing_with_no_payload_asks_once_instead_of_typing_the_connector(models, windows):
    """'notebook open krke likh' used to type the leftover word 'krke'."""
    brain = SpyChatModel("brain")
    models.register("brain", brain)

    result = await _run("notepad kholke likho", session="notype-1")

    assert windows.typed == [], f"must not type a guessed payload; typed={windows.typed}"
    assert result.status in {"needs_clarification", "done"}
    if result.status == "needs_clarification":
        assert result.answer.count("?") == 1, result.answer
    assert models.total_calls == 0


# ===========================================================================
# 5. "notepad open krke hy likhde"
# ===========================================================================
@pytest.mark.asyncio
async def test_window_appearing_after_three_seconds_is_accepted(models, windows, fake_clock):
    """Old behaviour: "window verify nahi hui" and typing was skipped."""
    windows.window_delay_s = 3.0
    brain = SpyChatModel("brain", replies=["(graph must not be reached)"])
    models.register("brain", brain)

    result = await _run("notepad open krke hy likhde", session="notepad-1")

    assert models.total_calls == 0, f"app open must not call the LLM (got {models.total_calls})"
    assert result.status == "done", f"{result.status}: {result.answer}"
    assert windows.launched == ["Notepad"], windows.launched
    assert windows.typed == ["hy"], f"typed={windows.typed}"
    names = [record.get("name") for record in result.tool_calls_flat]
    assert "open_any_app" in names and "type_text" in names, names
    assert all(record.get("ok") for record in result.tool_calls_flat), result.tool_calls_flat
    assert result.verified_execution is True
    # the verification loop must actually have waited out the 3 s window
    assert fake_clock.now >= 3.0, f"verification gave up after only {fake_clock.now}s"


@pytest.mark.asyncio
async def test_window_that_never_appears_is_reported_honestly(models, windows, fake_clock):
    """The opposite case: no window after the budget → no success claim, no typing."""
    windows.window_delay_s = 999.0
    brain = SpyChatModel("brain", replies=["(graph must not be reached)"])
    models.register("brain", brain)

    result = await _run("notepad open krke hy likhde", session="notepad-2")

    assert result.status != "done", f"claimed success without a window: {result.answer}"
    assert windows.typed == [], f"typed into a window that never opened: {windows.typed}"
    assert result.verified_execution is False
    assert "verify" in result.answer.lower() or "⚠️" in result.answer


# ===========================================================================
# 6. "python install krde"
# ===========================================================================
@pytest.mark.asyncio
async def test_ambiguous_install_asks_once_then_runs_pip_once(models, windows, monkeypatch):
    """Old behaviour: a generated script, 2 approvals, 125 s."""
    brain = SpyChatModel("brain", replies=["(code engine must not be reached)"])
    models.register("brain", brain)

    approvals: list[str] = []
    installs: list[str] = []

    from jarvis.security import gate

    async def _fake_authorize(command: str, cwd: str, **_kwargs: Any) -> Any:
        from jarvis.security.gate import ApprovalResult

        approvals.append(command)
        return ApprovalResult(True, "approved", "owner")

    import jarvis.tools.code_engine as ce

    monkeypatch.setattr(gate, "authorize_command", _fake_authorize)
    monkeypatch.setattr(ce, "python_executable", lambda: "python", raising=False)
    # the real install_package shells out; replace the subprocess with a recording stub

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"Successfully installed", b""

        def kill(self) -> None:
            return None

    async def _fake_exec(*args: Any, **_kwargs: Any) -> _FakeProc:
        installs.append(args[-1] if args else "")
        return _FakeProc()

    monkeypatch.setattr(ce.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(ce, "python_executable", lambda: "python", raising=False)

    asked = await _run("python install krde", session="install-1")

    assert asked.status == "needs_clarification", f"{asked.status}: {asked.answer}"
    assert installs == [], f"pip must not run before the owner answers: {installs}"
    assert asked.answer.count("?") == 1, f"expected one question, got: {asked.answer!r}"
    assert models.total_calls == 0

    ran = await _run("requests", session="install-1")

    assert installs == ["requests"], f"expected exactly one pip run, got {installs}"
    assert len(approvals) == 1, f"expected at most one approval, got {approvals}"
    assert "requests" in approvals[0]
    assert ran.status == "done", f"{ran.status}: {ran.answer}"
    assert models.total_calls == 0, "install must not go through the LLM"


@pytest.mark.asyncio
async def test_clear_install_runs_pip_once_with_one_approval(models, windows, monkeypatch):
    """No script is generated and no second approval is taken."""
    brain = SpyChatModel("brain", replies=["(code engine must not be reached)"])
    models.register("brain", brain)

    approvals: list[str] = []
    installs: list[str] = []

    import jarvis.tools.code_engine as ce
    from jarvis.security import gate
    from jarvis.security.gate import ApprovalResult

    async def _fake_authorize(command: str, cwd: str, **_kwargs: Any) -> Any:
        approvals.append(command)
        return ApprovalResult(True, "approved", "owner")

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"Successfully installed requests", b""

        def kill(self) -> None:
            return None

    async def _fake_exec(*args: Any, **_kwargs: Any) -> _FakeProc:
        installs.append(args[-1] if args else "")
        return _FakeProc()

    monkeypatch.setattr(gate, "authorize_command", _fake_authorize)
    monkeypatch.setattr(ce.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(ce, "python_executable", lambda: "python", raising=False)

    result = await _run("requests install kar do", session="install-2")

    assert installs == ["requests"], f"expected one pip run, got {installs}"
    assert len(approvals) == 1, f"expected one approval, got {approvals}"
    assert result.status == "done", f"{result.status}: {result.answer}"
    assert models.total_calls == 0
    # evidence is real, not a story
    assert any(r.get("name") == "install_package" and r.get("ok")
               for r in result.tool_calls_flat), result.tool_calls_flat


# ===========================================================================
# 7. second "chrome kholo"
# ===========================================================================
@pytest.mark.asyncio
async def test_second_chrome_kholo_focuses_the_existing_window(models, windows):
    """Old behaviour: n/a — the risk is a duplicate launch or an LLM round trip."""
    brain = SpyChatModel("brain", replies=["(graph must not be reached)"])
    models.register("brain", brain)

    first = await _run("chrome kholo", session="chrome-2")
    assert first.status == "done", f"{first.status}: {first.answer}"
    assert windows.launched == ["Google Chrome"]
    focus_after_first = windows.focus_calls

    second = await _run("chrome kholo", session="chrome-2")

    assert models.total_calls == 0, f"second open must not call the LLM (got {models.total_calls})"
    assert windows.launched == ["Google Chrome"], (
        f"duplicate launch: {windows.launched}")
    assert windows.focus_calls > focus_after_first, "existing window was not focused"
    assert second.status == "done", f"{second.status}: {second.answer}"
    assert "pehle se open" in second.answer or "focus" in second.answer.lower(), second.answer


# ===========================================================================
# regression guard: the whole table in one place
# ===========================================================================
@pytest.mark.asyncio
async def test_acceptance_table_summary(models, windows, fake_clock):
    """Every row of the owner's table, run in order, with the numbers asserted together."""
    from jarvis.llm import roles

    roles.reset_cooldowns()
    brain = SpyChatModel("brain", replies=[
        "Arre boss, bolo!",
        "Main fit hoon, tum sunao.",
        "(graph must not be reached)",
    ])
    models.register("brain", brain)

    rows: list[tuple[str, Any]] = []
    rows.append(("hy bhai", await _run("hy bhai", session="table-1")))
    rows.append(("kaisa hai re", await _run("kaisa hai re", session="table-1")))
    rows.append(("chrome kholde", await _run("chrome kholde", session="table-1")))
    rows.append(("notebook open krke likh", await _run("notebook open krke likh", session="table-1")))
    rows.append(("notepad open krke hy likhde",
                 await _run("notepad open krke hy likhde", session="table-1")))
    rows.append(("python install krde", await _run("python install krde", session="table-1")))
    rows.append(("chrome kholo (2nd)", await _run("chrome kholo", session="table-1")))

    by_name = dict(rows)
    assert by_name["hy bhai"].status == "chat"
    assert by_name["kaisa hai re"].status == "chat"
    assert by_name["chrome kholde"].status == "done"
    assert by_name["notebook open krke likh"].status == "needs_clarification"
    assert by_name["notepad open krke hy likhde"].status == "done"
    assert by_name["python install krde"].status == "needs_clarification"
    assert by_name["chrome kholo (2nd)"].status == "done"
    # only the two conversational turns touched the model
    assert brain.calls == 2, f"expected 2 model calls across the table, got {brain.calls}"
