from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from jarvis.tools import browser_tools


class FakeActionRegistry:
    def __init__(self):
        self.actions = {}

    def action(self, description):
        def register(fn):
            self.actions[fn.__name__] = (description, fn)
            return fn

        return register


class FakeBrowserTools:
    def __init__(self):
        self.registry = FakeActionRegistry()


def _install_fake_browser_use(monkeypatch):
    module = ModuleType("browser_use")
    module.Tools = FakeBrowserTools
    monkeypatch.setitem(sys.modules, "browser_use", module)


class FakeJarvisTool:
    def __init__(self, name, result="ok"):
        self.name = name
        self.result = result
        self.calls = []

    async def ainvoke(self, payload):
        self.calls.append(payload)
        return self.result


@pytest.mark.asyncio
async def test_browser_request_tool_uses_registry_permissions_and_human_gate(monkeypatch):
    _install_fake_browser_use(monkeypatch)
    import jarvis.tools as registry
    from jarvis.security import gate
    from jarvis.security.permissions import use_surface

    delete_tool = FakeJarvisTool("delete_file", "deleted")
    monkeypatch.setattr(registry, "ALL_TOOLS", [delete_tool])
    browser_registry = browser_tools._build_browser_tools()
    request_tool = browser_registry.registry.actions["jarvis_request_tool"][1]
    approvals = []

    async def deny(command, cwd, **kwargs):
        approvals.append((command, cwd, kwargs))
        return SimpleNamespace(allowed=False, reason="owner denied")

    monkeypatch.setattr(gate, "authorize_command", deny)
    with use_surface("cli_owner"):
        blocked = await request_tool("delete_file", '{"path":"memo.txt","token":"private-value"}')
    assert "owner denied" in blocked
    assert not delete_tool.calls
    assert len(approvals) == 1 and approvals[0][2]["force"] is True
    assert "private-value" not in approvals[0][0]

    read_tool = FakeJarvisTool("read_note", "note contents")
    monkeypatch.setattr(registry, "ALL_TOOLS", [read_tool])
    with use_surface("cli_owner"):
        result = await request_tool("read_note", '{"name":"daily"}')
        unknown = await request_tool("not_registered", "{}")
    assert result == "note contents" and read_tool.calls == [{"name": "daily"}]
    assert "registered Jarvis tool" in unknown


@pytest.mark.asyncio
async def test_browser_request_tool_denies_surface_before_invocation(monkeypatch):
    _install_fake_browser_use(monkeypatch)
    import jarvis.tools as registry
    from jarvis.security import gate
    from jarvis.security.permissions import use_surface

    shell_tool = FakeJarvisTool("run_shell_command", "executed")
    monkeypatch.setattr(registry, "ALL_TOOLS", [shell_tool])
    request_tool = browser_tools._build_browser_tools().registry.actions["jarvis_request_tool"][1]

    async def forbidden_gate(*_args, **_kwargs):
        raise AssertionError("surface-denied tool must not reach approval or execution")

    monkeypatch.setattr(gate, "authorize_command", forbidden_gate)
    with use_surface("api"):
        result = await request_tool("run_shell_command", '{"command":"echo safe"}')
    assert "Permission blocked" in result
    assert shell_tool.calls == []


@pytest.mark.asyncio
async def test_browser_task_gates_destructive_intent_but_not_read_only_how_to_search(monkeypatch):
    from jarvis.security import gate
    from jarvis.security.permissions import use_surface

    calls = {"gate": 0, "run": 0}

    async def deny(command, cwd, **kwargs):
        calls["gate"] += 1
        assert kwargs["force"] is True
        return SimpleNamespace(allowed=False, reason="cancelled by owner")

    async def fake_run(**kwargs):
        calls["run"] += 1
        return {"ok": True, "result": "read-only result", "urls": [], "steps": 1}

    monkeypatch.setattr(gate, "authorize_command", deny)
    monkeypatch.setattr(browser_tools, "run_browser_task", fake_run)
    with use_surface("cli_owner"):
        denied = await browser_tools.browser_task.ainvoke({"task": "Delete my account from the website"})
        safe = await browser_tools.browser_task.ainvoke(
            {"task": "Find official instructions on how to delete an account"}
        )
    assert "cancelled by owner" in denied
    assert "read-only result" in safe
    assert calls == {"gate": 1, "run": 1}


