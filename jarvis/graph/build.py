"""
jarvis/graph/build.py — Jarvis ka LangGraph supervisor compile karo
==================================================================

    graph = build_jarvis_graph()          # compiled LangGraph
    result = await jarvis_ainvoke("...")  # one-shot run
    async for ev in jarvis_stream("..."): # live events (UI/voice)
        ...

Features:
  * parallel worker fan-out (Send API) — browser + research + code ek saath
  * stable per-session checkpointer thread — implicit per-turn snapshots are evicted on completion
  * error isolation — ek worker fail ho to poora graph nahi girta
  * mermaid diagram (`graph_mermaid()`) — docs/preview ke liye
"""
from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

from langgraph.graph import END, START, StateGraph

from ..config import MAX_AUTO_REFINE_PASSES, MAX_GRAPH_ITERATIONS, USE_LANGGRAPH_CHECKPOINTER
from .nodes import (
    WORKER_NODES,
    after_critic,
    after_route,
    clarify_node,
    critic_node,
    dispatch,
    persist_node,
    plan_node,
    recall_node,
    refine_dispatch,
    refine_node,
    respond_node,
    route_node,
    synthesize_node,
    verification_node,
)
from .state import JarvisState, initial_state

_GRAPH = None
_CHECKPOINTER = None
_SESSION_LOCKS: OrderedDict[str, threading.Lock] = OrderedDict()
_SESSION_LOCKS_LOCK = threading.RLock()
_MAX_SESSION_LOCKS = 256


def _session_lock(thread_id: str) -> threading.Lock:
    with _SESSION_LOCKS_LOCK:
        lock = _SESSION_LOCKS.get(thread_id)
        if lock is None:
            for old_id, old_lock in list(_SESSION_LOCKS.items()):
                if len(_SESSION_LOCKS) < _MAX_SESSION_LOCKS:
                    break
                if not old_lock.locked():
                    _SESSION_LOCKS.pop(old_id, None)
            lock = threading.Lock()
            _SESSION_LOCKS[thread_id] = lock
        _SESSION_LOCKS.move_to_end(thread_id)
        return lock


def _cleanup_checkpoint(thread_id: str) -> None:
    delete_thread = getattr(_CHECKPOINTER, "delete_thread", None)
    if callable(delete_thread):
        try:
            delete_thread(thread_id)
        except Exception:
            pass


def _worker_update_succeeded(update: dict[str, Any]) -> bool:
    if update.get("status") in {"failed", "error", "blocked", "cancelled", "partial_failure"}:
        return False
    if update.get("errors") or update.get("action_failures"):
        return False
    evidence = [item for item in (update.get("tool_evidence") or []) if isinstance(item, dict)]
    if any(item.get("ok") is False for item in evidence):
        return False
    if update.get("execution_required") or update.get("action_required"):
        return any(item.get("ok") is True for item in evidence)
    return True


def _tracked_worker(worker_name: str, worker_fn):
    async def _run(state):
        try:
            update = await worker_fn(state)
        except Exception as exc:
            message = f"{worker_name} worker exception ({type(exc).__name__}): {str(exc)[:240]}"
            try:
                from ..runtime.progress import emit

                emit("error", message, tool=worker_name, status="fail")
            except Exception:
                pass
            update = {
                "errors": [message],
                "worker_results": {worker_name: {"ok": False, "tool_count": 0, "error": message}},
                "events": [{"node": f"{worker_name}_worker", "detail": message, "status": "fail"}],
            }
        if not isinstance(update, dict):
            update = {"errors": [f"{worker_name} worker returned a non-dict update"]}
        result = dict(update)
        worker_results = dict(result.get("worker_results") or {})
        reported = worker_results.get(worker_name)
        ok = _worker_update_succeeded(result)
        if isinstance(reported, dict) and reported.get("ok") is False:
            ok = False
        prior = dict(reported) if isinstance(reported, dict) else {}
        worker_results[worker_name] = {
            **prior,
            "ok": ok,
            "tool_count": len(result.get("tool_evidence") or []),
        }
        result["worker_results"] = worker_results
        return result

    _run.__name__ = worker_fn.__name__
    _run.__qualname__ = worker_fn.__qualname__
    return _run


def build_jarvis_graph(checkpointer: Any | None = None):
    """LangGraph supervisor build + compile."""
    global _CHECKPOINTER
    if checkpointer is None and USE_LANGGRAPH_CHECKPOINTER:
        try:
            from langgraph.checkpoint.memory import InMemorySaver

            _CHECKPOINTER = InMemorySaver()
            checkpointer = _CHECKPOINTER
        except Exception:
            _CHECKPOINTER = None
            checkpointer = None
    else:
        _CHECKPOINTER = checkpointer

    builder = StateGraph(JarvisState)

    # nodes
    builder.add_node("recall", recall_node)
    builder.add_node("route", route_node)
    builder.add_node("clarify", clarify_node)
    builder.add_node("plan", plan_node)
    builder.add_node("verification", verification_node)
    builder.add_node("synthesize", synthesize_node)
    builder.add_node("critic", critic_node)
    builder.add_node("refine", refine_node)
    builder.add_node("persist", persist_node)
    builder.add_node("respond", respond_node)

    # workers (Send targets — function __name__ se register hote hain)
    for _name, fn in WORKER_NODES.items():
        builder.add_node(fn.__name__, _tracked_worker(_name, fn))

    # edges
    builder.add_edge(START, "recall")
    builder.add_edge("recall", "route")
    builder.add_conditional_edges("route", after_route, {"clarify": "clarify", "plan": "plan"})

    worker_names = [fn.__name__ for fn in WORKER_NODES.values()] + ["verification", "synthesize"]
    builder.add_conditional_edges("plan", dispatch, worker_names)
    builder.add_conditional_edges("refine", refine_dispatch, worker_names)

    for fn in WORKER_NODES.values():
        builder.add_edge(fn.__name__, "verification")

    builder.add_edge("verification", "synthesize")
    builder.add_edge("synthesize", "critic")
    builder.add_conditional_edges("critic", after_critic, {"persist": "persist", "refine": "refine"})
    builder.add_edge("persist", "respond")
    builder.add_edge("clarify", END)
    builder.add_edge("respond", END)

    return builder.compile(checkpointer=checkpointer)


