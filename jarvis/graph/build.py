"""
jarvis/graph/build.py — Jarvis ka LangGraph supervisor compile karo
==================================================================

    graph = build_jarvis_graph()          # compiled LangGraph
    result = await jarvis_ainvoke("...")  # one-shot run
    async for ev in jarvis_stream("..."): # live events (UI/voice)
        ...

Features:
  * parallel worker fan-out (Send API) — browser + research + code ek saath
  * checkpointer (InMemorySaver) — session/history persist, same session_id se context
  * error isolation — ek worker fail ho to poora graph nahi girta
  * mermaid diagram (`graph_mermaid()`) — docs/preview ke liye
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator

from langgraph.graph import END, START, StateGraph

from ..config import MAX_GRAPH_ITERATIONS, USE_LANGGRAPH_CHECKPOINTER
from .nodes import (
    WORKER_NODES,
    after_critic,
    after_route,
    browser_worker,
    clarify_node,
    code_worker,
    critic_node,
    desktop_worker,
    dispatch,
    memory_worker,
    persist_node,
    plan_node,
    recall_node,
    refine_node,
    research_worker,
    respond_node,
    route_node,
    synthesize_node,
)
from .state import JarvisState, initial_state

_GRAPH = None
_CHECKPOINTER = None


def build_jarvis_graph(checkpointer: Any | None = None):
    """LangGraph supervisor build + compile."""
    global _CHECKPOINTER
    if checkpointer is None and USE_LANGGRAPH_CHECKPOINTER:
        try:
            from langgraph.checkpoint.memory import InMemorySaver

            _CHECKPOINTER = InMemorySaver()
            checkpointer = _CHECKPOINTER
        except Exception:
            checkpointer = None

    builder = StateGraph(JarvisState)

    # nodes
    builder.add_node("recall", recall_node)
    builder.add_node("route", route_node)
    builder.add_node("clarify", clarify_node)
    builder.add_node("plan", plan_node)
    builder.add_node("synthesize", synthesize_node)
    builder.add_node("critic", critic_node)
    builder.add_node("refine", refine_node)
    builder.add_node("persist", persist_node)
    builder.add_node("respond", respond_node)

    # workers (Send targets — function __name__ se register hote hain)
    for name, fn in WORKER_NODES.items():
        builder.add_node(fn.__name__, fn)

    # edges
    builder.add_edge(START, "recall")
    builder.add_edge("recall", "route")
    builder.add_conditional_edges("route", after_route, {"clarify": "clarify", "plan": "plan"})

    worker_names = [fn.__name__ for fn in WORKER_NODES.values()] + ["synthesize"]
    builder.add_conditional_edges("plan", dispatch, worker_names)
    builder.add_conditional_edges("refine", dispatch, worker_names)

    for fn in WORKER_NODES.values():
        builder.add_edge(fn.__name__, "synthesize")

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
    """
    Default: har turn ka apna thread → per-turn outputs (findings/notes/artifacts) clean rehte hain.
    Cross-turn continuity SQLite memory se aati hai (recall node), checkpointer se nahi.
    Explicit `thread_id` do to graph-level multi-turn state persist karega.
    """
    if thread_id:
        return thread_id
    return f"{session_id}-{int(time.time() * 1000)}"


async def jarvis_ainvoke(
    request: str,
    session_id: str = "default",
    thread_id: str | None = None,
    recursion_limit: int | None = None,
) -> dict[str, Any]:
    """Ek request chalao aur final state wapas lo."""
    graph = get_jarvis_graph()
    state = initial_state(request, session_id=session_id)
    config: dict[str, Any] = {
        "configurable": {"thread_id": _thread(session_id, thread_id)},
        "recursion_limit": recursion_limit or (12 + MAX_GRAPH_ITERATIONS * 8),
    }
    final = await graph.ainvoke(state, config=config)
    return final


async def jarvis_stream(
    request: str,
    session_id: str = "default",
    thread_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Node-by-node live events + thinking feed (UI/voice streaming ke liye).

    Har node update ke saath us dauraan emit hue ProgressEvents bhi aate hain
    (`{"node": "progress", ...}` chunks) — web console par bhi thinking/tools dikhte hain.
    """
    from ..runtime.progress import CollectingSink, use_sink

    graph = get_jarvis_graph()
    state = initial_state(request, session_id=session_id)
    config: dict[str, Any] = {
        "configurable": {"thread_id": _thread(session_id, thread_id)},
        "recursion_limit": 12 + MAX_GRAPH_ITERATIONS * 8,
    }
    sink = CollectingSink(limit=500)
    yielded = 0
    last_answer = ""  # respond node answer wapas nahi deta — synthesize se yaad rakho
    with use_sink(sink):
        async for chunk in graph.astream(state, config=config, stream_mode="updates"):
            for node, update in (chunk or {}).items():
                events = (update or {}).get("events") if isinstance(update, dict) else None
                if isinstance(update, dict) and update.get("answer"):
                    last_answer = str(update["answer"])
                yield {
                    "node": node,
                    "update_keys": list((update or {}).keys()) if isinstance(update, dict) else [],
                    "events": events or [],
                }
                if node == "respond" and isinstance(update, dict):
                    yield {
                        "node": "final",
                        "answer": last_answer,
                        "spoken": update.get("spoken", ""),
                        "status": update.get("status", "done"),
                    }
            fresh = sink.events[yielded:]
            yielded = len(sink.events)
            if fresh:
                yield {
                    "node": "progress",
                    "events": [
                        {"kind": e.kind, "text": e.text, "tool": e.tool, "status": e.status, "ms": e.ms} for e in fresh
                    ],
                    "lines": [e.line() for e in fresh],
                }


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
                             └─ refine  → (wapis dispatch, max {MAX_GRAPH_ITERATIONS} iterations)
"""
