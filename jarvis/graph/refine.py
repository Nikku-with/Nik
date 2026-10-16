"""Bounded, evidence-aware retries for unresolved graph work."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ..config import MAX_AUTO_REFINE_PASSES, MAX_GRAPH_ITERATIONS
from .routing import _worker_sends
from .state import RESET_STATUS, JarvisState, replace_list
from .workers import WORKER_NODES
from .workers.common import _event


def _execution_signature(state: JarvisState) -> str:
    """Stable signature over worker/tool/args/success records; deliberately excludes clocks/output."""

    from .idempotency import canonical_json

    items: set[tuple[str, str, str, bool]] = set()
    records = list(state.get("tool_evidence", []) or [])
    records.extend(
        record for record in (state.get("done_actions", {}) or {}).values()
        if isinstance(record, dict)
    )
    for record in records:
        if not isinstance(record, dict):
            continue
        worker = str(record.get("worker") or "")
        tool = str(record.get("name") or record.get("tool") or "")
        args_hash = hashlib.sha1(canonical_json(record.get("args") or {}).encode("utf-8")).hexdigest()
        items.add((worker, tool, args_hash, record.get("ok") is True))
    payload = json.dumps(sorted(items), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

def _requires_user_retry(state: JarvisState) -> bool:
    """Do not automatically replay a worker when a human must unblock it first."""
    if state.get("blocked_tools") or state.get("status") in {"blocked", "needs_clarification", "needs_user", "cancelled"}:
        return True
    user_action_markers = (
        "login required", "sign in required", "captcha", "manual confirmation",
        "owner action required", "human action required", "requires user",
    )
    records = list(state.get("action_failures", []) or [])
    errors = [str(error) for error in (state.get("errors", []) or [])]
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("requires_user") or record.get("human_required"):
            return True
        text = " ".join(str(record.get(key, "")) for key in ("output", "error", "source")).lower()
        if record.get("source") == "precondition_block" or any(marker in text for marker in user_action_markers):
            return True
    return any(any(marker in error.lower() for marker in user_action_markers) for error in errors)

def _worker_succeeded(outcome: Any) -> bool:
    if isinstance(outcome, bool):
        return outcome
    return isinstance(outcome, dict) and outcome.get("ok") is True

def _max_refine_passes() -> int:
    return max(0, min(int(MAX_AUTO_REFINE_PASSES), int(MAX_GRAPH_ITERATIONS)))

async def refine_node(state: JarvisState) -> dict[str, Any]:
    """Prepare one bounded refine pass without changing the owner's original request."""
    iteration = int(state.get("iterations", 0))
    limit = _max_refine_passes()
    hint = str(state.get("fix_hint") or "pichhla attempt adhoora tha — missing data complete karo")
    signature = _execution_signature(state)
    seen_signatures = state.get("refine_signatures", []) or []

    if iteration >= limit:
        return {
            "iterations": limit,
            "refine_hint": hint,
            "refine_stop": True,
            "events": [_event(state, "refine", "maximum automatic refine passes reached; stopping worker retries")],
        }
    if signature in seen_signatures:
        return {
            "iterations": limit,
            "refine_hint": hint,
            "refine_stop": True,
            "events": [_event(state, "refine", f"LOOP DETECTED (sig {signature}) — worker retry rok diya")],
        }
    if _requires_user_retry(state):
        return {
            "iterations": limit,
            "refine_hint": hint,
            "refine_stop": True,
            "events": [_event(state, "refine", "manual/user action required before retry; worker retry rok diya")],
        }

    worker_results = state.get("worker_results", {}) or {}
    requested_workers = [worker for worker in state.get("workers", []) or [] if worker in WORKER_NODES]
    unresolved = [item for item in (state.get("requirements", {}) or {}).values()
                  if isinstance(item, dict) and item.get("status") == "failed"]
    requirement_workers = [str(item.get("worker") or "") for item in unresolved if item.get("worker")]
    retry_workers = list(dict.fromkeys(worker for worker in requirement_workers if worker in requested_workers))
    if not retry_workers:
        retry_workers = [worker for worker in requested_workers
                         if not _worker_succeeded(worker_results.get(worker))]
    completed_workers = {worker for worker in requested_workers if worker not in retry_workers}

    keep_findings = [
        item for item in (state.get("findings", []) or [])
        if not isinstance(item, dict) or item.get("worker") not in requested_workers
        or item.get("worker") in completed_workers
    ]
    keep_evidence = [
        item for item in (state.get("tool_evidence", []) or [])
        if not isinstance(item, dict) or item.get("ok") is not False
    ]
    keep_actions = [
        item for item in (state.get("actions_done", []) or [])
        if not isinstance(item, dict) or item.get("ok") is True
    ]
    if unresolved:
        hint = "Retry only unresolved items: " + "; ".join(
            f"{item.get('id')} {item.get('requirement')} — {item.get('reason', 'missing evidence')}"
            for item in unresolved[:4])
        hint = hint[:700]

    return {
        "iterations": iteration + 1,
        "refine_hint": hint,
        "refine_stop": False,
        "retry_workers": retry_workers,
        "refine_signatures": [signature],
        "critique": {},
        "fix_hint": "",
        "findings": replace_list(keep_findings),
        "errors": replace_list([]),
        "tool_evidence": replace_list(keep_evidence),
        "actions_done": replace_list(keep_actions),
        "action_failures": replace_list([]),
        "status": RESET_STATUS,
        "events": [_event(state, "refine", f"pass {iteration + 1}: retry only {retry_workers or 'no failed workers'}; {hint[:120]}")],
    }

def refine_dispatch(state: JarvisState) -> Any:
    """Refine pass: re-run only failed/missing workers, never completed side effects."""
    if state.get("refine_stop") or int(state.get("iterations", 0)) > _max_refine_passes():
        return "synthesize"
    retry_workers = [worker for worker in (state.get("retry_workers", []) or []) if worker in WORKER_NODES]
    if not retry_workers:
        return "synthesize"
    return _worker_sends(state, retry_workers)