def get_jarvis_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_jarvis_graph()
    return _GRAPH


# ---------------------------------------------------------------------------
# convenience API
# ---------------------------------------------------------------------------
def _thread(session_id: str, thread_id: str | None) -> str:
    """Default to one stable thread per session; explicit ids retain graph-level state."""
    return str(thread_id or session_id or "default")


def _clear_turn_cache(session_id: str, request: str) -> None:
    try:
        from ..dspy_engine.modules import clear_request_cache

        clear_request_cache(session_id, request)
    except Exception:
        pass


async def jarvis_ainvoke(
    request: str,
    session_id: str = "default",
    thread_id: str | None = None,
    recursion_limit: int | None = None,
    surface: str = "cli_owner",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run one turn, then evict its implicit-thread checkpoint to bound saver memory."""
    graph = get_jarvis_graph()
    active_thread = _thread(session_id, thread_id)
    lock = _session_lock(active_thread)
    await asyncio.to_thread(lock.acquire)
    state = initial_state(request, session_id=session_id, surface=surface, run_id=run_id)
    config: dict[str, Any] = {
        "configurable": {"thread_id": active_thread},
        "recursion_limit": recursion_limit or (12 + MAX_GRAPH_ITERATIONS * 8),
    }
    try:
        return await graph.ainvoke(state, config=config)
    finally:
        if thread_id is None:
            _cleanup_checkpoint(active_thread)
        _clear_turn_cache(session_id, request)
        lock.release()


async def jarvis_stream(
    request: str,
    session_id: str = "default",
    thread_id: str | None = None,
    surface: str = "cli_owner",
    run_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one turn; implicit-session checkpoints are evicted when iteration ends."""
    graph = get_jarvis_graph()
    active_thread = _thread(session_id, thread_id)
    lock = _session_lock(active_thread)
    await asyncio.to_thread(lock.acquire)
    state = initial_state(request, session_id=session_id, surface=surface, run_id=run_id)
    config: dict[str, Any] = {
        "configurable": {"thread_id": active_thread},
        "recursion_limit": 12 + MAX_GRAPH_ITERATIONS * 8,
    }
    try:
        async for chunk in graph.astream(state, config=config, stream_mode="updates"):
            for node, update in (chunk or {}).items():
                events = (update or {}).get("events") if isinstance(update, dict) else None
                yield {"node": node, "update_keys": list((update or {}).keys()) if isinstance(update, dict) else [],
                       "events": events or []}
                if node == "respond" and isinstance(update, dict):
                    yield {"node": "final", "answer": update.get("answer", ""),
                           "spoken": update.get("spoken", ""), "status": update.get("status", "done")}
    finally:
        if thread_id is None:
            _cleanup_checkpoint(active_thread)
        _clear_turn_cache(session_id, request)
        lock.release()


def graph_mermaid() -> str:
    try:
        return get_jarvis_graph().get_graph().draw_mermaid()
    except Exception as exc:
        return f"graph drawing failed: {exc}"


def graph_summary() -> str:
    """Human-readable architecture (docs/preview me dikhata hai)."""
    return f"""Jarvis LangGraph supervisor
───────────────────────────
START
 └─ recall        (SQLite memory: owner facts + last turns + FTS search)
     └─ route     (DSPy fast router → workers: browser/research/code/desktop/memory/answer)
         ├─ clarify   → ambiguous request par owner se sawaal → END
         └─ plan      (DSPy planner → 3-7 concrete steps + deliverable)
             └─ dispatch  ═══ PARALLEL (LangGraph Send) ═══
                 ├─ browser_worker   (browser-use agents; multi-site = parallel agents)
                 ├─ research_worker  (search + parallel page fetch + DSPy extraction)
                 ├─ code_worker      (code likho → run → traceback se self-heal → re-run)
                 ├─ desktop_worker   (apps/files/notes/notepad + shell — ReAct sub-agent)
                 └─ memory_worker    (facts yaad rakhna/recall)
                     └─ synthesize  (DSPy FinalAnswer → Hinglish answer)
                         └─ critic  (DSPy critic: ok? score? missing? fix_hint?)
                             ├─ persist → respond → END   (good answer)
                             └─ refine  → failed/missing workers only, max {MAX_AUTO_REFINE_PASSES} pass(es)
"""
