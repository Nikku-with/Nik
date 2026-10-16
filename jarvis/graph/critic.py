"""Quality critic stage; answer-only paths intentionally avoid an extra call."""
from __future__ import annotations

import asyncio
from typing import Any

from ..config import PROVIDER, is_offline
from ..dspy_engine import modules as dspy_mods
from .refine import _max_refine_passes
from .state import JarvisState
from .workers.common import _event


def after_critic(state: JarvisState) -> str:
    verdict = state.get("critique") or {}
    requirements = state.get("requirements", {}) or {}
    if any(item.get("status") == "needs_user" for item in requirements.values()):
        return "persist"
    unresolved = any(item.get("status") == "failed" for item in requirements.values())
    ok = bool(verdict.get("ok", True)) and not unresolved
    iterations = int(state.get("iterations", 0))
    if ok or state.get("refine_stop") or iterations >= _max_refine_passes():
        return "persist"
    return "refine"


async def critic_node(state: JarvisState) -> dict[str, Any]:
    request = state.get("request", "")
    answer = state.get("answer", "")
    evidence = state.get("evidence", "")
    requirements = state.get("requirements", {}) or {}
    needs_user = [item for item in requirements.values() if item.get("status") == "needs_user"]
    if needs_user:
        verdict = {"ok": True, "score": 1.0, "missing": [item.get("question", "") for item in needs_user],
                   "fix_hint": "", "skipped": "needs-user"}
        return {"critique": verdict, "fix_hint": "",
                "events": [_event(state, "critic", "skipped: owner input required before retry")]}
    unresolved = [item for item in requirements.values() if item.get("status") == "failed"]
    if unresolved:
        focus = "; ".join(f"{item.get('id')} {item.get('requirement')}" for item in unresolved[:4])
        verdict = {"ok": False, "score": 0.0, "missing": [item.get("reason", "evidence missing") for item in unresolved[:4]],
                   "fix_hint": f"Retry only unresolved requirement(s): {focus}"}
        return {"critique": verdict, "fix_hint": verdict["fix_hint"],
                "events": [_event(state, "critic", f"unresolved requirements: {focus[:150]}")]}
    if set(state.get("workers") or []) == {"answer"} and not state.get("errors"):
        # A simple conversational answer has no tool result to critique; avoid another model call.
        verdict = {"ok": True, "score": 1.0, "missing": [], "fix_hint": "", "skipped": "answer-only"}
        return {
            "critique": verdict,
            "fix_hint": "",
            "events": [_event(state, "critic", "skipped: answer-only route")],
        }

    verdict = await asyncio.to_thread(dspy_mods.critic, request, answer, evidence)
    if verdict.get("error") or verdict.get("skipped") == "critic-unavailable":
        # Critic outages must not discard a usable draft or trigger another action pass.
        kept = {**verdict, "ok": True, "fix_hint": ""}
        return {
            "critique": kept,
            "fix_hint": "",
            "events": [_event(state, "critic", "unavailable; existing answer kept without refinement")],
        }

    # offline heuristic critic
    if is_offline() or PROVIDER == "demo" or not verdict.get("score"):
        score = 0.75
        if state.get("findings"):
            score += 0.15
        if state.get("errors"):
            score -= 0.2
        if len(answer) < 80:
            score -= 0.25
        verdict = {"ok": score >= 0.6, "score": max(0.0, min(1.0, score)),
                   "missing": [] if score >= 0.6 else ["answer chhota/incomplete lag raha hai"],
                   "fix_hint": "" if score >= 0.6 else "errors dekho aur failed worker dobara chalao"}

    return {
        "critique": verdict,
        "fix_hint": verdict.get("fix_hint", ""),
        "events": [_event(state, "critic", f"ok={verdict.get('ok')} score={verdict.get('score')}")],
    }
