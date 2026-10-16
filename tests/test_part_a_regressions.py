"""Behavioral reproductions for Part A duplicate-execution and routing defects."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace


def test_router_treats_string_false_as_false(monkeypatch):
    from jarvis.dspy_engine import modules

    prediction = SimpleNamespace(
        workers=["answer"], reason="clear request", needs_clarification="False"
    )
    monkeypatch.setattr(modules, "is_offline", lambda: False)
    monkeypatch.setattr(modules, "PROVIDER", "test")
    monkeypatch.setattr(modules, "get_module", lambda _name: lambda **_kwargs: prediction)
    monkeypatch.setattr(modules, "_safe", lambda call, _default, **_kwargs: call())

    request = "A clear request that must not need clarification"
    result = modules.route(request)
    assert result["needs_clarification"] is False

    from jarvis.graph.nodes import after_route, route_node
    from jarvis.graph.state import initial_state

    state = initial_state(request)
    update = asyncio.run(route_node(state))
    assert update["needs_clarification"] is False
    assert after_route({**state, **update}) == "plan"


def test_dspy_output_parsers_normalize_typed_and_string_values():
    from jarvis.dspy_engine.modules import parse_bool, parse_list

    assert parse_bool(False) is False
    assert parse_bool("False") is False
    assert parse_bool("yes") is True
    assert parse_bool("unknown", default=False) is False
    assert parse_list(["research", "desktop"]) == ["research", "desktop"]
    assert parse_list("['research', 'desktop']") == ["research", "desktop"]
    assert parse_list("research; desktop") == ["research", "desktop"]


def test_router_rejects_invalid_workers_and_logs_keyword_fallback(monkeypatch, caplog):
    from jarvis.dspy_engine import modules

    bad_prediction = SimpleNamespace(
        workers=["desktop", "not-a-worker"], reason="bad output", needs_clarification=False
    )
    monkeypatch.setattr(modules, "is_offline", lambda: False)
    monkeypatch.setattr(modules, "PROVIDER", "test")
    monkeypatch.setattr(modules, "get_module", lambda _name: lambda **_kwargs: bad_prediction)
    monkeypatch.setattr(modules, "_safe", lambda call, _default, **_kwargs: call())

    request = "Please search for the latest weather"
    with caplog.at_level("WARNING", logger="jarvis.dspy_engine.modules"):
        result = modules.route(request, session_id="invalid-route-fallback")

    assert result["workers"] == modules.fallback_route(request)
    assert "unsupported workers" in caplog.text


def test_dspy_route_and_plan_are_cached_once_per_session_request(monkeypatch):
    from jarvis.dspy_engine import modules

    counts = {"router": 0, "planner": 0}

    def router(**_kwargs):
        counts["router"] += 1
        return SimpleNamespace(workers=["answer"], reason="clear", needs_clarification=False)

    def planner(**_kwargs):
        counts["planner"] += 1
        return SimpleNamespace(steps=["answer"], deliverable="answer")

    monkeypatch.setattr(modules, "is_offline", lambda: False)
    monkeypatch.setattr(modules, "PROVIDER", "test")
    monkeypatch.setattr(modules, "get_module", lambda name: {"router": router, "planner": planner}[name])
    monkeypatch.setattr(modules, "_safe", lambda call, _default, **_kwargs: call())

    assert modules.route("same request", session_id="cache-session") == modules.route(
        "same request", session_id="cache-session"
    )
    assert modules.plan("same request", ["answer"], session_id="cache-session") == modules.plan(
        "same request", ["answer"], session_id="cache-session"
    )
    assert counts == {"router": 1, "planner": 1}

    # Same request in another session, or another request in this session, gets a separate entry.
    modules.route("same request", session_id="other-cache-session")
    modules.plan("same request", ["answer"], session_id="other-cache-session")
    modules.route("different request", session_id="cache-session")
    modules.plan("different request", ["answer"], session_id="cache-session")
    assert counts == {"router": 3, "planner": 3}


def test_compiled_dspy_loader_uses_typed_signatures_and_matching_modules(monkeypatch, tmp_path):
    import dspy

    from jarvis.dspy_engine import optimize
    from jarvis.dspy_engine.signatures import MakePlan, RouteIntent

    class FakeProgram:
        def __init__(self, kind, signature):
            self.kind = kind
            self.signature = signature

        def load(self, _path):
            return None

        def __call__(self, **_kwargs):
            return SimpleNamespace(workers=["answer"], needs_clarification="False")

    class FakeOptimizer:
        def __init__(self, **_kwargs):
            pass

        def compile(self, program, **_kwargs):
            return program

    monkeypatch.setattr(optimize, "OPTIMIZED_DIR", tmp_path)
    monkeypatch.setattr(optimize, "build_examples", lambda: [SimpleNamespace()])
    monkeypatch.setattr(dspy, "Predict", lambda signature: FakeProgram("Predict", signature))
    monkeypatch.setattr(dspy, "ChainOfThought", lambda signature: FakeProgram("ChainOfThought", signature))
    monkeypatch.setattr(dspy, "BootstrapFewShot", lambda **kwargs: FakeOptimizer())
    (tmp_path / "router.json").write_text("{}", encoding="utf-8")
    (tmp_path / "planner.json").write_text("{}", encoding="utf-8")

    compiled_router = optimize.compile_router()
    compiled_planner = optimize.compile_planner()
    loaded_router = optimize.load_compiled("router")
    loaded_planner = optimize.load_compiled("planner")
    assert compiled_router.signature is RouteIntent
    assert compiled_planner.signature is MakePlan
    assert loaded_router is not None and loaded_router.signature is RouteIntent
    assert loaded_planner is not None and loaded_planner.kind == "ChainOfThought" and loaded_planner.signature is MakePlan


def test_compiled_router_smoke_test_discards_artifact_with_excess_clarification(monkeypatch, tmp_path):
    import dspy

    from jarvis.dspy_engine import optimize

    calls = 0

    class FakeProgram:
        def __init__(self, _signature):
            pass

        def load(self, _path):
            return None

        def __call__(self, **_kwargs):
            nonlocal calls
            calls += 1
            return SimpleNamespace(
                workers=["answer"], needs_clarification="True" if calls <= 3 else "False"
            )

    monkeypatch.setattr(optimize, "OPTIMIZED_DIR", tmp_path)
    monkeypatch.setattr(optimize, "is_offline", lambda: False)
    monkeypatch.setattr(optimize, "PROVIDER", "test")
    monkeypatch.setattr(dspy, "Predict", FakeProgram)
    (tmp_path / "router.json").write_text("{}", encoding="utf-8")

    assert optimize.load_compiled("router") is None
    assert calls == 5


def test_refine_signature_is_stable_when_only_the_local_clock_changes(monkeypatch):
    from jarvis.graph import state as graph_state
    from jarvis.graph.nodes import _execution_signature

    clock_values = iter(("Monday epoch: 100", "Monday epoch: 101"))
    monkeypatch.setattr(graph_state, "local_clock_line", lambda: next(clock_values))
    state = {
        "session_id": "signature-test",
        "tool_evidence": [{
            "worker": "desktop", "name": "open_app", "args": {"app": "Notepad"}, "ok": True,
        }],
    }
    assert _execution_signature(state) == _execution_signature(state)


def test_real_graph_refine_retries_only_failed_worker_and_runs_successful_tools_once(monkeypatch):
    from jarvis.graph import build, routing
    from jarvis.graph.idempotency import completed_action, remember_completed_action
    from jarvis.graph.state import initial_state

    worker_calls = {"desktop": 0, "research": 0, "critic": 0}
    tool_attempts = {"desktop.open_app": 0, "research.fetch": 0}
    successful_tool_invocations = {"desktop.open_app": 0, "research.fetch": 0}
    original_request = "run one desktop action and one research action"

    async def invoke_fake_tool(ledger, worker_name, tool_name, args, *, succeeds):
        cached = completed_action(ledger, tool_name, args)
        if cached:
            record = dict(cached)
            record["name"] = tool_name
            return record
        tool_attempts[tool_name] += 1
        output = "verified result" if succeeds else "temporary failure"
        record = {
            "worker": worker_name, "name": tool_name, "args": args,
            "output": output, "ok": succeeds,
        }
        if succeeds:
            successful_tool_invocations[tool_name] += 1
            remember_completed_action(
                ledger, tool_name, args, {"ok": True, "output": output},
                output=output, worker=worker_name,
            )
        return record

    def make_worker(worker_name: str):
        async def worker(state):
            worker_calls[worker_name] += 1
            ledger = dict(state.get("done_actions", {}) or {})
            tool_name = "desktop.open_app" if worker_name == "desktop" else "research.fetch"
            succeeds = worker_name == "desktop" or worker_calls[worker_name] > 1
            record = await invoke_fake_tool(
                ledger, worker_name, tool_name, {"request": original_request}, succeeds=succeeds
            )
            update = {
                "findings": [{"worker": worker_name, "summary": f"{worker_name} result"}],
                "tool_evidence": [record],
                "done_actions": ledger,
            }
            if not succeeds:
                update["errors"] = [f"{worker_name}: temporary failure"]
            return update

        worker.__name__ = f"{worker_name}_worker"
        return worker

    workers = {"desktop": make_worker("desktop"), "research": make_worker("research")}
    monkeypatch.setattr(routing, "WORKER_NODES", workers)
    monkeypatch.setattr(build, "WORKER_NODES", workers)
    monkeypatch.setattr(build, "USE_LANGGRAPH_CHECKPOINTER", False)

    async def recall(_state):
        return {}

    async def route(_state):
        return {"workers": ["desktop", "research"], "needs_clarification": False}

    async def plan(_state):
        return {"plan": ["run both workers"]}

    async def synthesize(_state):
        return {"answer": "Both actions have evidence.", "evidence": "verified evidence"}

    async def critic(_state):
        worker_calls["critic"] += 1
        return {
            "critique": {"ok": worker_calls["critic"] > 1, "score": 0.9, "missing": []},
            "fix_hint": "re-check the evidence",
        }

    async def persist(_state):
        return {"memory_persisted": True}

    async def respond(state):
        return {"status": "done", "spoken": state.get("answer", "")}

    for name, fn in {
        "recall_node": recall,
        "route_node": route,
        "plan_node": plan,
        "synthesize_node": synthesize,
        "critic_node": critic,
        "persist_node": persist,
        "respond_node": respond,
    }.items():
        fn.__name__ = name
        monkeypatch.setattr(build, name, fn)

    graph = build.build_jarvis_graph(checkpointer=None)
    result = asyncio.run(
        graph.ainvoke(
            initial_state(original_request, session_id="part-a-graph"),
            config={"configurable": {"thread_id": "part-a-graph"}},
        )
    )

    assert worker_calls == {"desktop": 1, "research": 2, "critic": 2}
    assert tool_attempts == {"desktop.open_app": 1, "research.fetch": 2}
    assert successful_tool_invocations == {"desktop.open_app": 1, "research.fetch": 1}
    assert result["request"] == original_request
    assert result["iterations"] == 1


def test_research_worker_reuses_successful_fetch_from_action_ledger(monkeypatch):
    from jarvis.dspy_engine import modules
    from jarvis.graph.nodes import research_worker
    from jarvis.graph.state import initial_state
    from jarvis.tools import web_tools

    calls = []

    async def fake_fetch(url, max_chars=10000):
        calls.append((url, max_chars))
        return "Verified page content"

    async def fake_extract_many(pairs, fields):
        return []

    monkeypatch.setattr(web_tools, "fetch_url_text", fake_fetch)
    monkeypatch.setattr(modules, "extract_many", fake_extract_many)

    state = initial_state("Read https://example.com/report")
    first = asyncio.run(research_worker(state))
    second = asyncio.run(research_worker({**state, "done_actions": first["done_actions"]}))

    assert calls == [("https://example.com/report", 10000)]
    assert second["done_actions"] == first["done_actions"]


def test_refine_loop_guard_stops_before_worker_dispatch():
    from jarvis.graph.nodes import _execution_signature, refine_dispatch, refine_node
    from jarvis.graph.state import initial_state

    state = initial_state("already-seen evidence", session_id="loop-guard")
    state["workers"] = ["desktop"]
    state["worker_results"] = {"desktop": {"ok": False}}
    state["tool_evidence"] = [{
        "worker": "desktop", "name": "open_app", "args": {"app": "Notepad"}, "ok": False,
    }]
    state["refine_signatures"] = [_execution_signature(state)]

    update = asyncio.run(refine_node(state))
    stopped = {**state, **update}
    assert update["refine_stop"] is True
    assert refine_dispatch(stopped) == "synthesize"


def test_refine_dispatch_selects_only_failed_or_missing_workers():
    from jarvis.graph.nodes import refine_dispatch, refine_node
    from jarvis.graph.state import append_or_replace, initial_state

    request = "keep the original request"
    state = initial_state(request, session_id="failed-worker-only")
    state.update({
        "workers": ["desktop", "research"],
        "worker_results": {"desktop": {"ok": True}, "research": {"ok": False}},
        "findings": [
            {"worker": "desktop", "summary": "done"},
            {"worker": "research", "summary": "temporary error"},
        ],
        "tool_evidence": [
            {"worker": "desktop", "name": "open_app", "args": {"app": "Notepad"}, "ok": True},
            {"worker": "research", "name": "web_search", "args": {"query": "x"}, "ok": False},
        ],
        "errors": ["research: temporary error"],
        "fix_hint": "retry research only",
    })

    update = asyncio.run(refine_node(state))
    assert update["retry_workers"] == ["research"]
    assert "request" not in update
    assert append_or_replace(state["findings"], update["findings"]) == [
        {"worker": "desktop", "summary": "done"}
    ]
    assert append_or_replace(state["errors"], update["errors"]) == []
    refined_state = {**state, **update}
    refined_state["iterations"] = update["iterations"]
    assert [send.node for send in refine_dispatch(refined_state)] == ["research_worker"]


def test_refine_does_not_retry_when_a_human_must_unblock_the_task():
    from jarvis.graph.nodes import refine_dispatch, refine_node
    from jarvis.graph.state import initial_state

    state = initial_state("open a protected resource", session_id="human-retry")
    state.update({
        "workers": ["desktop"],
        "worker_results": {"desktop": {"ok": False}},
        "blocked_tools": ["open_app"],
        "errors": ["manual confirmation required"],
    })
    update = asyncio.run(refine_node(state))
    assert update["refine_stop"] is True
    assert refine_dispatch({**state, **update}) == "synthesize"


def test_desktop_action_ledger_skips_success_and_retries_only_failure():
    from jarvis.graph.idempotency import action_idempotency_key
    from jarvis.graph.nodes import _execute_desktop_action

    class Tool:
        def __init__(self, name, responses):
            self.name = name
            self.responses = iter(responses)
            self.calls = 0

        async def ainvoke(self, _args):
            self.calls += 1
            return next(self.responses)

    open_tool = Tool("open_any_app", ["Opened Notepad"])
    type_tool = Tool("type_text", ["error: temporary failure", "Typed hello"])
    tools = [open_tool, type_tool]
    actions = {"actions": [
        {"kind": "open_app", "app": "Notepad"},
        {"kind": "type_text", "text": "hello", "press_enter": False},
    ]}
    ledger = {}

    first = asyncio.run(_execute_desktop_action(actions, tools, done_actions=ledger))
    second = asyncio.run(_execute_desktop_action(actions, tools, done_actions=ledger))

    assert [record["ok"] for record in first] == [True, False]
    assert [record["ok"] for record in second] == [True, True]
    assert open_tool.calls == 1
    assert type_tool.calls == 2
    assert action_idempotency_key("type_text", {"text": "hello", "press_enter": False}) in ledger


def test_two_parallel_workers_can_reduce_execution_flags_and_status():
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Send

    from jarvis.graph.state import JarvisState, initial_state

    async def worker_a(_state):
        return {"execution_required": True, "action_required": True, "status": "failed"}

    async def worker_b(_state):
        return {"execution_required": False, "action_required": False, "status": ""}

    graph_builder = StateGraph(JarvisState)
    graph_builder.add_node("fanout", lambda _state: {})
    graph_builder.add_node("worker_a", worker_a)
    graph_builder.add_node("worker_b", worker_b)
    graph_builder.add_edge(START, "fanout")
    graph_builder.add_conditional_edges(
        "fanout",
        lambda _state: [Send("worker_a", {}), Send("worker_b", {})],
        ["worker_a", "worker_b"],
    )
    graph_builder.add_edge("worker_a", END)
    graph_builder.add_edge("worker_b", END)
    graph = graph_builder.compile()

    result = asyncio.run(graph.ainvoke(initial_state("two worker test")))
    assert result["execution_required"] is True
    assert result["action_required"] is True
    assert result["status"] == "failed"


def test_extract_urls_normalizes_and_deduplicates_targets():
    from jarvis.graph.nodes import _extract_urls

    urls = _extract_urls("Compare https://x.com/a with x.com")
    assert urls == ["https://x.com/a"]
    canonical = _extract_urls("https://EXAMPLE.com:443/a/../b/ and https://example.com/b")
    assert canonical == ["https://example.com/b"]


def test_daily_automation_reenable_uses_next_scheduled_slot_and_coalesces_downtime(tmp_path):
    import time

    from jarvis.automation.scheduler import AutomationStore

    store = AutomationStore(tmp_path / "automations.json")
    created = store.create("daily check", "daily 08:00")
    assert created["ok"]
    auto = store.get(created["automation"]["id"])
    assert auto is not None

    now = time.time()
    auto.next_run = now - 5 * 24 * 60 * 60
    assert store.set_enabled(auto.id, False)["ok"]
    assert store.set_enabled(auto.id, True)["ok"]
    from jarvis.automation.scheduler import _from_timestamp

    reenabled = _from_timestamp(auto.next_run)
    assert auto.next_run > now
    assert (reenabled.hour, reenabled.minute) == (8, 0)

    missed_slot = now - 5 * 24 * 60 * 60
    auto.next_run = missed_slot
    store.mark_run(auto, "coalesced run", now=now, fired_slot=missed_slot)
    assert auto.next_run > now
    assert auto.last_fired_slot == missed_slot


def test_missed_daily_run_is_coalesced_to_the_next_future_slot(tmp_path):
    import time

    from jarvis.automation.scheduler import Automation, AutomationStore

    store = AutomationStore(tmp_path / "automations.json")
    now = time.time()
    auto = Automation(
        id="daily12345678", task="daily check", schedule="daily 08:00", kind="daily",
        next_run=now - 5 * 24 * 60 * 60, enabled=True,
    )
    store._items[auto.id] = auto
    store.mark_run(auto, "coalesced run")
    assert auto.next_run > time.time()


def test_scheduler_marks_due_job_when_runner_is_missing(tmp_path, monkeypatch):
    import time

    from jarvis.automation import scheduler
    from jarvis.automation.scheduler import Automation, AutomationStore

    store = AutomationStore(tmp_path / "automations.json")
    now = time.time()
    auto = Automation(
        id="once12345678", task="one shot", schedule="once", kind="once",
        next_run=now - 10, enabled=True,
    )
    store._items[auto.id] = auto
    monkeypatch.setattr(scheduler, "_STORE", store)
    monkeypatch.setattr(scheduler, "RUNNER", None)

    scheduler._execute(auto)
    assert auto.runs == 1
    assert auto.enabled is False
    assert not store.due(now=now + 1)


def test_cli_does_not_retry_an_untyped_runtime_error_for_automation(monkeypatch):
    from contextlib import nullcontext

    from jarvis.automation import scheduler
    from jarvis.cli import app, pro_ui, ui
    from jarvis.runtime.agent import AgentResult

    calls = 0

    class Dashboard:
        def __init__(self, *_args, **_kwargs):
            self.on_event = lambda _event: None

    async def fake_run_agent(task, **_kwargs):
        nonlocal calls
        if task == "bootstrap cli":
            return AgentResult(request=task, answer="ready", status="done")
        calls += 1
        raise RuntimeError("all models failed")

    monkeypatch.setattr(scheduler, "RUNNER", scheduler.RUNNER)
    monkeypatch.setattr(app, "run_agent", fake_run_agent)
    monkeypatch.setattr(app, "_header_meta", lambda: {})
    monkeypatch.setattr(pro_ui, "Dashboard", Dashboard)
    monkeypatch.setattr(pro_ui, "live_run", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(scheduler, "start_background", lambda: False)
    monkeypatch.setattr(ui, "answer_panel", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui, "error_panel", lambda *_args, **_kwargs: None)

    asyncio.run(app.run_cli(once="bootstrap cli", show_reasoning=False))
    runner = scheduler.RUNNER
    assert runner is not None
    try:
        runner("automation task", "auto12345678", "run-once")
    except RuntimeError as exc:
        assert "all models failed" in str(exc)
    else:
        raise AssertionError("the simulated runner failure should propagate")

    assert calls == 1


def test_cli_retries_only_typed_transient_errors_and_closes_each_loop():
    from jarvis.automation.scheduler import TransientError
    from jarvis.cli.app import _run_automation_sync

    attempts = []
    loops = []
    run_ids = []

    async def one_attempt(run_id):
        loops.append(asyncio.get_running_loop())
        run_ids.append(run_id)
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise TransientError("temporary network outage")
        return "completed"

    assert _run_automation_sync(lambda: one_attempt("auto:slot-123")) == "completed"
    assert attempts == [1, 2]
    assert run_ids == ["auto:slot-123", "auto:slot-123"]
    assert all(loop.is_closed() for loop in loops)

    async def call_from_an_existing_loop():
        outer_loop = asyncio.get_running_loop()
        inner_loops = []

        async def nested_work():
            inner_loops.append(asyncio.get_running_loop())
            return "completed from worker thread"

        assert _run_automation_sync(lambda: nested_work()) == "completed from worker thread"
        assert outer_loop.is_running()
        assert len(inner_loops) == 1 and inner_loops[0].is_closed()

    asyncio.run(call_from_an_existing_loop())


def test_scheduled_slot_is_claimed_once_and_runner_receives_run_id(tmp_path, monkeypatch):
    import time

    from jarvis.automation import scheduler
    from jarvis.automation.scheduler import Automation, AutomationStore

    store = AutomationStore(tmp_path / "automations.json")
    now = time.time()
    auto = Automation(
        id="daily-run-id", task="check once", schedule="daily 08:00", kind="daily",
        next_run=now - 1, enabled=True,
    )
    store._items[auto.id] = auto
    calls = []
    monkeypatch.setattr(scheduler, "_STORE", store)
    monkeypatch.setattr(scheduler, "RUNNER", lambda task, auto_id, run_id: calls.append((task, auto_id, run_id)) or "ok")

    scheduler._execute(auto, scheduled=True, now=now)
    scheduler._execute(auto, scheduled=True, now=now)

    assert len(calls) == 1
    assert calls[0][0:2] == ("check once", auto.id)
    assert calls[0][2].startswith(f"{auto.id}:")
    assert auto.last_fired_slot <= auto.last_run
    assert auto.next_run > now


def test_run_agent_propagates_and_serializes_scheduler_run_id(monkeypatch):
    from jarvis.runtime import agent

    received = []

    async def fake_inner(*args):
        received.append(args[-1])
        return agent.AgentResult(request=args[0], answer="done")

    monkeypatch.setattr(agent, "_run_agent_inner", fake_inner)
    result = asyncio.run(agent.run_agent("scheduled request", run_id="auto:slot-42"))

    assert received == ["auto:slot-42"]
    assert result.run_id == "auto:slot-42"
    assert result.as_dict()["run_id"] == "auto:slot-42"


def test_default_graph_checkpoint_is_evicted_after_one_shot_turn(monkeypatch):
    from jarvis.graph import build

    removed = []

    class Saver:
        def delete_thread(self, thread_id):
            removed.append(thread_id)

    class Graph:
        async def ainvoke(self, state, config):
            assert config["configurable"]["thread_id"] == "stable-session"
            return state

    monkeypatch.setattr(build, "get_jarvis_graph", lambda: Graph())
    monkeypatch.setattr(build, "_CHECKPOINTER", Saver())

    result = asyncio.run(build.jarvis_ainvoke(
        "one turn", session_id="stable-session", run_id="scheduled-run-1"
    ))
    assert removed == ["stable-session"]
    assert result["run_id"] == "scheduled-run-1"


def test_default_graph_thread_is_stable_per_session():
    from jarvis.graph.build import _thread

    assert _thread("same-session", None) == "same-session"
    assert _thread("same-session", "explicit-thread") == "explicit-thread"


def test_refine_hint_and_original_request_reach_persist_node(monkeypatch):
    from jarvis.graph import refine, synthesis
    from jarvis.graph.state import initial_state

    original_request = "Find the official maintenance schedule"
    hint = "retry research with an official source"
    saved_turns = []

    class Memory:
        def add_turn(self, request, answer, *, session):
            saved_turns.append((request, answer, session))

    class SessionState:
        def finish_task(self, **_kwargs):
            return None

        def set_pending_steps(self, _steps):
            return None

    monkeypatch.setattr(synthesis, "get_memory", lambda: Memory())
    monkeypatch.setattr(synthesis, "get_state", lambda: SessionState())
    state = initial_state(original_request, session_id="refine-original-request")
    state.update({
        "workers": ["research"],
        "worker_results": {"research": {"ok": False}},
        "findings": [{"worker": "research", "summary": "stale failure"}],
        "fix_hint": hint,
    })

    update = asyncio.run(refine.refine_node(state))
    assert update["refine_hint"] == hint
    assert "request" not in update
    assert state["request"] == original_request

    refined_state = {**state, **update, "answer": "Schedule found", "status": "done"}
    asyncio.run(synthesis.persist_node(refined_state))
    assert saved_turns == [(original_request, "Schedule found", "refine-original-request")]


def test_refine_max_iteration_dispatches_to_synthesize_not_worker(monkeypatch):
    from jarvis.graph import refine
    from jarvis.graph.state import initial_state

    monkeypatch.setattr(refine, "MAX_AUTO_REFINE_PASSES", 1)
    monkeypatch.setattr(refine, "MAX_GRAPH_ITERATIONS", 3)
    state = initial_state("retry a failed desktop action", session_id="max-refine-dispatch")
    state.update({
        "iterations": 1,
        "refine_stop": False,
        "workers": ["desktop"],
        "retry_workers": ["desktop"],
        "worker_results": {"desktop": {"ok": False}},
    })

    update = asyncio.run(refine.refine_node(state))
    assert update["refine_stop"] is True
    assert update["iterations"] == 1
    assert refine.refine_dispatch({**state, **update}) == "synthesize"
