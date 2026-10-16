from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage


class FakeModel:
    tools_bound = True


class DummyTool:
    name = "open_any_app"
    args_schema = None
    description = "Open an app"


@pytest.mark.asyncio
async def test_empty_refusal_gets_one_tool_enabled_retry_and_real_evidence(monkeypatch):
    from jarvis.graph.workers import common

    class FakeAgent:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, _payload, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"messages": [AIMessage(content="I cannot do that.")]}
            return {"messages": [
                AIMessage(content="", tool_calls=[{"name": "open_any_app", "args": {"query": "Notepad"},
                                                   "id": "open-1", "type": "tool_call"}]),
                ToolMessage(content="✅ Notepad active window verified", tool_call_id="open-1", status="success"),
                AIMessage(content="Notepad is open and its active window was verified."),
            ]}

    fake_agent = FakeAgent()
    monkeypatch.setattr(common, "is_offline", lambda: False)
    monkeypatch.setattr(common, "PROVIDER", "fake")
    monkeypatch.setattr(common, "get_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr("langchain.agents.create_agent", lambda *_args, **_kwargs: fake_agent)

    run = await common._run_react_subagent(
        "desktop", "Open Notepad", [DummyTool()], raw_request="Open Notepad",
    )
    assert fake_agent.calls == 2
    assert run.retries == 1
    assert run.calls and run.calls[0]["ok"] is True
    assert "active window was verified" in run.text


@pytest.mark.asyncio
async def test_refusal_retry_never_loops_and_user_gate_is_not_retried(monkeypatch):
    from jarvis.graph.workers import common

    class FakeAgent:
        def __init__(self, body):
            self.calls = 0
            self.body = body

        async def ainvoke(self, _payload, **_kwargs):
            self.calls += 1
            return {"messages": [AIMessage(content=self.body)]}

    monkeypatch.setattr(common, "is_offline", lambda: False)
    monkeypatch.setattr(common, "PROVIDER", "fake")
    monkeypatch.setattr(common, "get_chat_model", lambda *_args, **_kwargs: FakeModel())

    normal = FakeAgent("I found the right app in the registry.")
    monkeypatch.setattr("langchain.agents.create_agent", lambda *_args, **_kwargs: normal)
    result = await common._run_react_subagent("desktop", "open app", [DummyTool()], raw_request="open app")
    assert normal.calls == 1 and result.retries == 0

    manual = FakeAgent("Login required. Please sign in first.")
    monkeypatch.setattr("langchain.agents.create_agent", lambda *_args, **_kwargs: manual)
    result = await common._run_react_subagent("desktop", "open website", [DummyTool()], raw_request="open website")
    assert manual.calls == 1 and result.retries == 0

    repeated = FakeAgent("I cannot do that.")
    monkeypatch.setattr("langchain.agents.create_agent", lambda *_args, **_kwargs: repeated)
    result = await common._run_react_subagent("desktop", "open app", [DummyTool()], raw_request="open app")
    assert repeated.calls == 2 and result.retries == 1
