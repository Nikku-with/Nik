"""Browser-use worker."""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from ...dspy_engine import modules as dspy_mods
from ..state import JarvisState
from .common import _event, _extract_urls, _finding, _permission_block


async def browser_worker(state: JarvisState) -> dict[str, Any]:
    """browser-use agent(s) ko Jarvis ka prompt do → asli browser me kaam karwao."""
    blocked = _permission_block(state, "browser", "browser")
    if blocked:
        return blocked
    request = state.get("request", "")
    refine_hint = state.get("refine_hint", "")
    task_request = f"{request}\n\nRefine hint: {refine_hint}" if refine_hint else request
    plan_txt = " → ".join(state.get("plan", [])[:5])
    sites = _extract_urls(request)
    done_actions = dict(state.get("done_actions", {}) or {})
    started = time.time()

    spec = await asyncio.to_thread(dspy_mods.website_task_spec, task_request, sites)
    browser_task_prompt = spec.get("browser_task") or task_request
    fields = spec.get("fields") or []
    save_as = spec.get("save_note_as") or ""

    if "note" in request.lower() or "note pad" in request.lower() or "notepad" in request.lower():
        if not save_as:
            m = re.search(r"(\w[\w\s]{2,40}?)\s*(?:naam|name)\s*(?:se|rakh)", request, re.I)
            save_as = (m.group(1).strip().replace(" ", "_") if m else "jarvis_research")

    from ...tools.browser_tools import browser_research, run_browser_task
    from ..idempotency import completed_action, remember_completed_action

    browser_tool = "browser_research" if (sites and len(sites) >= 2) else "run_browser_task"
    action_args = (
        {
            "task": browser_task_prompt,
            "sites": sites,
            "fields": fields,
            "save_note_as": save_as,
        }
        if browser_tool == "browser_research"
        else {
            "task": browser_task_prompt,
            "start_url": sites[0] if sites else "",
            "output_fields": fields,
            "save_note_as": save_as,
        }
    )
    cached = completed_action(done_actions, browser_tool, action_args)
    if cached:
        saved_result = cached.get("result", cached.get("output", "(previously completed)"))
        if isinstance(saved_result, dict):
            outcome = dict(saved_result)
        else:
            outcome = {"ok": True, "result": str(saved_result), "sites": sites, "worker": "browser"}
        outcome["sites"] = sites
    elif sites and len(sites) >= 2:
        # multi-site → parallel browser agents (ek saath, semaphore se bounded)
        result_text = await browser_research.ainvoke(action_args)
        outcome = {"ok": True, "result": str(result_text), "sites": sites, "worker": "browser"}
    else:
        outcome = await run_browser_task(
            task=browser_task_prompt,
            start_url=sites[0] if sites else "",
            output_fields=fields,
            save_note_as=save_as,
        )
        outcome["sites"] = sites

    if outcome.get("ok") and not cached:
        remember_completed_action(
            done_actions, browser_tool, action_args, outcome, output=str(outcome.get("result", "")),
            worker="browser",
        )

    artifacts = []
    if outcome.get("structured"):
        try:
            from ...config import ARTIFACTS_DIR

            path = ARTIFACTS_DIR / f"browser_{int(time.time())}.json"
            path.write_text(json.dumps(outcome["structured"], indent=2, ensure_ascii=False), encoding="utf-8")
            artifacts.append(str(path))
        except Exception:
            pass

    summary = f"task: {browser_task_prompt[:300]}\nplan: {plan_txt}\n\nresult:\n{str(outcome.get('result'))[:6000]}"
    if outcome.get("notes_saved"):
        summary += f"\n\nnote: {outcome['notes_saved']}"
    if outcome.get("diagnosis"):
        summary += f"\n\nwhy it failed (browser_doctor):\n{outcome['diagnosis']}"

    if not outcome.get("ok"):
        # A failed browser run is the one moment a diagnosis is worth its cost; without this
        # the registered `browser_doctor` tool is unreachable at runtime.
        try:
            from ...tools.browser_tools import browser_doctor

            outcome["diagnosis"] = str(await browser_doctor.ainvoke({}))[:1200]
        except Exception as exc:  # a failed diagnosis must never mask the real failure
            outcome["diagnosis"] = f"(doctor unavailable: {type(exc).__name__})"

    # --- execution evidence (hard invariant): kaunsa asli tool chala, kya result aaya ---
    ok = bool(outcome.get("ok"))
    evidence = {
        "worker": "browser",
        "name": browser_tool,
        "args": {"task": browser_task_prompt[:200], "sites": sites, "fields": fields},
        "output": str(outcome.get("result") or outcome.get("error") or "")[:500],
        "ok": ok,
        "source": "worker",
        "seconds": round(time.time() - started, 1),
    }
    if not ok and outcome.get("error"):
        evidence["error"] = str(outcome.get("error"))[:200]

    update: dict[str, Any] = {
        "findings": [_finding("browser", summary, sites=sites, artifacts=artifacts,
                              offline=bool(outcome.get("offline")), seconds=round(time.time() - started, 1))],
        "artifacts": artifacts,
        # browser = tool-required worker: bina is record ke success claim allowed nahi
        "execution_required": True,
        "tool_evidence": [evidence],
        "done_actions": done_actions,
        "events": [_event(state, "browser_worker",
                          f"{time.time() - started:.1f}s • {len(sites)} sites • {browser_tool}={ok}")],
    }
    if outcome.get("notes_saved"):
        update["notes_saved"] = [save_as or "browser_result"]
    if not outcome.get("ok") and outcome.get("error"):
        update["errors"] = [f"browser: {outcome['error']}"]
    return update
