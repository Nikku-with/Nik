"""Profile-memory worker."""
from __future__ import annotations

import re
from typing import Any

from ...memory.store import get_memory
from ..state import JarvisState
from .common import _event, _finding, _permission_block


async def memory_worker(state: JarvisState) -> dict[str, Any]:
    """Facts yaad rakhna / recall karna, current user ke isolated store me."""
    request = state.get("request", "")
    done_actions = dict(state.get("done_actions", {}) or {})
    if any(k in request.lower() for k in ("yaad rakh", "remember", "note kar le")):
        blocked = _permission_block(state, "memory", "remember_fact")
        if blocked:
            return blocked
    else:
        blocked = _permission_block(state, "memory", "memory_read")
        if blocked:
            return blocked
    mem = get_memory()
    stored = ""
    recalled = ""
    low = request.lower()
    try:
        if any(k in low for k in ("yaad rakh", "remember", "note kar le")):
            from ..idempotency import completed_action, remember_completed_action

            fact = re.sub(r"(yaad rakhna|yaad rakho|yaad rakh|remember karo|remember|note kar le)", "", request, flags=re.I).strip(" ,।:-")
            action_args = {"text": fact or request, "tags": "owner,explicit"}
            cached = completed_action(done_actions, "remember_fact", action_args)
            if cached:
                stored = cached.get("output", f"stored: {fact or request}")
            else:
                mem.remember_fact(action_args["text"], tags=action_args["tags"])
                stored = f"stored: {fact or request}"
                remember_completed_action(
                    done_actions, "remember_fact", action_args, {"stored": stored},
                    output=stored, worker="memory",
                )
        if any(k in low for k in ("yaad hai", "kya pata", "recall", "bata mujhe", "bhool")):
            hits = mem.search(request, limit=6)
            recalled = "\n".join(f"- [{h.kind}] {h.text[:200]}" for h in hits) or "(kuch nahi mila)"
    except Exception as exc:
        return {"errors": [f"memory: {exc}"], "findings": [_finding("memory", f"error: {exc}")]}

    summary = f"request: {request[:200]}\n{stored}\n{recalled}".strip()
    return {
        "findings": [_finding("memory", summary or "memory me koi change nahi")],
        "done_actions": done_actions,
        "events": [_event(state, "memory_worker", "memory handled")],
    }
