"""Stable idempotency keys and success ledger helpers for graph worker actions."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(args: Any) -> str:
    """Serialize tool arguments deterministically for a repeatable action key."""
    return json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def action_idempotency_key(tool: str, args: Any) -> str:
    """Return SHA-1(tool + canonical JSON args), as used by the per-turn done ledger."""
    return hashlib.sha1((str(tool) + canonical_json(args)).encode("utf-8")).hexdigest()


def completed_action(ledger: dict[str, Any] | None, tool: str, args: Any) -> dict[str, Any] | None:
    """Find a previously successful action; failures are deliberately not cached."""
    entry = (ledger or {}).get(action_idempotency_key(tool, args))
    return entry if isinstance(entry, dict) and entry.get("ok") is True else None


def remember_completed_action(
    ledger: dict[str, Any],
    tool: str,
    args: Any,
    result: Any,
    *,
    output: str | None = None,
    worker: str = "",
) -> str:
    """Store one successful tool result and return its deterministic key."""
    key = action_idempotency_key(tool, args)
    record = {
        "tool": str(tool),
        "args": args,
        "output": str(output if output is not None else result)[:1200],
        "ok": True,
    }
    if worker:
        record["worker"] = worker
    try:
        record["result"] = json.loads(canonical_json(result))
    except (TypeError, ValueError):
        record["result"] = str(result)
    ledger[key] = record
    return key
