"""Recall, route, plan, and graph fan-out helpers."""
from __future__ import annotations

import asyncio
from typing import Any

from langgraph.types import Send

from ..dspy_engine import modules as dspy_mods
from ..memory.session_state import get_state
from ..memory.store import get_memory
from ..runtime.progress import emit
from ..skills.loader import skills_prompt_block
from .state import JarvisState
from .workers import WORKER_NODES
from .workers.common import _event


async def recall_node(state: JarvisState) -> dict[str, Any]:
    request = state.get("request", "")
    mem = get_memory()
    try:
        ctx = mem.relevant_context(request, token_budget=850, limit=8,
                                  session=state.get("session_id"), recent_limit=4)
        facts = ""
        # Local place is useful for explicit weather/nearby requests, never injected by default.
        if any(word in request.lower() for word in ("weather", "mausam", "near me", "nearby", "nearby")):
            locations = [row for row in mem.facts(limit=30)
                         if row.get("key") == "location" and row.get("sensitivity") != "sensitive"]
            if locations:
                facts = "Relevant local preference: " + str(locations[0].get("value", ""))[:160]
    except Exception as exc:
        facts, ctx = f"(memory error: {exc})", ""

    # The final persist node writes the complete turn once (user + assistant episodes).
    # Do not insert a second user episode here; current request is already in graph state.

    # Short-term task row is created once by runtime before the graph starts.
    short_term = ""
    try:
        short_term = get_state().context_block(max_tasks=4)
    except Exception:
        short_term = ""

    return {
        "owner_facts": facts,
        "memory_context": f"{ctx}\n\n{facts}\n\nShort-term state:\n{short_term}",
        "events": [_event(state, "recall", f"{len(ctx)} chars hybrid memory context")],
    }

async def route_node(state: JarvisState) -> dict[str, Any]:
    request = state.get("request", "")
    history = state.get("memory_context", "")
    decision = await asyncio.to_thread(
        dspy_mods.route, request, history, session_id=state.get("session_id", "default")
    )
    workers = decision.get("workers") or ["answer"]
    return {
        "workers": workers,
        "route_reason": decision.get("reason", ""),
        "needs_clarification": dspy_mods.parse_bool(decision.get("needs_clarification")),
        "events": [_event(state, "route", f"workers={workers} • {decision.get('reason', '')[:120]}")],
    }

async def clarify_node(state: JarvisState) -> dict[str, Any]:
    """Ambiguous request → owner se ek short clarifying sawaal (guess karne se behtar)."""
    request = state.get("request", "")
    answer = (
        f"बॉस, ek chhoti si clarification chahiye — _{request.strip()}_\n\n"
        "Kis cheez ka exact output chahiye (file/note/json/answer), aur kaunsi sites/inputs use karun? "
        "Bata dijiye, main turant chala deta hoon. 🙌"
    )
    return {
        "answer": answer,
        "spoken": answer,
        "status": "needs_clarification",
        "events": [_event(state, "clarify", "owner se clarification maangi")],
    }

async def plan_node(state: JarvisState) -> dict[str, Any]:
    from .verification import decompose_requirements

    request = state.get("request", "")
    workers = state.get("workers", [])
    skills = ""
    try:
        skills = skills_prompt_block(request)  # owner ke bataye workflows (SKILL.md)
    except Exception:
        skills = ""
    context = f"{state.get('owner_facts', '')}\n{state.get('memory_context', '')}"
    from ..time_intent import needs_clock_context

    if needs_clock_context(request):
        from .state import local_clock_line

        context += f"\n\nLocal clock for this explicit scheduling/date request: {local_clock_line()}"
    if skills:
        context += f"\n\n{skills}"
    task_id = get_memory().start_task(request, route=",".join(workers))
    if set(workers or []) == {"answer"} and not skills:
        # One direct answer does not need a separate planner model call.
        try:
            get_state().set_pending_steps([])
        except Exception:
            pass
        emit("thinking", "Single-answer request; skipped multi-step planning")
        requirements = decompose_requirements(request, [], workers)
        return {
            "plan": [], "deliverable": "direct answer", "task_id": task_id,
            "requirements": requirements,
            "events": [_event(state, "plan", "planning skipped: answer-only route")],
        }
    emit("thinking", f"Plan bana raha hoon — workers: {', '.join(workers) or 'default'}")
    result = await asyncio.to_thread(
        dspy_mods.plan, request, workers, context[:6000], session_id=state.get("session_id", "default")
    )
    steps = result.get("steps") or []
    if steps:
        emit("plan", f"{len(steps)}-step plan ready", detail=" → ".join(str(x)[:60] for x in steps[:5]))
    try:
        get_state().set_pending_steps(steps)
    except Exception:
        pass
    return {
        "plan": steps,
        "requirements": decompose_requirements(request, steps, workers),
        "deliverable": result.get("deliverable", ""),
        "task_id": task_id,
        "events": [_event(state, "plan", f"{len(steps)} steps" + (" +skill" if skills else ""), steps=steps)],
    }

def after_route(state: JarvisState) -> str:
    if state.get("needs_clarification"):
        return "clarify"
    return "plan"

def _worker_sends(state: JarvisState, worker_names: list[str]) -> Any:

    workers = state.get("workers", []) or ["answer"]
    real_workers = [worker for worker in worker_names if worker in WORKER_NODES]
    if not real_workers:
        return "verification"
    payload = {
        "request": state.get("request", ""),
        "plan": state.get("plan", []),
        "workers": workers,
        "session_id": state.get("session_id", "default"),
        "run_id": state.get("run_id", ""),
        "surface": state.get("surface", "cli_owner"),
        "memory_context": state.get("memory_context", ""),
        "owner_facts": state.get("owner_facts", ""),
        "iterations": state.get("iterations", 0),
        "refine_hint": state.get("refine_hint", ""),
        "done_actions": state.get("done_actions", {}) or {},
        "worker_results": state.get("worker_results", {}) or {},
        "requirements": state.get("requirements", {}) or {},
    }
    return [Send(WORKER_NODES[worker].__name__, payload) for worker in real_workers]

def dispatch(state: JarvisState) -> Any:
    """Initial worker fan-out (all routed workers, once)."""
    workers = state.get("workers", []) or ["answer"]
    return _worker_sends(state, workers)
