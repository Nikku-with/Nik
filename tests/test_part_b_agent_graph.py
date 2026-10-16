"""Behavioral regressions for Part B graph/agent findings."""
from __future__ import annotations

import asyncio


def test_plan_progress_renders_numbered_checklist_steps():
    from jarvis.runtime.progress import ProgressEvent

    line = ProgressEvent("plan", "2-step plan ready", detail="search official source → verify result").line()
    assert "1. search official source" in line
    assert "2. verify result" in line


async def test_empty_model_synthesis_falls_back_to_verified_evidence(monkeypatch):
    from types import SimpleNamespace

    from jarvis.graph import synthesis
    from jarvis.graph.state import initial_state

    calls = {"final": 0, "brain": 0}

    def empty_final_answer(*_args, **_kwargs):
        calls["final"] += 1
        return ""

    class EmptyBrain:
        async def ainvoke(self, _messages):
            calls["brain"] += 1
            return SimpleNamespace(content="")

    monkeypatch.setattr(synthesis, "is_offline", lambda: False)
    monkeypatch.setattr(synthesis, "PROVIDER", "gemini")
    monkeypatch.setattr(synthesis.dspy_mods, "final_answer", empty_final_answer)
    monkeypatch.setattr(synthesis, "get_chat_model", lambda _role: EmptyBrain())

    state = initial_state("find report.txt", session_id="empty-synthesis")
    state.update({
        "workers": ["desktop"],
        "execution_required": True,
        "findings": [{"worker": "desktop", "summary": "Found report.txt", "tool_calls": [
            {"name": "find_files", "args": {"name": "report.txt"}, "ok": True, "output": "report.txt"},
        ]}],
        "tool_evidence": [{"name": "find_files", "args": {"name": "report.txt"},
                           "ok": True, "output": "report.txt"}],
        "actions_done": [{"name": "find_files", "args": {"name": "report.txt"},
                           "ok": True, "output": "report.txt"}],
    })
    result = await synthesis.synthesize_node(state)

    assert calls == {"final": 1, "brain": 1}
    assert "report.txt" in result["answer"]
    assert result.get("status", "done") == "done"
    assert not result.get("errors")


def test_execution_claim_detection_keeps_unrelated_success_claims():
    from jarvis.graph.nodes import _claims_success
    from jarvis.runtime.evidence import claims_execution

    answer = "The task is complete. I could not find the saved file."
    claims = claims_execution("I opened Notepad. I could not find the saved file.")
    assert "i opened" in claims
    assert claims_execution("I did not open Notepad.") == []
    assert _claims_success(answer) is True
    assert _claims_success("The task is not complete.") is False


def test_route_to_desktop_is_not_enough_to_require_an_action():
    from jarvis.graph.nodes import _needs_action, detect_desktop_action
    from jarvis.runtime.evidence import is_tool_required

    how_to = {"request": "How do I open Notepad?", "workers": ["desktop"]}
    imperative = {"request": "Open Notepad", "workers": ["desktop"]}
    assert is_tool_required(how_to) is False
    assert _needs_action(how_to["request"]) is False
    assert detect_desktop_action(how_to["request"]) == {}
    assert is_tool_required(imperative) is True
    assert _needs_action(imperative["request"]) is True


def test_read_file_content_words_do_not_override_tool_status():
    from jarvis.graph.nodes import _tool_call_record

    text = "The document says: cannot proceed because of an exception; this is quoted content."
    assert _tool_call_record("read_file", {"path": "report.txt"}, text)["ok"] is True
    assert _tool_call_record("read_file", {"path": "report.txt"}, "Exception: quoted content", ok=True)["ok"] is True
    assert _tool_call_record("read_file", {"path": "report.txt"}, text, ok=False)["ok"] is False


def test_honesty_guard_does_not_skip_real_graph_statuses():
    from jarvis.graph.nodes import honesty_guard

    state = {
        "request": "Open Notepad",
        "workers": ["desktop"],
        "status": "needs_clarification",
        "execution_required": True,
        "tool_evidence": [],
    }
    answer, reason = honesty_guard(state, "Notepad opened successfully")
    assert reason
    assert "verify" in answer.lower() or "tool call" in answer.lower()


def test_synthesize_returns_guard_errors_in_its_state_update(monkeypatch):
    from jarvis.graph import synthesis
    from jarvis.graph.state import initial_state

    monkeypatch.setattr(synthesis, "is_offline", lambda: True)
    monkeypatch.setattr(synthesis, "_offline_synthesize", lambda *_args: "Notepad opened successfully")
    monkeypatch.setattr(synthesis, "evidence_digest", lambda _state: "no tool evidence")
    state = initial_state("Open Notepad", session_id="guard-errors")
    state.update({"workers": ["desktop"], "execution_required": True, "tool_evidence": []})

    update = asyncio.run(synthesis.synthesize_node(state))
    assert any("honesty_guard" in error or "invariant" in error for error in update.get("errors", []))
    assert any(event.get("node") in {"honesty_guard", "invariant"} for event in update.get("events", []))


