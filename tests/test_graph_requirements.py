from __future__ import annotations

import json

import pytest

from jarvis.graph.state import initial_state
from jarvis.graph.verification import decompose_requirements, evaluate_requirements, finalize_requirements


def _two_step_state():
    state = initial_state("Open Notepad and type hello", session_id="requirements")
    state["workers"] = ["desktop"]
    state["plan"] = ["Open Notepad", "Type hello"]
    state["action_required"] = True
    state["execution_required"] = True
    state["requirements"] = decompose_requirements(state["request"], state["plan"], state["workers"])
    state["tool_evidence"] = [
        {"worker": "desktop", "name": "open_any_app", "args": {"query": "Notepad"},
         "output": "Notepad active window verified", "ok": True},
        {"worker": "desktop", "name": "type_text", "args": {"text": "hello"},
         "output": "keyboard backend refused text input", "ok": False},
    ]
    return state


def test_planner_reporting_step_is_not_left_as_an_unverifiable_tool_requirement():
    requirements = decompose_requirements(
        "Save the requested note",
        [
            "Save the requested note",
            "results ko verify karo aur owner ko Hinglish me short summary do",
        ],
        ["desktop"],
    )
    assert len(requirements) == 1
    assert requirements["R1"]["requirement"] == "Save the requested note"


def test_requests_decompose_and_each_requirement_gets_evidence_or_failure():
    state = _two_step_state()
    assert list(state["requirements"]) == ["R1", "R2"]
    statuses = evaluate_requirements(state)
    assert statuses["R1"]["status"] == "done"
    assert statuses["R1"]["evidence"]
    assert statuses["R2"]["status"] == "failed"
    assert "refused" in statuses["R2"]["reason"]

    final, status, note = finalize_requirements(state, "Everything is done!")
    assert final["R1"]["status"] == "done"
    assert final["R2"]["status"] == "failed"
    assert status == "partial_failure"
    assert "R2" in note and "Unresolved" in note


@pytest.mark.asyncio
async def test_graph_verification_blocks_done_and_refine_retries_only_unresolved_item():
    from jarvis.graph.critic import after_critic
    from jarvis.graph.refine import refine_node
    from jarvis.graph.verification import verification_node

    state = _two_step_state()
    state["worker_results"] = {"desktop": {"ok": True}}
    verified = await verification_node(state)
    assert verified["requirements_unresolved"] is True
    assert verified["requirements"]["R1"]["status"] == "done"
    assert verified["requirements"]["R2"]["status"] == "failed"

    critic_state = {**state, **verified, "critique": {"ok": True}, "iterations": 0}
    assert after_critic(critic_state) == "refine"
    retry = await refine_node(critic_state)
    assert retry["retry_workers"] == ["desktop"]
    assert "R2 Type hello" in retry["refine_hint"]
    assert retry["iterations"] == 1
    kept_evidence = retry["tool_evidence"][0]["items"]
    assert all(record.get("ok") for record in kept_evidence)


@pytest.mark.asyncio
async def test_blocked_requirement_becomes_needs_user_not_done():
    from jarvis.graph.verification import verification_node

    state = _two_step_state()
    state["blocked_tools"] = ["desktop_type"]
    state["status"] = "blocked"
    update = await verification_node(state)
    assert update["requirements_unresolved"] is True
    assert all(item["status"] == "needs_user" for item in update["requirements"].values())
    assert all(item["question"] for item in update["requirements"].values())


@pytest.mark.asyncio
async def test_synthesis_headline_matches_partial_requirement_status(monkeypatch):
    from jarvis.graph import synthesis

    monkeypatch.setattr(synthesis, "is_offline", lambda: True)
    monkeypatch.setattr(synthesis, "PROVIDER", "demo")
    state = initial_state("Open Notepad and type hello", session_id="honest-synthesis")
    state.update(
        workers=["desktop"],
        plan=["Open Notepad", "Type hello"],
        action_required=True,
        execution_required=True,
        requirements=decompose_requirements("Open Notepad and type hello", ["Open Notepad", "Type hello"],
                                           ["desktop"]),
        tool_evidence=[{
            "worker": "desktop", "name": "open_any_app", "args": {"query": "Notepad"},
            "output": "✅ Notepad focused", "ok": True,
        }],
        actions_done=[{
            "worker": "desktop", "name": "open_any_app", "args": {"query": "Notepad"},
            "output": "✅ Notepad focused", "ok": True,
        }],
        findings=[{
            "worker": "desktop", "summary": "Notepad focused; typing was not attempted.",
            "tool_calls": [{
                "worker": "desktop", "name": "open_any_app", "args": {"query": "Notepad"},
                "output": "✅ Notepad focused", "ok": True,
            }],
        }],
    )

    result = await synthesis.synthesize_node(state)
    assert result["status"] == "partial_failure"
    assert "Verified ✅" not in result["answer"]
    assert "Partial completion" in result["answer"]
    assert "Unresolved — not marked done" in result["answer"]


@pytest.mark.asyncio
async def test_requirement_statuses_persist_with_the_task_across_steps(tmp_path, monkeypatch):
    from jarvis.graph import synthesis
    from jarvis.memory.store import MemoryStore

    store = MemoryStore(tmp_path / "tasks.sqlite3", user_id="owner")
    task_id = store.start_task("Open Notepad and type hello")
    state = _two_step_state()
    state["task_id"] = task_id
    state["answer"] = "Notepad opened; typing failed."
    state["status"] = "partial_failure"
    state["requirements"] = evaluate_requirements(state)

    class StateStub:
        def finish_task(self, **_kwargs):
            return None

        def set_pending_steps(self, _steps):
            return None

    monkeypatch.setattr(synthesis, "get_memory", lambda: store)
    monkeypatch.setattr(synthesis, "get_state", lambda: StateStub())
    persisted = await synthesis.persist_node(state)
    row = next(task for task in store.tasks() if task["id"] == task_id)
    requirements = json.loads(row["requirements"])
    assert persisted["memory_persisted"]
    assert requirements["R1"]["status"] == "done"
    assert requirements["R2"]["status"] == "failed"
    assert row["status"] == "partial_failure"