class FakeBrowserHistory:
    def __init__(self, *, result="verified result", urls=None, structured=None):
        self._result = result
        self._urls = urls or ["https://example.test/page?token=secret-token"]
        self.structured_output = structured or {"value": "verified"}

    def final_result(self):
        return self._result

    def urls(self):
        return self._urls

    def model_actions(self):
        return [{"click": "Continue"}]


class FakeBrowserAgent:
    def __init__(self, *, repeat_state=False):
        self.browser_session = SimpleNamespace(
            get_browser_state_summary=self._summary,
            get_current_page_url=self._url,
            get_current_page_title=self._title,
        )
        self.state = SimpleNamespace(n_steps=0)
        self.history = FakeBrowserHistory()
        self.repeat_state = repeat_state
        self.paused = False
        self.max_steps = 0

    async def _summary(self):
        return SimpleNamespace(url="https://example.test/page", title="Results", dom_state="same" if self.repeat_state else str(self.state.n_steps))

    async def _url(self):
        return "https://example.test/page"

    async def _title(self):
        return "Results"

    def pause(self):
        self.paused = True

    async def run(self, max_steps, on_step_end=None):
        self.max_steps = max_steps
        if self.repeat_state:
            for step in range(1, 20):
                self.state.n_steps = step
                await on_step_end(self)
                if self.paused:
                    await asyncio.sleep(30)
        else:
            self.state.n_steps = 1
            await on_step_end(self)
            return self.history


@pytest.mark.asyncio
async def test_browser_steps_emit_progress_sanitize_urls_and_honor_step_cap(monkeypatch):
    from jarvis.runtime.progress import use_sink
    from tests.support import CollectingSink

    agent = FakeBrowserAgent()
    monkeypatch.setattr(browser_tools, "build_browser_agent", lambda *_args, **_kwargs: _async_value(agent))
    notes = []

    async def save_note(name, content, *, tags):
        notes.append((name, content, tags))
        return "✅ note saved"

    monkeypatch.setattr(browser_tools, "_save_browser_note", save_note)
    sink = CollectingSink()
    callback_steps = []
    with use_sink(sink):
        result = await browser_tools.run_browser_task(
            "research page", max_steps=999, timeout=10, save_note_as="browser-report",
            on_step=callback_steps.append,
        )
    assert result["ok"] is True
    assert agent.max_steps <= browser_tools.BROWSER_MAX_STEPS
    assert result["steps"] == 1 and len(callback_steps) == 1
    assert "secret-token" not in result["urls"][0]
    assert notes and notes[0][0] == "browser-report" and notes[0][2] == "browser-use"
    assert any(event.kind == "thinking" for event in sink.events)
    assert any(event.kind == "tool_end" and event.status == "ok" for event in sink.events)


@pytest.mark.asyncio
async def test_browser_stuck_loop_is_interrupted_and_reported(monkeypatch):
    agent = FakeBrowserAgent(repeat_state=True)
    monkeypatch.setattr(browser_tools, "BROWSER_STUCK_REPEAT_LIMIT", 3)
    monkeypatch.setattr(browser_tools, "build_browser_agent", lambda *_args, **_kwargs: _async_value(agent))
    result = await browser_tools.run_browser_task("stuck page", timeout=3, max_steps=15)
    assert result["ok"] is False
    assert "Browser rok diya" in result["result"]
    assert agent.paused
    assert result["steps"] == 3