def test_critic_failure_keeps_answer_and_never_routes_to_refine(monkeypatch):
    from jarvis.dspy_engine import modules
    from jarvis.graph import critic
    from jarvis.graph.state import initial_state

    monkeypatch.setattr(modules, "is_offline", lambda: False)
    monkeypatch.setattr(modules, "PROVIDER", "test")
    monkeypatch.setattr(modules, "ensure_ready", lambda: False)
    monkeypatch.setattr(critic, "is_offline", lambda: False)
    monkeypatch.setattr(critic, "PROVIDER", "test")
    monkeypatch.setattr(critic.dspy_mods, "critic", modules.critic)
    state = initial_state("Research this claim", session_id="critic-error")
    state.update({"workers": ["research"], "answer": "A useful draft that should remain unchanged."})

    update = asyncio.run(critic.critic_node(state))
    next_node = critic.after_critic({**state, **update})
    assert update["critique"].get("error")
    assert update["critique"].get("ok") is True
    assert next_node == "persist"
    assert state["answer"] == "A useful draft that should remain unchanged."


def test_tracked_worker_converts_exception_to_failure_record():
    from langgraph.graph import END, START, StateGraph

    from jarvis.graph.build import _tracked_worker
    from jarvis.graph.state import JarvisState, initial_state

    async def broken_research(_state):
        raise RuntimeError("temporary search failure")

    graph_builder = StateGraph(JarvisState)
    graph_builder.add_node("research_worker", _tracked_worker("research", broken_research))
    graph_builder.add_edge(START, "research_worker")
    graph_builder.add_edge("research_worker", END)
    result = asyncio.run(graph_builder.compile().ainvoke(initial_state("find a source")))

    assert result["worker_results"]["research"]["ok"] is False
    assert "temporary search failure" in " ".join(result["errors"])
    assert result["worker_results"]["research"]["tool_count"] == 0


def test_stream_agent_delegates_to_run_agent_with_surface_and_fast_paths(monkeypatch):
    from jarvis.runtime import agent
    from jarvis.runtime.progress import ProgressEvent

    received = {}

    async def fake_run_agent(text, **kwargs):
        received.update(text=text, **kwargs)
        kwargs["on_progress"](ProgressEvent(kind="thinking", text="running fast path"))
        return agent.AgentResult(request=text, answer="stream result", status="done", surface=kwargs["surface"])

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)

    async def collect():
        return [chunk async for chunk in agent.stream_agent(
            "What time is it?", session_id="stream-session", surface="telegram_owner",
            memory_user_id="owner-123",
        )]

    chunks = asyncio.run(collect())
    assert received["surface"] == "telegram_owner"
    assert received["session_id"] == "stream-session"
    assert received["memory_user_id"] == "owner-123"
    assert received["use_fast_path"] is True
    assert any(chunk.get("node") == "progress" for chunk in chunks)
    assert chunks[-1]["node"] == "final"
    assert chunks[-1]["answer"] == "stream result"


def test_time_and_keyword_routing_use_word_boundaries():
    from jarvis.dspy_engine.modules import fallback_route
    from jarvis.runtime.agent import _is_time_question

    assert _is_time_question("Let's talk about dinner plans") is False
    assert _is_time_question("I am reading a book") is False
    assert _is_time_question("What day is it today?") is True
    assert fallback_route("What is the opposite of this word?") == ["answer"]
    assert fallback_route("I like this notebook") == ["answer"]


def test_compound_app_open_actions_are_split_and_ordered():
    from jarvis.graph.nodes import _execute_desktop_action, detect_desktop_action

    action = detect_desktop_action("Notepad kholo aur Chrome kholo")
    assert [item["app"] for item in action["actions"] if item["kind"] == "open_app"] == ["notepad", "chrome"]

    calls = []

    class FakeOpenTool:
        name = "open_any_app"

        async def ainvoke(self, args):
            calls.append(args["query"])
            return f"Opened {args['query']}"

    asyncio.run(_execute_desktop_action(action, [FakeOpenTool()]))
    assert calls == ["notepad", "chrome"]


def test_compound_app_fast_path_preserves_unhandled_second_clause(monkeypatch):
    from jarvis.graph import nodes
    from jarvis.runtime.agent import _direct_app_request

    invoked = []

    class Tool:
        name = "open_any_app"

    async def fake_execute(action, _tools):
        invoked.append(action)
        return [{"name": "open_any_app", "args": {}, "output": "Opened Chrome", "ok": True}]

    monkeypatch.setattr(nodes, "_desktop_tools_and_blocked", lambda *_args: ([Tool()], []))
    monkeypatch.setattr(nodes, "_execute_desktop_action", fake_execute)

    async def run():
        return await _direct_app_request(
            "Chrome kholo aur latest weather search karo", "compound", "cli_owner", lambda *_a, **_k: None
        )

    result = asyncio.run(run())
    assert result is None
    assert invoked == []

    from jarvis.graph import build
    from jarvis.runtime import agent

    received = {}

    async def no_fast_path(*_args):
        return None

    async def fake_graph(request, **_kwargs):
        received["request"] = request
        return {"answer": "I will handle both ordered tasks.", "status": "done", "workers": ["desktop", "research"]}

    monkeypatch.setattr(agent, "_fast_paths", no_fast_path)
    monkeypatch.setattr(agent, "_post_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(build, "jarvis_ainvoke", fake_graph)

    async def run_agent_request():
        return await agent.run_agent("Chrome kholo aur latest weather search karo", session_id="compound-test")

    result = asyncio.run(run_agent_request())
    assert received["request"] == "Chrome kholo aur latest weather search karo"
    assert "both ordered tasks" in result.answer
    assert invoked == []
