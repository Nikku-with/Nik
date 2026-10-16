"""
jarvis/graph/state.py — LangGraph state schema
=============================================

Ek shared state jo poore graph me flow karta hai. Reducers (`operator.add`) se
parallel workers apna-apna hissa jodte hain — race condition nahi hoti.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

_REPLACE_LIST_SENTINEL = "_jarvis_replace_list"
RESET_STATUS = "__JARVIS_RESET_STATUS__"


def append_or_replace(existing: list[Any] | None, updates: list[Any] | None) -> list[Any]:
    """Append parallel records, with an explicit reset/replace control record."""
    result = list(existing or [])
    for item in updates or []:
        if isinstance(item, dict) and item.get(_REPLACE_LIST_SENTINEL) is True:
            result = list(item.get("items") or [])
        else:
            result.append(item)
    return result


def replace_list(items: list[Any] | None) -> list[dict[str, Any]]:
    """Create a one-update reset record consumed by ``append_or_replace``."""
    return [{_REPLACE_LIST_SENTINEL: True, "items": list(items or [])}]


def merge_dicts(existing: dict[str, Any] | None, updates: dict[str, Any] | None) -> dict[str, Any]:
    """Merge independent per-worker ledgers/results into shared graph state."""
    return {**(existing or {}), **(updates or {})}


def last_non_empty(existing: str | None, update: str | None) -> str:
    """Use the newest non-empty status; an explicit sentinel clears a prior-pass status."""
    if update == RESET_STATUS:
        return ""
    if isinstance(update, str) and update.strip():
        return update
    return existing or ""


class JarvisState(TypedDict, total=False):
    # ---- conversation -----------------------------------------------------
    messages: Annotated[list[AnyMessage], add_messages]
    request: str
    session_id: str
    surface: str
    run_id: str

    # ---- context ----------------------------------------------------------
    owner_facts: str
    memory_context: str
    history_summary: str

    # ---- routing & planning ----------------------------------------------
    workers: list[str]
    route_reason: str
    needs_clarification: bool
    plan: list[str]
    deliverable: str
    reasoning_summary: list[str]     # PLAN / DECISION / ACTION / RESULT / VERIFICATION / NEXT

    # ---- execution --------------------------------------------------------
    findings: Annotated[list[dict[str, Any]], append_or_replace]
    evidence: str
    artifacts: Annotated[list[str], operator.add]
    notes_saved: Annotated[list[str], operator.add]
    errors: Annotated[list[str], append_or_replace]
    blocked_tools: Annotated[list[str], operator.add]
    tool_calls: Annotated[int, operator.add]
    worker_results: Annotated[dict[str, dict[str, Any]], merge_dicts]
    done_actions: Annotated[dict[str, dict[str, Any]], merge_dicts]
    refine_signatures: Annotated[list[str], operator.add]
    refine_hint: str
    retry_workers: list[str]
    refine_stop: bool
    requirements: Annotated[dict[str, dict[str, Any]], merge_dicts]
    requirements_unresolved: bool

    # ---- reflection loop --------------------------------------------------
    iterations: int
    critique: dict[str, Any]
    fix_hint: str

    # ---- output -----------------------------------------------------------
    answer: str
    spoken: str
    task_id: str
    status: Annotated[str, last_non_empty]
    memory_persisted: bool

    # ---- action verification (LLM "kar diya" bole par tool chala hi na ho) ----
    action_required: Annotated[bool, operator.or_]      # parallel worker updates combine safely
    execution_required: Annotated[bool, operator.or_]   # parallel worker updates combine safely
    tool_evidence: Annotated[list[dict[str, Any]], append_or_replace]
    actions_done: Annotated[list[dict[str, Any]], append_or_replace]
    action_failures: Annotated[list[dict[str, Any]], append_or_replace]
    events: Annotated[list[dict[str, Any]], operator.add]


def initial_state(request: str, session_id: str = "default", messages: list | None = None,
                  surface: str = "cli_owner", run_id: str | None = None) -> JarvisState:
    return JarvisState(
        request=request,
        session_id=session_id,
        surface=surface,
        run_id=run_id or "",
        messages=messages or [],
        workers=[],
        plan=[],
        reasoning_summary=[],
        findings=[],
        artifacts=[],
        notes_saved=[],
        errors=[],
        blocked_tools=[],
        action_required=False,
        execution_required=False,
        tool_evidence=[],
        actions_done=[],
        action_failures=[],
        done_actions={},
        worker_results={},
        refine_signatures=[],
        refine_hint="",
        retry_workers=[],
        refine_stop=False,
        requirements={},
        requirements_unresolved=False,
        iterations=0,
        critique={},
        events=[],
        status="running",
    )


def evidence_digest(state: JarvisState, limit_per_item: int = 1800, total_limit: int = 9000) -> str:
    """Findings ko ek readable evidence block me badlo (final answer/critic ke liye)."""
    chunks: list[str] = []
    for i, f in enumerate(state.get("findings", []) or [], 1):
        worker = f.get("worker", "?")
        summary = str(f.get("summary") or f.get("result") or "").strip()
        if not summary:
            continue
        body = summary[:limit_per_item]
        extra = ""
        if f.get("sites"):
            extra = f"\n  sites: {', '.join(str(s) for s in f['sites'][:6])}"
        if f.get("artifacts"):
            extra += f"\n  files: {', '.join(str(a) for a in f['artifacts'][:6])}"
        chunks.append(f"[{i}] {worker}{extra}\n{body}")

    if not chunks:
        text = "(koi tool evidence nahi — direct answer path)"
    else:
        text = "\n\n".join(chunks)
        text = text[:total_limit] + ("\n… (truncated)" if len(text) > total_limit else "")
    # Clock facts enter context only for an explicit time, schedule, deadline, or relative-date ask.
    from ..time_intent import needs_clock_context

    if needs_clock_context(str(state.get("request") or "")):
        return f"LOCAL CLOCK (only for this explicit request): {local_clock_line()}\n\n{text}"
    return text


def local_clock_line() -> str:
    """Configured local clock; callers must first establish explicit time intent."""
    from datetime import datetime, timezone

    from ..timezone import configured_timezone

    now = datetime.now(configured_timezone())
    utc = datetime.now(timezone.utc)
    return f"{now.strftime('%A, %d %B %Y — %I:%M %p')} ({now.tzname() or 'local'}) | UTC: {utc.strftime('%H:%M')}"