@pytest.mark.asyncio
async def test_parallel_browser_research_normalizes_saves_and_reports_partial_failure(monkeypatch):
    from jarvis.dspy_engine import modules as dspy_modules

    monkeypatch.setattr(browser_tools, "BROWSER_MAX_PARALLEL", 2)
    active = 0
    peak_active = 0
    calls = []

    async def fake_browser_task(*, task, start_url, output_fields, max_steps, isolated_session=False, **_kwargs):
        nonlocal active, peak_active
        calls.append((start_url, isolated_session, output_fields, max_steps))
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        if "broken" in start_url:
            raise RuntimeError("fake site failure")
        return {"ok": True, "result": "product facts", "urls": [start_url + "/source"],
                "structured": {"price": "10"}, "steps": 2}

    normalized = []

    def fake_normalize(rows, fields):
        normalized.append((rows, fields))
        return [{"site": "https://shop.test", "price": "10 USD"}]

    saved = []

    async def fake_save(name, content, *, tags, owner_write_allowed):
        assert owner_write_allowed is True
        saved.append((name, content, tags, owner_write_allowed))
        return "✅ report note saved"

    monkeypatch.setattr(browser_tools, "run_browser_task", fake_browser_task)
    monkeypatch.setattr(browser_tools, "_save_browser_note", fake_save)
    monkeypatch.setattr(dspy_modules, "normalize_rows", fake_normalize)
    monkeypatch.setattr(browser_tools, "is_offline", lambda: False)
    monkeypatch.setattr(browser_tools, "PROVIDER", "gemini")

    report = await browser_tools.browser_research.ainvoke({
        "task": "compare listed prices and save this report", "sites": ["https://shop.test", "https://broken.test"],
        "fields": ["price"], "save_note_as": "price-report", "max_parallel": 20,
    })
    assert peak_active == 2
    assert len(calls) == 2 and all(item[1] is True for item in calls)
    assert len(normalized) == 1 and normalized[0][1] == ["price"]
    assert saved and saved[0][0] == "price-report" and "10 USD" in saved[0][1]
    assert saved[0][3] is True
    assert "1/2 sites returned results" in report
    assert "broken.test — incomplete/failed" in report and "shop.test/source" in report
    assert "report note saved" in report


@pytest.mark.asyncio
async def test_browser_note_write_requires_explicit_owner_intent_and_allowed_surface(monkeypatch):
    from jarvis.security.permissions import use_surface
    from jarvis.tools import notes_tools

    writes = []

    class FakeSaveNote:
        async def ainvoke(self, payload):
            writes.append(payload)
            return "✅ note saved"

    monkeypatch.setattr(notes_tools, "save_note", FakeSaveNote())
    assert browser_tools._browser_memory_context("compare products") is False
    with use_surface("telegram_guest"):
        assert browser_tools._browser_memory_context("compare products and save this report") is False
        blocked = await browser_tools._save_browser_note(
            "report", "page content", tags="browser-research", owner_write_allowed=False,
        )
    assert "save nahi kiya" in blocked
    assert writes == []

    with use_surface("cli_owner"):
        assert browser_tools._browser_memory_context("compare products and save this report") is True
        allowed = await browser_tools._save_browser_note(
            "report", "owner-requested report", tags="browser-research", owner_write_allowed=True,
        )
    assert allowed == "✅ note saved"
    assert writes == [{"name": "report", "content": "owner-requested report",
                       "open_in_notepad": False, "tags": "browser-research"}]


def test_parallel_browser_sessions_do_not_reuse_the_owner_profile(monkeypatch):
    root = ModuleType("browser_use")
    root.__path__ = []
    package = ModuleType("browser_use.browser")
    package.__path__ = []
    profile_module = ModuleType("browser_use.browser.profile")

    class FakeProfile:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeSession:
        def __init__(self, browser_profile):
            self.browser_profile = browser_profile

    root.BrowserSession = FakeSession
    profile_module.BrowserProfile = FakeProfile
    monkeypatch.setitem(sys.modules, "browser_use", root)
    monkeypatch.setitem(sys.modules, "browser_use.browser", package)
    monkeypatch.setitem(sys.modules, "browser_use.browser.profile", profile_module)
    monkeypatch.setattr(browser_tools, "BROWSER_USER_DATA_DIR", "persistent-owner-profile")

    isolated = browser_tools._make_browser_session(isolated=True)
    reused = browser_tools._make_browser_session(isolated=False)
    assert isolated is not reused
    assert isolated.browser_profile.kwargs["keep_alive"] is False
    assert "user_data_dir" not in isolated.browser_profile.kwargs
    assert reused.browser_profile.kwargs["user_data_dir"] == "persistent-owner-profile"


async def _async_value(value):
    return value
