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


class JarvisState(TypedDict, total=False):
    # ---- conversation -----------------------------------------------------
    messages: Annotated[list[AnyMessage], add_messages]
    request: str
    session_id: str

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
    selected_tools: list[str]
    reasoning_summary: list[str]     # PLAN / DECISION / ACTION / RESULT / VERIFICATION / NEXT

    # ---- execution --------------------------------------------------------
    findings: Annotated[list[dict[str, Any]], operator.add]
    evidence: str
    artifacts: Annotated[list[str], operator.add]
    notes_saved: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]
    tool_calls: Annotated[int, operator.add]

    # ---- reflection loop --------------------------------------------------
    iterations: int
    critique: dict[str, Any]
    fix_hint: str

    # ---- output -----------------------------------------------------------
    answer: str
    spoken: str
    task_id: str
    status: str

    # ---- action verification (LLM "kar diya" bole par tool chala hi na ho) ----
    action_required: bool                            # request me koi asli action maanga gaya?
    execution_required: bool                         # is run me asli tool execution chahiye thi?
    tool_evidence: Annotated[list[dict[str, Any]], operator.add]     # verified tool records (sab workers)
    actions_done: Annotated[list[dict[str, Any]], operator.add]      # actual tool calls (ok)
    action_failures: Annotated[list[dict[str, Any]], operator.add]   # fail hue tool calls
    events: Annotated[list[dict[str, Any]], operator.add]


def initial_state(request: str, session_id: str = "default", messages: list | None = None) -> JarvisState:
    return JarvisState(
        request=request,
        session_id=session_id,
        messages=messages or [],
        workers=[],
        plan=[],
        selected_tools=[],
        reasoning_summary=[],
        findings=[],
        artifacts=[],
        notes_saved=[],
        errors=[],
        action_required=False,
        actions_done=[],
        action_failures=[],
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
    # Live clock har answer ke saath — model ko date/time guess karne ki zaroorat hi na pade
    return f"LOCAL CLOCK (authoritative, isko hi sach maano): {local_clock_line()}\n\n{text}"


def local_clock_line() -> str:
    """Ek line me local time + timezone + UTC offset + weekday + UTC."""
    from datetime import datetime, timedelta, timezone

    try:
        from ..tools.web_tools import IST
    except Exception:  # pragma: no cover - fallback agar import fail ho
        IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(IST)
    utc = datetime.now(timezone.utc)
    offset = now.utcoffset()
    off = f"UTC{'+' if (offset or timedelta()).total_seconds() >= 0 else '-'}{abs(offset).seconds // 3600:02d}:{(abs(offset).seconds // 60) % 60:02d}" if offset else "UTC"
    return (f"{now.strftime('%A, %d %B %Y — %I:%M %p')} ({now.tzname() or 'IST'}, {off})"
            f" | UTC: {utc.strftime('%H:%M')} | epoch: {int(now.timestamp())}")
