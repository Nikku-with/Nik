"""Local app/file/desktop operator worker."""
from __future__ import annotations

import inspect
import time
from pathlib import Path
from typing import Any

from ...config import OWNER_NAME, WORKSPACE_DIR
from ..idempotency import completed_action
from ..state import JarvisState
from .common import SubAgentRun, _event, _finding, _run_react_subagent
from .desktop_actions import (
    _ACTION_TOOL_NAMES,
    _execute_desktop_action,
    _needs_action,
    detect_desktop_action,
)


def _verify_write_file_call(call: dict[str, Any]) -> str:
    """Independently confirm a write_file result against the requested path and exact text."""
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    raw_path, expected = args.get("path"), args.get("content")
    if not isinstance(raw_path, str) or not raw_path.strip() or not isinstance(expected, str):
        return ""
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = WORKSPACE_DIR / path
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == expected:
            return str(path.absolute())
    except (OSError, UnicodeError, ValueError):
        pass
    return ""


async def desktop_worker(state: JarvisState) -> dict[str, Any]:
    """Local machine par kaam: apps, files, notes/notepad — ek ReAct sub-agent ke saath.

    Verification: agar request me asli action maanga gaya hai par sub-agent ne koi tool call
    NAHI kiya (LLM hawa me "kar diya" bol deta hai), to hum khud deterministic executor se
    wo action chalate hain. Jo bhi hua, wahi `actions_done` / `action_failures` me jaata hai —
    aur synthesize me honesty guard lagta hai.
    """
    request = state.get("request", "")
    started = time.time()
    tools, withheld_tools = _desktop_tools_and_blocked(str(state.get("surface") or "cli_owner"))
    done_actions = dict(state.get("done_actions", {}) or {})

    # --- 1) action pehle se detect karo (LLM se pehle) ---
    action = detect_desktop_action(request)
    needs_action = bool(action.get("actions")) or _needs_action(request)

    subagent_kwargs = {
        "role": "desktop",
        "task": (
            f"Owner ({OWNER_NAME}) ki request: {request}\n"
            f"Refine hint (if any): {state.get('refine_hint', '')}\n"
            "Local machine ke tools use karo: apps kholna/band karna, files dhoondna/padhna, exact file content ko write_file se save karna, notes save karna, "
            "system maintenance (refresh/status/process clear), aur scheduled automations banana. "
            "Confirm karo kya kya actually hua — guess mat karo. Agar tool call nahi kiya to ye mat likho ki kaam ho gaya."
        ),
        "tools": tools,
        "raw_request": request,
    }
    try:
        subagent_params = inspect.signature(_run_react_subagent).parameters
        accepts_ledger = "done_actions" in subagent_params or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in subagent_params.values()
        )
    except (TypeError, ValueError):
        accepts_ledger = True
    if accepts_ledger:
        subagent_kwargs["done_actions"] = done_actions
    run = await _run_react_subagent(**subagent_kwargs)
    calls = list(run.calls)
    forced: list[dict[str, Any]] = []

    # --- 2) verified execution: action *attempt* hi nahi hua to khud chalao ---
    # (dhyaan: fail hui call bhi "attempt" hai — warna same kaam do baar chal jata,
    #  jaise Windows par notepad do baar khulna aur text do baar likhna)
    available_names = {getattr(tool, "name", "") for tool in tools}

    def _action_payload(candidate: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        kind = candidate.get("kind")
        names = _ACTION_TOOL_NAMES.get(kind, ())
        name = next((item for item in names if item in available_names), names[0] if names else "")
        if kind == "open_app":
            args = {"query": candidate.get("app", "")} if name == "open_any_app" else {"app_title": candidate.get("app", "")}
        elif kind == "type_text":
            args = {"text": candidate.get("text", ""), "press_enter": bool(candidate.get("press_enter"))}
        elif kind == "hotkey":
            args = {"keys": candidate.get("keys", "")}
        elif kind == "screenshot":
            args = {"name": "screen"}
        elif kind == "volume":
            args = {"action": candidate.get("action", "up")}
        elif kind == "close_app":
            args = {"window_title": candidate.get("app", "")}
        else:
            args = {}
        return name, args

    def _action_completed(candidate: dict[str, Any]) -> bool:
        tool_name, payload = _action_payload(candidate)
        return bool(tool_name and completed_action(done_actions, tool_name, payload))

    pending_actions = {"actions": [candidate for candidate in action.get("actions", [])
                                   if not _action_completed(candidate)]}
    if pending_actions["actions"]:
        forced = await _execute_desktop_action(pending_actions, tools, done_actions=done_actions)
        calls.extend(forced)

    artifacts: list[str] = []
    for call in calls:
        if call.get("name") != "write_file" or call.get("ok") is not True:
            continue
        verified_path = _verify_write_file_call(call)
        if verified_path:
            if verified_path not in artifacts:
                artifacts.append(verified_path)
        else:
            call["ok"] = False
            call["output"] = "❌ File post-write verification failed; saved file is missing or content differs."

    done = [c for c in calls if c.get("ok")]
    failed = [c for c in calls if not c.get("ok")]

    notes: list[str] = []
    low_r = request.lower()
    wants_note = any(k in low_r for k in ("save kar", "save kardo", "save krdo", "likh do", "likh de",
                                          "notepad me", "note me save", "note bana", "note banao"))
    if wants_note and ("save_note" in run.text or "Note save ho gaya" in run.text):
        notes = [f"desktop_{int(time.time())}"]

    # --- 3) finding me sirf verified facts ---
    body = run.text
    if forced:
        body += ("\n\n[verified execution] sub-agent ne tool call nahi kiya tha, isliye maine khud ye chalaya:\n"
                 + "\n".join(SubAgentRun(calls=forced).lines(limit=8)))
    if done:
        body += "\n\n[verified actions]\n" + "\n".join(SubAgentRun(calls=done).lines(limit=8))
    if failed:
        body += "\n\n[FAILED actions]\n" + "\n".join(SubAgentRun(calls=failed).lines(limit=8))

    # jab request action wali thi par kuch bhi nahi chala — sub-agent ka text unreliable hai
    if needs_action and not calls:
        body = (f"⚠️ VERIFICATION FAILED: is request ke liye koi tool call nahi hua (sub-agent ka text: "
                f"{run.text[:300]})\n"
                "Matlab: machine par kuch bhi nahi hua. Owner ko sach batao, kaam hua nahi.")

    finding = _finding("desktop", body[:6000], seconds=round(time.time() - started, 1),
                       actions=[{"name": c["name"], "ok": c["ok"]} for c in calls][:12],
                       tool_calls=calls[:24], artifacts=artifacts)
    return {
        "findings": [finding],
        "artifacts": artifacts,
        "notes_saved": notes,
        "action_required": needs_action,
        "execution_required": needs_action,
        "tool_evidence": calls[:24],          # asli tool results — honesty_guard ka evidence
        "actions_done": done,
        "action_failures": failed,
        "done_actions": done_actions,
        "blocked_tools": withheld_tools,
        "events": [_event(state, "desktop_worker",
                          f"{time.time() - started:.1f}s • {len(tools)} tools • "
                          f"calls={len(calls)} ok={len(done)} fail={len(failed)}")],
    }

def _desktop_tools_and_blocked(surface: str = "cli_owner") -> tuple[list, list[str]]:
    """Permission-filtered tools plus names withheld at the worker boundary."""
    from ...security.permissions import check as permission_check
    from ...tools import tools_for

    # "skills" (list_skills / read_skill) is bound here or the two registered skill tools
    # are unreachable at runtime: nothing else wires them to a model.
    candidates = tools_for("desktop", "runtime", "ops", "skills")
    # open_any_app handles fuzzy matching and focusing; exclude legacy duplicate launcher.
    candidates = [t for t in candidates if getattr(t, "name", "") != "open_app"]
    allowed, blocked = [], []
    for tool_obj in candidates:
        name = getattr(tool_obj, "name", "")
        if permission_check(name, surface).allowed:
            allowed.append(tool_obj)
        else:
            blocked.append(name)
    return allowed, blocked
