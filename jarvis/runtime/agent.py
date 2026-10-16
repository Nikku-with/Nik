"""
Unified agent runner — CLI, voice, Telegram, API sab yahi use karte hain
=======================================================================

`run_agent(text, session_id, surface)` ek hi entry point hai: LangGraph chalta hai, memory update
hoti hai, permissions apply hote hain, events trace hote hain, aur ek structured result milta hai.

Kyun ek hi runner: voice/text/telegram ke alag-alag brain banane se behaviour diverge ho jata hai
aur bugs teen jagah fix karne padte hain. Yahan sab ek hi graph + ek hi memory + ek hi tools use
karte hain — bas session aur permission level alag hota hai.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from typing import Any

from ..memory.session_state import get_state
from ..voice.speech import for_speech, summary_for_speech


@dataclass
class AgentResult:
    request: str
    answer: str = ""
    spoken: str = ""
    plan: list[str] = field(default_factory=list)
    workers: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)   # structured PLAN/DECISION/ACTION/RESULT lines
    events: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    iterations: int = 0
    critique: dict[str, Any] = field(default_factory=dict)
    requirements: dict[str, dict[str, Any]] = field(default_factory=dict)
    seconds: float = 0.0
    session_id: str = "default"
    surface: str = "cli_owner"
    status: str = "done"
    blocked_tools: list[str] = field(default_factory=list)
    memory_updates: list[str] = field(default_factory=list)
    # ---- execution evidence (hard invariant: success bina proof ke nahi) --------------
    tool_evidence: list[dict[str, Any]] = field(default_factory=list)   # asli tool records
    execution_required: bool = False                                    # kaam ke liye tool zaroori tha?
    verified_execution: bool = False                                    # evidence mila + koi fail nahi
    memory_persisted: bool = False                                      # graph ya runner ne turn save kiya
    task_id: str = ""                                                    # shared task row; no duplicate task insert
    run_id: str = ""                                                     # scheduler trigger identity

    @property
    def tool_calls_flat(self) -> list[dict[str, Any]]:
        """Saare tool records ek list me (findings + worker-level evidence)."""
        out = [rec for rec in (self.tool_evidence or []) if isinstance(rec, dict)]
        for f in (self.findings or []):
            for rec in (f.get("tool_calls") or []) if isinstance(f, dict) else []:
                if isinstance(rec, dict):
                    out.append(rec)
        return out

    def recompute_execution_flags(self) -> None:
        """flags ko evidence se dobara nikalo — koi bhi code path inhe fake true na kar sake.

        (Manual set nahi: sirf `tool_evidence`/findings se, aur success ke liye kam se kam
        ek ok record + koi failed record nahi.)
        """
        from ..runtime.evidence import supporting_records

        records = self.tool_calls_flat
        ok = supporting_records(records)          # khali search (found=False) success nahi hai
        failed = [r for r in records if not r.get("ok")]
        self.verified_execution = bool(ok) and not failed

    def as_dict(self) -> dict[str, Any]:
        return {
            "request": self.request, "response": self.answer, "answer": self.answer,
            "spoken": self.spoken, "plan": self.plan, "selected_workers": self.workers,
            "workers": self.workers, "reasoning": self.reasoning, "events": self.events,
            "findings": self.findings, "artifacts": self.artifacts, "errors": self.errors,
            "iterations": self.iterations, "critique": self.critique, "requirements": self.requirements,
            "seconds": self.seconds,
            "session_id": self.session_id, "surface": self.surface, "status": self.status,
            "blocked_tools": self.blocked_tools,
            "execution_required": self.execution_required,
            "verified_execution": self.verified_execution,
            "memory_persisted": self.memory_persisted, "task_id": self.task_id,
            "run_id": self.run_id, "tool_evidence": self.tool_calls_flat[:24],
            "memory_updates": self.memory_updates,
        }


# ---------------------------------------------------------------------------
# time / date intent — direct answers only for explicit requests
# ---------------------------------------------------------------------------
def _is_time_question(text: str) -> bool:
    from ..time_intent import is_explicit_time_question

    return is_explicit_time_question(text)


def _time_answer(text: str) -> str:
    """Concise local-clock answer; no timestamp/date extras unless they were requested."""
    from datetime import datetime, timedelta, timezone

    from ..timezone import configured_timezone

    tz = configured_timezone()
    low = (text or "").lower()
    now = datetime.now(tz)
    if any(k in low for k in ("beeta kal", "bita kal", "guzra kal", "yesterday")):
        target, label = now - timedelta(days=1), "Kal (beeta)"
    elif any(k in low for k in ("parso", "parson", "day after tomorrow")):
        target, label = now + timedelta(days=2), "Parso"
    elif any(k in low for k in ("kal", "tomorrow")):
        target, label = now + timedelta(days=1), "Kal"
    else:
        target, label = now, "Aaj"

    time_words = ("time", "samay", "baje", "baj", "clock", "ghadi", "utc", "timezone", "time zone")
    date_words = ("date", "tareekh", "tarikh", "din", "day", "tomorrow", "yesterday", "kal", "parso")
    want_time = any(word in low for word in time_words)
    want_date = any(word in low for word in date_words)
    if not want_time and not want_date:
        want_time = True

    lines: list[str] = []
    if want_time:
        local_time = target.strftime("%I:%M %p")
        if "utc" in low:
            lines.append(f"{label}: {target.astimezone(timezone.utc).strftime('%I:%M %p')} UTC.")
        elif target.date() == now.date():
            lines.append(f"Abhi {local_time} ({target.tzname() or 'local'}) hai.")
        else:
            lines.append(f"{label}, {local_time} ({target.tzname() or 'local'}).")
    if want_date:
        lines.append(f"{label}: {target.strftime('%A, %d %B %Y')}.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# fast-path handlers (LLM se pehle deterministic kaam — sasta + bharosemand)
# ---------------------------------------------------------------------------
_GREETING_RX = re.compile(
    r"^(?:hi+|hey+|hello+|hy+|yo|namaste|namaskar|good\s+morning|good\s+afternoon|good\s+evening)"
    r"(?:\s+(?:jarvis|bhai|boss|there))?"
    r"(?:\s*(?:how\s+are\s+you|how\s+r\s+u|kaise\s+ho|kaisa\s+hai))?$",
    re.I,
)


def is_trivial_greeting(text: str) -> bool:
    normalized = re.sub(r"[^\w\s]", " ", (text or "").lower(), flags=re.UNICODE)
    normalized = " ".join(normalized.split())
    return bool(normalized and len(normalized.split()) <= 7 and _GREETING_RX.fullmatch(normalized))


async def _fast_paths(text: str, session_id: str, surface: str,
                      emit=None) -> AgentResult | None:
    from ..config import runtime as R
    from ..memory.manager import get_manager, is_memory_request, strip_trigger

    started = time.time()
    low = text.lower().strip()
    emit = emit or (lambda *_args, **_kwargs: None)

    # 0) time / date — local clock, koi LLM nahi (hallucination-proof)
    if _is_time_question(low):
        answer = _time_answer(text)
        return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                           surface=surface, seconds=time.time() - started, status="time",
                           workers=["clock"], reasoning=["ACTION: local clock read",
                                                      "RESULT: time/date local system clock se (IST + UTC)"])

    # 0.5) trivial greetings deliberately have NO canned reply here.
    # A fixed "Hey <owner>! Main yahin hoon." for every "hy"/"hy bhai" is the exact
    # behaviour the owner reported as broken. Greetings now fall through to the one-call
    # chat path (see _run_agent_body), so the wording varies per turn.

    # Sensitive memory saves are staged; resolve only a clear owner yes/no reply.
    confirmation_text = " ".join(re.sub(r"[,;:.!?]+", " ", low).split())
    confirmation_rx = (r"(?:yes|y|haan|han|ha|confirm|confirm karo|haan save karo|yes save it|save it|"
                       r"nahi|nahin|no|cancel|cancel karo|mat save karo|nahi mat save karo|"
                       r"don't save|do not save)")
    if re.fullmatch(confirmation_rx, confirmation_text):
        try:
            pending_manager = get_manager()
            pending_items = pending_manager.pending()
            if pending_items:
                if len(pending_items) > 1:
                    lines = [f"• pending ID: {item.get('id', '')}" for item in pending_items]
                    if surface == "api":
                        guidance = "GET /memory mein pending IDs dekho, phir POST /memory par action=confirm ya cancel aur exact id bhejo."
                    elif surface.startswith("telegram"):
                        guidance = "/memory pending dekho, phir /memory confirm <id> ya /memory cancel <id> chalao."
                    else:
                        guidance = "Pehle memory list mein exact pending ID chuno; main andaze se kisi ko save/cancel nahi karunga."
                    answer = ("Ek se zyada sensitive memory confirmations pending hain; koi item nahi badla. "
                              + guidance + "\n" + "\n".join(lines))
                    return AgentResult(text, answer, spoken=summary_for_speech(answer), session_id=session_id,
                                       surface=surface, seconds=time.time() - started, status="needs_user",
                                       workers=["memory"])
                confirmed = confirmation_text in {
                    "yes", "y", "haan", "han", "ha", "confirm", "confirm karo",
                    "haan save karo", "yes save it", "save it",
                }
                resolved = pending_manager.confirm_pending(
                    confirmed=confirmed, pending_id=str(pending_items[0].get("id") or ""),
                )
                answer = resolved.get("message", "Memory confirmation process nahi ho paaya.")
                return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                                   surface=surface, seconds=time.time() - started,
                                   status="memory" if resolved.get("ok") else "failed",
                                   workers=["memory"], memory_updates=[resolved.get("action", "")])
        except Exception:
            pass

    # 1) memory operations
    op = is_memory_request(text)
    if op:
        if op in {"remember", "forget", "update"}:
            from ..security.permissions import check as permission_check

            if not permission_check("remember_fact", surface).allowed:
                answer = "🔒 Is interface par memory badalne ki permission nahi hai. Kuch save/delete/update nahi hua."
                return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                                   surface=surface, seconds=time.time() - started, status="blocked",
                                   workers=["permissions"], blocked_tools=["remember_fact"])
        mgr = get_manager()
        if op == "remember":
            res = mgr.remember(strip_trigger(text))
            answer = ("🧠 " if res.get("ok") else "⚠️ ") + res["message"]
            return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                               surface=surface, seconds=time.time() - started,
                               status="needs_user" if res.get("action") == "pending_confirmation" else "memory",
                               workers=["memory"], memory_updates=[res.get("action", "")])
        if op == "forget":
            if surface == "voice_owner":
                from ..security.gate import request_human_approval

                approval = await request_human_approval(
                    f"forget memory: {strip_trigger(text)[:180]}", "memory", surface=surface,
                    reason="spoken confirmation required before deleting memory",
                )
                if not approval.allowed:
                    answer = "Memory delete cancel kiya; clear haan nahi mila. Kuch delete nahi hua."
                    return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                                       surface=surface, seconds=time.time() - started, status="cancelled",
                                       workers=["memory"], errors=[approval.reason])
            res = mgr.forget(text)
            answer = ("🗑️ " if res.get("ok") else "🧐 ") + res["message"]
            return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                               surface=surface, seconds=time.time() - started, status="memory",
                               workers=["memory"])
        if op == "update":
            from ..config import runtime as rt

            # setting jaisi update? → turant apply
            m = __import__("re").search(r"(?:set|kar do|karo|rakho|badal)\s+([a-z_]+)\s*[:=]?\s*(\S+)", low)
            if m and m.group(1) in rt.SCHEMA:
                applied, errors = rt.set_many(**{m.group(1): m.group(2)})
                answer = (f"✅ setting `{m.group(1)}` = {list(applied.values())[0]}" if applied
                          else f"⚠️ {'; '.join(errors)}")
                return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                                   surface=surface, seconds=time.time() - started, status="settings",
                                   workers=["settings"])
            res = mgr.update_by_query(text, strip_trigger(text))
            answer = ("♻️ " if res.get("ok") else "⚠️ ") + res["message"]
            return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                               surface=surface, seconds=time.time() - started, status="memory",
                               workers=["memory"])
        if op == "list":
            rows = mgr.list_memories(limit=20)
            body = "\n".join(f"{i}. [{r['kind']}] {r['text'][:110]}" for i, r in enumerate(rows, 1)) or "khali"
            answer = f"🧠 Memory ({len(rows)}):\n{body}"
            return AgentResult(text, answer, spoken=f"Aapki {len(rows)} memories hain.",
                               session_id=session_id, surface=surface,
                               seconds=time.time() - started, status="memory", workers=["memory"])

    # 1.5) package installs — one decision, one run.
    # Pehle ye code engine me jaata tha: script generate → Gate (likhne par) → Gate (chalane
    # par) = 2 approvals aur ~2 minute. Ab `install_package` directly: 0 LLM calls, ek approval.
    try:
        from ..graph.workers.code import install_clarification, install_intent

        intent = install_intent(text)
        if intent.get("detected"):
            if intent.get("ambiguous"):
                pending = {"package": intent.get("package") or "", "ts": time.time()}
                try:
                    get_state().set("pending_install", pending)
                except Exception:
                    pass
                answer = install_clarification(intent)
                return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                                   surface=surface, seconds=time.time() - started,
                                   status="needs_clarification", workers=["code"],
                                   execution_required=True,
                                   reasoning=["DECISION: install request ambiguous; one question",
                                              "ACTION: koi install abhi nahi chala"])
            return await _run_package_install(intent["package"], text, session_id, surface,
                                              started, emit)
    except Exception:
        pass

    # 1.6) an answered install clarification from the previous turn
    try:
        resolved = await _resolve_pending_install(text, session_id, surface, started, emit)
        if resolved is not None:
            return resolved
    except Exception:
        pass

    # 2) pure settings / mode commands natural language me
    if __import__("re").search(r"\b(mode|model|provider|owner name|language|personality)\b", low) and \
            __import__("re").search(r"\b(badal|set|karo|kar do|change)\b", low):
        import re as _re

        mgr_pairs: dict[str, str] = {}
        # mode: text|voice|telegram
        if _re.search(r"\bmode\b", low):
            mm = _re.search(r"\b(text|voice|telegram)\b", low)
            if mm:
                mgr_pairs["mode"] = mm.group(1)
        # provider names
        if _re.search(r"\bprovider\b", low):
            pp = _re.search(r"\b(gemini|openrouter|groq|openai|anthropic|xai|ollama|auto)\b", low)
            if pp:
                mgr_pairs["provider"] = pp.group(1)
        # language
        if _re.search(r"\b(language|bhasha)\b", low):
            ll = _re.search(r"\b(hindi|english|hinglish)\b", low)
            if ll:
                mgr_pairs["language"] = ll.group(1)
        # model: explicit "model X" pattern
        m = _re.search(r"\bmodel\b\s*(?:ko|=|:)?\s*([\w./:-]{4,60})", low)
        if m and m.group(1).lower() not in {"badal", "badlo", "change", "karo", "set"}:
            mgr_pairs["model"] = m.group(1)
        m2 = _re.search(r"owner\s*naam\s*([\w .]{2,40})", low)
        if m2:
            mgr_pairs["owner_name"] = m2.group(1).strip()
        if mgr_pairs:
            from ..security.permissions import check as permission_check

            if not permission_check("settings", surface).allowed:
                answer = "🔒 Is interface par runtime settings badalne ki permission nahi hai. Koi setting change nahi hui."
                return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                                   surface=surface, seconds=time.time() - started, status="blocked",
                                   workers=["permissions"], blocked_tools=["settings"])
            applied, errors = R.set_many(**mgr_pairs)
            if applied:
                try:
                    from ..llm import reset_cache

                    reset_cache()
                except Exception:
                    pass
            answer = ("✅ " + ", ".join(f"{k}={v}" for k, v in applied.items())) if applied else f"⚠️ {'; '.join(errors)}"
            return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                               surface=surface, seconds=time.time() - started, status="settings",
                               workers=["settings"])
    return None


_CLARIFY_MARKERS = ("🤔", "ka exact match clear nahi", "ek number ya naam bolo")
_CLARIFY_OPTION_RX = re.compile(r"^\s*(\d+)[\.\)]\s+(.+?)\s{2,}\(", re.M)


def _first_ambiguous_app(actions: list[dict[str, Any]]) -> tuple[str, list[dict]] | None:
    """Return `(requested_name, candidates)` for the first app whose match is not confident.

    Confidence uses the same 88 % threshold `open_any_app` enforces, so the question is asked
    *before* anything is launched rather than after a wrong app is already open.
    """
    try:
        from ..tools.app_finder import _APP_MEMORY, _app_memory_entry_valid, find_apps
    except Exception:
        return None
    for action in actions or []:
        if action.get("kind") != "open_app":
            continue
        name = str(action.get("app") or "").strip()
        if not name:
            continue
        # An empty result means "no index / nothing installed" (headless box), not "confusing".
        # Asking a question with zero options helps nobody; `open_any_app` already returns a
        # friendly "not found" for that case, so fall through and let it run.
        try:
            if not find_apps(name, 3, min_score=30):
                continue
        except Exception:
            continue
        # A name the owner already resolved is not ambiguous — otherwise the alias we just
        # saved would be ignored and the same question would come back on the next turn.
        try:
            remembered = _APP_MEMORY.lookup(name)
            if remembered and _app_memory_entry_valid(remembered):
                continue
        except Exception:
            pass
        try:
            matches = find_apps(name, 3, min_score=30)
        except Exception:
            continue
        from ..tools.app_finder import match_is_ambiguous

        if match_is_ambiguous(matches):
            return name, list(matches or [])
    return None


def _is_clarifying_output(output: str) -> bool:
    """True when a tool result is a question to the owner rather than a success/failure."""
    text = (output or "").strip()
    return bool(text) and any(marker in text for marker in _CLARIFY_MARKERS)


def _parse_clarification_options(answer: str) -> list[str]:
    return [name.strip() for _num, name in _CLARIFY_OPTION_RX.findall(answer or "")][:3]


# ---------------------------------------------------------------------------
# pending app-name clarification → alias learning
# ---------------------------------------------------------------------------
_NUMBER_WORDS = {"1": 1, "one": 1, "ek": 1, "2": 2, "two": 2, "do": 2, "3": 3, "three": 3, "teen": 3}


def _resolve_pending_app_clarification(text: str) -> tuple[str, str] | None:
    """Match the owner's reply against a stored app-clarification question.

    Returns `(query, chosen_app_name)` or None. Matching is by option number or by name, so
    "1", "notepad" and "notepad wala" all resolve.
    """
    try:
        from ..memory.session_state import get_state

        pending = get_state().get("pending_app_clarification") or {}
    except Exception:
        return None
    if not isinstance(pending, dict) or not pending.get("query"):
        return None
    options = [str(o) for o in (pending.get("options") or []) if o]
    query = str(pending.get("query") or "").strip()
    reply = " ".join((text or "").strip().lower().split())
    if not reply or not query:
        return None
    # a fresh command ("chrome kholo") is not an answer to the pending question
    if _looks_like_new_command(reply):
        return None
    index = _NUMBER_WORDS.get(reply.strip(" .!"))
    if index and 1 <= index <= len(options):
        return query, options[index - 1]
    for option in options:
        option_norm = _APP_NAME_KEY(option)
        reply_norm = _APP_NAME_KEY(reply)
        if option_norm and (option_norm == reply_norm or option_norm in reply_norm):
            return query, option
    return None


def _APP_NAME_KEY(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split())


_NEW_COMMAND_RX = re.compile(
    r"\b(?:open|kholo|khol\s*(?:de|do|na|ke)|launch|chalu\s*kar|chala\s*do|install|delete|"
    r"hatao|band\s*kar|close|save|banao|download|search|dhundo|dhoondo)\b", re.I,
)


def _looks_like_new_command(lowered: str) -> bool:
    return bool(_NEW_COMMAND_RX.search(lowered or ""))


async def _run_package_install(package: str, text: str, session_id: str, surface: str,
                               started: float, emit) -> AgentResult:
    """Run `pip install <package>` exactly once through the installed-package tool."""
    from ..security.permissions import check as permission_check

    if not permission_check("install_package", surface).allowed:
        answer = (f"🔒 Is interface par package install allowed nahi hai "
                  f"({permission_check('install_package', surface).reason}). Kuch install nahi hua.")
        return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                           surface=surface, seconds=time.time() - started, status="blocked",
                           workers=["permissions"], blocked_tools=["install_package"],
                           execution_required=True)

    from ..tools.code_engine import install_package

    emit("tool_start", f"install_package chala raha hoon: {package}", tool="install_package",
         detail=package)
    try:
        output = await install_package.ainvoke({"package": package})
    except Exception as exc:
        output = f"❌ install_package fail: {type(exc).__name__}: {exc}"
    ok = not str(output).lstrip().startswith(("❌", "🚫", "⚠️"))
    record = {"name": "install_package", "args": {"package": package}, "output": str(output)[:800],
              "ok": ok, "source": "deterministic"}
    emit("tool_end", f"install_package: {str(output)[:100]}", tool="install_package",
         status="ok" if ok else "fail")
    answer = str(output)
    result = AgentResult(
        text, answer, spoken=summary_for_speech(answer), session_id=session_id, surface=surface,
        seconds=time.time() - started, status="done" if ok else "failed", workers=["code"],
        plan=[f"pip install {package}"],
        reasoning=["DECISION: clear package install; no LLM planning, no script generation",
                   "ACTION: 1 install_package call (ek Gate approval)"],
        findings=[{"worker": "code", "summary": answer, "tool_calls": [record]}],
        errors=[] if ok else [str(output)[:300]],
        tool_evidence=[record], execution_required=True,
    )
    result.recompute_execution_flags()
    return result


async def _resolve_pending_install(text: str, session_id: str, surface: str,
                                   started: float, emit) -> AgentResult | None:
    """Consume a stored install clarification once the owner answers it."""
    try:
        pending = get_state().get("pending_install") or {}
    except Exception:
        return None
    if not isinstance(pending, dict) or not pending:
        return None
    reply = " ".join((text or "").strip().lower().split())
    if not reply or _looks_like_new_command(reply):
        return None
    if reply in {"haan", "yes", "y", "ha", "kar do", "karo", "chala do", "ok", "okay", "theek hai"}:
        package = str(pending.get("package") or "")
        if not package:
            return None
        try:
            get_state().set("pending_install", {})
        except Exception:
            pass
        return await _run_package_install(package, text, session_id, surface, started, emit)
    # a bare package name is the answer
    tokens = [t for t in re.findall(r"[A-Za-z][A-Za-z0-9._-]{1,40}", reply)]
    if len(tokens) == 1:
        try:
            get_state().set("pending_install", {})
        except Exception:
            pass
        return await _run_package_install(tokens[0], text, session_id, surface, started, emit)
    return None


async def _apply_pending_app_clarification(text: str, session_id: str, surface: str,
                                           emit) -> AgentResult | None:
    """Resolve a stored app-name question, save the alias, then run the open once."""
    from ..memory.session_state import get_state

    match = _resolve_pending_app_clarification(text)
    if match is None:
        return None
    query, chosen = match
    try:
        from ..tools.app_finder import save_alias_choice
    except Exception:
        save_alias_choice = None  # type: ignore[assignment]
    saved, note = (False, "alias helper unavailable")
    if save_alias_choice is not None:
        saved, note = save_alias_choice(query, chosen)
    try:
        get_state().set("pending_app_clarification", {})
    except Exception:
        pass

    from ..graph.nodes import _desktop_tools_and_blocked, _execute_desktop_action

    tools, _ = _desktop_tools_and_blocked(surface)
    calls = await _execute_desktop_action({"actions": [{"kind": "open_app", "app": chosen}]}, tools)
    ok = bool(calls) and all(bool(c.get("ok")) for c in calls)
    detail = "\n".join(str(c.get("output") or "").strip() for c in calls if c.get("output"))
    answer = (f"✅ '{query}' = {chosen} yaad rakh liya; ab se ye naam seedha wahi kholega.\n"
              f"{note}\n{detail}".strip())
    if not ok:
        answer = (f"⚠️ '{query}' → {chosen} alias to save ho gaya, par abhi launch verify nahi hua.\n"
                  f"{note}\n{detail}".strip())
    emit("tool_end", f"alias saved: {query} -> {chosen}", tool="open_any_app",
         status="ok" if ok else "fail")
    result = AgentResult(
        text, answer, spoken=summary_for_speech(answer), plan=[f"Open {chosen}"],
        workers=["desktop"], session_id=session_id, surface=surface,
        status="done" if ok else "partial_failure", tool_evidence=calls,
        execution_required=True,
        reasoning=[f"DECISION: owner answered the pending question with '{chosen}'",
                   "ACTION: alias saved, then one verified open"],
        errors=[] if ok else [detail or "launch verify nahi hua"],
    )
    result.recompute_execution_flags()
    return result


_COMPOUND_TASK_INTENT_RX = re.compile(
    r"\b(?:search|research|find|dhoondh|compare|weather|mausam|news|price|calculate|code|script|"
    r"create|save|read|download|upload|send|mail|message|play|click|press|run|execute|focus|screenshot)\b",
    re.I,
)


def _has_unhandled_compound_task(text: str, detect_action) -> bool:
    """Do not fast-path an app open if a later actionable clause would be discarded."""
    clauses = re.split(r"\b(?:and|aur|then|phir|uske\s+baad)\b", text or "", flags=re.I)
    if len(clauses) < 2:
        return False
    for clause in clauses:
        if detect_action(clause).get("actions"):
            continue
        if _COMPOUND_TASK_INTENT_RX.search(clause):
            return True
    return False


async def _direct_app_request(text: str, session_id: str, surface: str, emit) -> AgentResult | None:
    """Handle clear ordered app actions; leave mixed compound tasks intact for graph planning."""
    from ..memory.session_state import get_state

    try:
        from ..graph.nodes import _desktop_tools_and_blocked, _execute_desktop_action, detect_desktop_action
    except Exception:
        return None
    action = detect_desktop_action(text)
    actions = action.get("actions") or []
    if not actions or not any(a.get("kind") == "open_app" for a in actions):
        return None
    if _has_unhandled_compound_task(text, detect_desktop_action):
        return None
    if len(actions) > 6 or any(a.get("kind") not in {"open_app", "type_text"} for a in actions):
        return None

    from ..security.permissions import check as permission_check

    required = {"open_app" if a.get("kind") == "open_app" else "type_text" for a in actions}
    denied = [name for name in required if not permission_check(name, surface).allowed]
    if denied:
        answer = (f"🔒 Is interface par app/desktop control allowed nahi ({', '.join(denied)}). "
                  "Koi app launch nahi kiya.")
        return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                           surface=surface, status="blocked", workers=["permissions"],
                           blocked_tools=denied, execution_required=True,
                           errors=["desktop action blocked by surface permission"])

    # An ambiguous app name is the more blocking problem, so it is resolved first: opening
    # the wrong app is worse than not knowing what to type yet.
    ambiguous_app = _first_ambiguous_app(actions)
    if ambiguous_app is not None:
        app_name, matches = ambiguous_app
        from ..tools.app_finder import _clarify_app

        answer = _clarify_app(app_name, matches)
        try:
            get_state().set("pending_app_clarification", {
                "query": app_name, "kind": "app_name", "ts": time.time(),
                "options": _parse_clarification_options(answer),
            })
        except Exception:
            pass
        emit("warn", "Clarification requested: app name is ambiguous", status="skipped")
        return AgentResult(
            text, answer, spoken=summary_for_speech(answer), workers=["desktop"],
            plan=[f"Open {app_name}"], session_id=session_id, surface=surface,
            status="needs_clarification", execution_required=True,
            reasoning=["DECISION: app name ambiguous; asked one question instead of guessing",
                       f"ACTION: no app launched; {len(matches)} candidate(s) offered"],
            errors=[],
        )

    # "notepad kholke likho" with no payload text → ask once what to write, never guess.
    missing_text = any(a.get("kind") == "type_text" and not str(a.get("text") or "").strip()
                       for a in actions)
    if missing_text:
        pending_apps = [a.get("app") for a in actions if a.get("kind") == "open_app"]
        try:
            get_state().set("pending_app_clarification", {
                "query": (pending_apps[0] if pending_apps else ""),
                "kind": "missing_text", "ts": time.time(),
            })
        except Exception:
            pass
        target = pending_apps[0] if pending_apps else "app"
        answer = (f"🤔 {target} khol dunga, par likhne ke liye kya hai? Ek line me text bolo "
                  "to main wahi type kar dunga. (Andaza laga kar kuch nahi likhunga.)")
        emit("warn", "Clarification requested: no text to type", status="skipped")
        return AgentResult(
            text, answer, spoken=for_speech(answer), plan=[f"Open {target}", "Type the owner's text"],
            workers=["desktop"], session_id=session_id, surface=surface,
            status="needs_clarification", execution_required=True,
            reasoning=["DECISION: typing requested with no payload text; ask one question",
                       "ACTION: nothing executed yet"],
            errors=[],
        )

    tools, _ = _desktop_tools_and_blocked(surface)
    plan = ["Identify the requested application", "Focus its existing window or launch it once"]
    if any(a.get("kind") == "type_text" for a in actions):
        plan.append("Type the requested text only after the app action returns")
    emit("plan", f"Direct desktop action ({len(actions)} step(s))", detail=" → ".join(plan))
    calls = await _execute_desktop_action(action, tools)
    failures = [c for c in calls if not c.get("ok")]
    outputs = [str(c.get("output") or "").strip() for c in calls]

    # An ambiguous app name must produce ONE clarifying question, not a refusal and not a
    # wrong-app launch. The pending choice is stored so the owner's answer becomes an alias.
    clarifications = [c for c in calls if _is_clarifying_output(str(c.get("output") or ""))]
    if clarifications:
        answer = "\n".join(str(c.get("output") or "").strip() for c in clarifications).strip()
        opened_app = next((str(a.get("app")) for a in actions if a.get("kind") == "open_app"), "")
        try:
            get_state().set("pending_app_clarification", {
                "query": opened_app, "kind": "app_name", "ts": time.time(),
                "options": _parse_clarification_options(answer),
            })
        except Exception:
            pass
        emit("warn", "Clarification requested: app name is ambiguous", status="skipped")
        return AgentResult(
            text, answer, spoken=summary_for_speech(answer), workers=["desktop"],
            plan=[f"Open {opened_app}"], session_id=session_id, surface=surface,
            status="needs_clarification", execution_required=True, tool_evidence=calls,
            reasoning=["DECISION: app name ambiguous; asked one question instead of guessing",
                       "ACTION: no app launched yet"],
            errors=[],
        )

    if failures:
        answer = "⚠️ App request verify nahi hua; maine success claim nahi ki.\n" + "\n".join(
            f"• {c.get('name')}: {str(c.get('output') or '')[:240]}" for c in failures[:3])
        status = "partial_failure" if len(failures) < len(calls) else "failed"
    else:
        answer = "\n".join(out for out in outputs if out) or "⚠️ Tool ne koi result nahi diya; launch verify nahi hua."
        status = "done" if outputs else "failed"
    result = AgentResult(
        text, answer, spoken=summary_for_speech(answer), plan=plan, workers=["desktop"],
        reasoning=["DECISION: clear app-open intent; skipped LLM planning",
                   f"ACTION: {len(calls)} actual desktop tool call(s)",
                   f"VERIFICATION: {len(calls) - len(failures)} ok, {len(failures)} failed"],
        findings=[{"worker": "desktop", "summary": answer, "tool_calls": calls}],
        errors=[str(c.get("output") or "desktop action failed") for c in failures],
        status=status, tool_evidence=calls, execution_required=True,
        session_id=session_id, surface=surface,
    )
    result.recompute_execution_flags()
    return result



_CHAT_TASK_ACTION_RX = re.compile(
    r"\b(?:open(?![-\s]+source)|opens|opened|close|closes|closed|launch\w*|start\w*|type|write|save|delete|remove|rename|"
    r"move|install|download|upload|search|research|look\s+up|browse|scrape|fetch|find|locate|compare|google|"
    r"press|click|execute|run|send|message|schedule|remind(?:er)?|remember|forget|create|make|build|read|copy|"
    r"paste|edit|screenshot|kholo|khol(?:o|na|ke|kar)?|kholke|krke|karke|kar\s+ke|kar\s+do|karo|"
    r"banao|bana(?:\s+do|\s+ke)?|likho|likh(?:\s+do|\s+de|\s+ke)?|hatao|chalao|chala\s+do|bhejo|"
    r"dhundo|dhoondo|dhoondho|dhoondh(?:o|ho)?|dikhao|band\s+kar(?:o|do)?"
    r")\b",
    re.I,
)
_CHAT_INSTRUCTION_RX = re.compile(
    r"\b(?:how\s+(?:do|can|should)\s+i|how\s+to|kaise|kaisay|steps\s+to|explain|tutorial)\b", re.I
)
_CHAT_CURRENT_FACT_RX = re.compile(
    r"\b(?:latest|current|live|today'?s\s+(?:weather|news|price)|near\s+me|nearby)\b", re.I
)
_CHAT_ASSET_RX = (
    r"notepad|chrome|browser|calculator|explorer|vs\s*code|vscode|desktop|folder|folders|file|files|"
    r"notes?\.(?:txt|md|json|csv)|[\w.-]+\.(?:txt|md|json|csv|pdf|docx|xlsx)"
)
_CHAT_ASSET_LOOKUP_RX = re.compile(
    rf"\b(?:where(?:'s|\s+is|\s+are)?|kahan|kidhar|locate|find|list|show|open|read|search\s+for|"
    rf"what(?:'s|\s+is)\s+in|mere\s+(?:desktop|folder|file)|my\s+(?:desktop|folder|file)|"
    rf"on\s+(?:my\s+)?desktop|in\s+(?:my\s+)?folder)\b.{{0,80}}\b(?:{_CHAT_ASSET_RX})\b",
    re.I,
)
_CHAT_CURRENT_DATA_RX = re.compile(r"\b(?:weather|mausam|forecast|latest\s+news|stock\s+price|prices?)\b", re.I)
_CHAT_CLEAR_CONVERSATION_RX = re.compile(
    r"\b(?:thanks?|thank\s+you|shukriya|dhanyavaad|i\s+(?:am|['’]m|feel|felt|think|love|like|hate|"
    r"need\s+(?:(?:a\s+little|some|more)\s+)?(?:motivation|encouragement|support|reassurance))|what\s+do\s+you\s+think|"
    r"what\s+is\s+your\s+take|do\s+you\s+like|how\s+are\s+you|how\s+r\s+u|"
    r"that\s+(?:.{0,30}\b(?:was|is)\b.{0,25}\b(?:funny|good|great|nice)\b|"
    r"made\s+me\s+(?:laugh|smile))|"
    r"you\s+made\s+me\s+(?:laugh|smile)|i\s+had\s+.*\s+day|"
    r"i(?:['’]m|\s+am)\s+(?:feeling|feel|bored|tired|nervous|excited|happy|sad|anxious|proud)|"
    r"i\s+(?:finally\s+)?(?:finished|read|watched|heard|enjoyed)\b|i\s+was\s+thinking\b|"
    r"my\s+day\s+(?:was|has\s+been|is)\b|mujhe\s+.*(?:motivation|encouragement|pasand)|"
    r"kya\s+(?:scene|haal)|mood\s+.*\s+off|just\s+chat|bas\s+baat|baat\s+karo)\b",
    re.I,
)
_CHAT_CONVERSATION_OVERRIDE_RX = re.compile(
    r"\b(?:no\s+questions\s*,?\s*just\s+chat|mat\s+poocho\s*,?\s*(?:bas\s+)?baat\s+karo|"
    r"bas\s+mu[j]?h?se\s+baat\s+karo|just\s+chat\s+with\s+me|chat\s+karo)\b",
    re.I,
)
_CHAT_GENERAL_QUESTION_RX = re.compile(
    r"^(?:who|what|when|where|why|which|how|is|are|do|does|did|can\s+you\s+tell\s+me|tell\s+me|"
    r"explain|kya|kaun|kyun|kaise|kaisa|kaisi|what['’]s|who['’]s)\b",
    re.I,
)
# "kaisa hai re" / "kaisi ho" / "kya haal hai" — everyday Hinglish check-ins. Without this
# they fell through to the LangGraph task pipeline (the owner's 63.7 s / 5-call failure).
_CHAT_MOOD_RX = re.compile(
    r"\b(?:kaisa|kaisi|kaise)\s+(?:hai|ho|hain|he|re|bhai|yar|yaar|tu|tum|aap)\b"
    r"|\b(?:kya|kaisa|kaisi)\s+haal\b|\bhaal\s+(?:kaisa|kaisi|chaal)\b"
    r"|\b(?:how\s+(?:are|r)\s+(?:you|u)|how\s+are\s+you\s+doing|how\s+do\s+you\s+do)\b"
    r"|\b(?:tu|tum)\s+(?:kaisa|kaisi|kaise)\b|\bsab\s+(?:theek|thik)\b",
    re.I,
)
_CHAT_DIRECT_ACTION_REQUEST_RX = re.compile(
    r"^(?:(?:please|plz|jarvis|boss|bhai)[,\s]+)*(?:open|close|launch|start|type|write|save|delete|remove|"
    r"rename|move|install|download|upload|search|find|browse|run|execute|send|create|make|build|click|press|"
    r"kholo|khol|kholke|krke|karke|kar\s+do|karo|banao|bana\s+do|likho|likh\s+do|hatao|chalao|bhejo|"
    r"dhundo|dhoondo)\b|\b(?:can|could|would)\s+you\s+(?:please\s+)?(?:open|close|launch|start|type|write|"
    r"save|delete|remove|rename|move|install|download|upload|search|find|run|execute|send|create|make|build)\b"
    # "hata do", "band kar do", "bhej do", "daal do" — the split imperative is as common in
    # Hinglish as the single verb, and the single-verb list above did not match it.
    r"|\b(?:hata|hatana|band|bhej|bhejo|de|le|kar|karo|bana|likh|khol|chalao|daal|save|delete|remove|"
    r"rename|move|copy|paste|undo|redo|refresh|restart|set|change|update|clear)\s+(?:do|de|dena|lo|lena)\b",
    re.I,
)


def is_chat_request(text: str) -> bool:
    """Default to task; use the one-call chat path only for clearly conversational input."""
    low = " ".join((text or "").strip().lower().split())
    if not low or _is_time_question(low):
        return False
    if is_trivial_greeting(text):
        return True
    if _CHAT_CONVERSATION_OVERRIDE_RX.search(low):
        return True
    if _CHAT_INSTRUCTION_RX.search(low) and not _CHAT_DIRECT_ACTION_REQUEST_RX.search(low) \
            and not re.search(r"\b(?:for me|mere liye|do it|please do|kar do|karo)\b", low):
        return True
    if _CHAT_TASK_ACTION_RX.search(low) or _CHAT_CURRENT_FACT_RX.search(low) \
            or _CHAT_CURRENT_DATA_RX.search(low) or _CHAT_ASSET_LOOKUP_RX.search(low):
        return False
    if _CHAT_CLEAR_CONVERSATION_RX.search(low):
        return True
    if _CHAT_MOOD_RX.search(low):
        return True
    if _CHAT_GENERAL_QUESTION_RX.search(low):
        return True
    # Short filler ("acha", "theek hai", "hmm", "aur batao") is not a task. Sending it through
    # the full graph costs recall + route + plan + worker + synthesize + critic — the owner's
    # 25-message casual session was paying that on almost every turn. Only apply this when the
    # message is short, reads as no kind of command, and the desktop parser sees no action in
    # it: `is_chat_request` runs *before* `_direct_app_request`, so an over-eager rule here
    # would silently stop "notepad kholo" from ever reaching the desktop worker.
    if len(low.split()) <= 5 and not _CHAT_DIRECT_ACTION_REQUEST_RX.search(low):
        try:
            from ..graph.workers.desktop_actions import detect_desktop_action

            if not (detect_desktop_action(text) or {}).get("actions"):
                return True
        except Exception:
            pass
    # An unfamiliar imperative/request is safer in the task pipeline than as an unverified chat answer.
    return False


# Greeting replies rotate so two consecutive "hy" never read as the same canned line.
# Used only when every chat model is unavailable/cooled down — never in place of a real reply.
_GREETING_FALLBACKS: tuple[str, ...] = (
    "Haaji bulao, main yahin hoon.",
    "Boss, bolo — sun raha hoon.",
    "Haanji? Kaam batao ya bas baat karni hai.",
    "Present sir. Kya chal raha hai?",
    "Hey! Der ho gayi thi aapki. Bolo.",
    "Main ready hoon. Kya karna hai?",
)
_GREETING_FALLBACK_STATE = {"index": 0}


def _offline_chat_fallback(text: str) -> str:
    """Honest, varied reply when no chat model can be reached this turn."""
    if is_trivial_greeting(text):
        index = _GREETING_FALLBACK_STATE["index"] % len(_GREETING_FALLBACKS)
        _GREETING_FALLBACK_STATE["index"] = index + 1
        return _GREETING_FALLBACKS[index]
    return ("Abhi chat reply generate nahi ho paaya (model unavailable ya cooldown me hai); "
            "main guess kar ke kuch nahi bataunga. Thodi der mein dobara try karein.")


async def _chat_fast_path(text: str, session_id: str, surface: str, emit) -> AgentResult:
    """One-model-call chat path; no graph routing, planner, tools, synthesis, or critic."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from ..llm import get_chat_model
    from ..memory.store import get_memory
    from ..prompts import chat_system_prompt

    started = time.monotonic()
    memory_context = ""
    follow_up_allowed = True
    try:
        memory = get_memory()
        recent = memory.recent(limit=8, session=session_id)
        memory_context = memory.relevant_context(
            text, token_budget=420, limit=5, session=session_id, recent_limit=6,
        )
        recent_assistant = [row for row in recent[-8:] if row.get("role") == "assistant"]
        follow_up_allowed = not any("?" in str(row.get("content") or "") for row in recent_assistant[-3:])
    except Exception:
        memory_context = ""

    if re.search(r"\b(?:mat\s+poocho|mat\s+puchho|don't\s+ask|do\s+not\s+ask|no\s+questions)\b", text, re.I):
        follow_up_allowed = False

    prompt = chat_system_prompt(follow_up_allowed=follow_up_allowed)
    messages = [SystemMessage(content=prompt)]
    if memory_context:
        messages.append(SystemMessage(content=memory_context))
    messages.append(HumanMessage(content=text))

    label = "brain/chat"
    emit("model_start", "Chat reply started", tool=label, status="running")
    try:
        response = await get_chat_model("brain").ainvoke(messages)
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = " ".join(str(part.get("text", "")) if isinstance(part, dict) else str(part)
                                for part in content)
        answer = str(content or "").strip()
        if not answer:
            raise ValueError("chat model returned an empty reply")
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        emit("model_end", "Chat reply unavailable", tool=label, ms=elapsed, status="fail")
        answer = _offline_chat_fallback(text)
        result = AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                             surface=surface, seconds=time.monotonic() - started, status="failed",
                             workers=["chat"], errors=[f"chat model: {type(exc).__name__}: {str(exc)[:180]}"])
        result.events = [{"node": "chat", "status": "failed", "ms": elapsed}]
        return result

    elapsed = int((time.monotonic() - started) * 1000)
    emit("model_end", "Chat reply ready", tool=label, ms=elapsed, status="ok")
    emit("trace", "route=chat · planner=skipped · tools=0 · critic=skipped", ms=elapsed, status="ok")
    result = AgentResult(
        text, answer, spoken=summary_for_speech(answer, max_sentences=3), session_id=session_id,
        surface=surface, seconds=time.monotonic() - started, status="chat", workers=["chat"],
        reasoning=["DECISION: conversational request; direct chat path", "ACTION: one brain call; no tools/planner/critic"],
    )
    result.events = [{"node": "chat", "status": "done", "ms": elapsed, "llm_calls": 1,
                     "tools": 0, "planner": "skipped", "critic": "skipped"}]
    return result


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------
async def run_agent(
    text: str,
    session_id: str = "cli",
    surface: str = "cli_owner",
    use_fast_path: bool = True,
    voice_output: bool = False,
    on_progress: Any = None,
    memory_user_id: str = "nikhil_mehra",
    run_id: str | None = None,
) -> AgentResult:
    """Ek request chalao — fast paths → LangGraph → structured result.

    `on_progress`: optional callable(ProgressEvent) — CLI/Telegram real phase/tool/model
    status dikhate hain. Owner modes share one memory; guest identities get isolated stores.
    """
    started = time.time()
    from ..memory.session_state import use_state_user
    from ..memory.store import use_memory_user
    from ..security.permissions import use_surface
    from .progress import CallbackSink, emit, use_sink

    with use_memory_user(memory_user_id), use_state_user(memory_user_id), use_surface(surface):
        result = await _run_agent_inner(text, session_id, surface, use_fast_path, voice_output,
                                        started, on_progress, CallbackSink, emit, use_sink, run_id)
    result.run_id = str(run_id or result.run_id or "")
    return result


async def _run_agent_inner(text, session_id, surface, use_fast_path, voice_output,
                           started, on_progress, CallbackSink, emit, use_sink, run_id=None):
    # agar caller ne apna callback diya to wahi sink use karo, warna bahar wala (contextvar) sink
    sink = CallbackSink(on_progress) if on_progress else None
    if sink is None:
        emit("start", f"Request mila: {text[:80]}", detail=text)
        return await _run_agent_body(text, session_id, surface, use_fast_path, voice_output,
                                     started, emit, run_id)
    with use_sink(sink):
        emit("start", f"Request mila: {text[:80]}", detail=text)
        return await _run_agent_body(text, session_id, surface, use_fast_path, voice_output,
                                     started, emit, run_id)


async def _run_agent_body(text, session_id, surface, use_fast_path, voice_output, started, emit, run_id=None):
    text = (text or "").strip()
    if not text:
        return AgentResult(text, "kuch bola nahi 😅", session_id=session_id, surface=surface)

    result = AgentResult(text, session_id=session_id, surface=surface)
    emit("thinking", "Samajh raha hoon + memory check kar raha hoon…")

    # session state: task log (loop prevention ke liye)
    try:
        get_state().add_task(text[:200], workers=[], status="running")
    except Exception:
        pass

    if use_fast_path:
        emit("thinking", "Fast-path check (commands / registry / session state)…")
        fast = await _fast_paths(text, session_id, surface, emit)
        if fast is not None:
            fast.seconds = time.time() - started
            _post_run(fast, session_id, emit=emit)
            fast.seconds = time.time() - started
            _emit_final_status(emit, fast)
            return fast

        # A question we just asked owns the next turn. The answer is often a bare noun
        # ("notepad", "requests") which the router would happily read as small talk.
        resolved = await _apply_pending_app_clarification(text, session_id, surface, emit)
        if resolved is not None:
            resolved.seconds = time.time() - started
            _post_run(resolved, session_id, emit=emit)
            resolved.seconds = time.time() - started
            _emit_final_status(emit, resolved)
            return resolved

        if is_chat_request(text):
            chat = await _chat_fast_path(text, session_id, surface, emit)
            chat.seconds = time.time() - started
            _post_run(chat, session_id, emit=emit)
            chat.seconds = time.time() - started
            _emit_final_status(emit, chat)
            return chat

        direct = await _direct_app_request(text, session_id, surface, emit)
        if direct is not None:
            direct.seconds = time.time() - started
            _post_run(direct, session_id, emit=emit)
            direct.seconds = time.time() - started
            _emit_final_status(emit, direct)
            return direct

    if not use_fast_path and is_chat_request(text):
        chat = await _chat_fast_path(text, session_id, surface, emit)
        chat.seconds = time.time() - started
        _post_run(chat, session_id, emit=emit)
        chat.seconds = time.time() - started
        _emit_final_status(emit, chat)
        return chat

    try:
        from ..graph.build import jarvis_ainvoke

        emit("thinking", "LangGraph workflow start (recall → route → plan → act → verify)…")
        state = await jarvis_ainvoke(text, session_id=session_id, surface=surface, run_id=run_id)
        result.answer = state.get("answer") or state.get("response") or ""
        result.plan = state.get("plan") or []
        result.workers = state.get("workers") or []
        result.findings = state.get("findings") or []
        result.artifacts = state.get("artifacts") or []
        result.errors = state.get("errors") or []
        result.blocked_tools = state.get("blocked_tools") or []
        result.iterations = state.get("iterations") or 0
        result.critique = state.get("critique") or {}
        result.requirements = state.get("requirements") or {}
        result.events = state.get("events") or []
        from ..graph.synthesis import build_reasoning_summary

        result.reasoning = state.get("reasoning_summary") or build_reasoning_summary(state)
        result.status = state.get("status") or "done"
        result.memory_persisted = bool(state.get("memory_persisted"))
        result.task_id = str(state.get("task_id") or "")
        # evidence-based flags (hard invariant) — state se, guess se nahi
        from ..runtime.evidence import evidence_records, is_tool_required

        result.tool_evidence = evidence_records(state)[:24]
        result.execution_required = bool(is_tool_required(state, workers=result.workers))
        result.recompute_execution_flags()
        plan_lines = [str(x) for x in (result.plan or [])][:6]
        if plan_lines:
            emit("plan", f"{len(result.plan)} step plan tayyar", detail=" | ".join(plan_lines))
        calls_ = [c for f in (result.findings or []) for c in (f.get("tool_calls") or [])]
        if calls_:
            failed = [c for c in calls_ if not c.get("ok", True)]
            emit("verify", f"{len(calls_)} tool call verify — {len(failed)} fail",
                 status="fail" if failed else "ok")
        spoken = state.get("spoken") or ""
        result.spoken = for_speech(spoken) if spoken else summary_for_speech(result.answer)
    except Exception as exc:
        result.status = "error"
        result.errors = [str(exc)]
        result.verified_execution = False
        emit("error", f"Agent crash: {type(exc).__name__}: {str(exc)[:160]}", status="fail")
        result.answer = (f"⚠️ Agent chalane me dikkat aayi: {str(exc)[:300]}\n"
                         "Try: `python -m jarvis doctor` (config check) ya `python -m jarvis doctor --fix`.")
        result.spoken = for_speech(result.answer)

    if voice_output and not result.spoken:
        result.spoken = summary_for_speech(result.answer)
    _post_run(result, session_id, emit=emit)
    result.seconds = time.time() - started
    _emit_final_status(emit, result)
    return result


def _emit_final_status(emit, result: AgentResult) -> None:
    """Terminal progress must match the result; an error never becomes a green done event."""
    status = (result.status or "").lower()
    elapsed_ms = int(max(0.0, result.seconds) * 1000)
    if status in {"failed", "error", "blocked", "cancelled"}:
        emit("error", f"Request {status}; kaam complete nahi hua", status="fail", ms=elapsed_ms)
    elif status in {"needs_clarification", "needs_user"}:
        emit("warn", "Clarification requested; action has not run yet", status="skipped", ms=elapsed_ms)
    elif result.errors or status in {"partial_failure", "done_with_errors"}:
        emit("warn", f"Request finished with {len(result.errors)} error(s)", status="fail", ms=elapsed_ms)
    else:
        emit("done", f"Request complete ({result.seconds:.1f}s)", status="ok", ms=elapsed_ms)


def _post_run(result: AgentResult, session_id: str, emit=None) -> None:
    """Persist one turn, close one task row, and surface every persistence failure."""
    def persistence_error(where: str, exc: Exception) -> None:
        msg = f"{where} failed ({type(exc).__name__}: {str(exc)[:120]})"
        if msg not in result.errors:
            result.errors.append(msg)
        if result.status not in {"failed", "error", "blocked", "cancelled", "needs_user"}:
            result.status = "partial_failure"
        if emit:
            emit("error", msg, status="fail")

    if result.errors and result.status not in {"failed", "error", "blocked", "cancelled", "needs_user", "partial_failure"}:
        result.status = "partial_failure"
    mem = None
    try:
        from ..memory.store import get_memory
        mem = get_memory()
    except Exception as exc:
        persistence_error("memory store initialization", exc)

    if mem is not None and not result.memory_persisted:
        try:
            mem.add_turn(result.request, result.answer[:1500], session=session_id)
            result.memory_persisted = True
            if emit:
                emit("thinking", "Conversation turn saved to shared memory store")
        except Exception as exc:
            persistence_error("conversation memory persistence", exc)

    if mem is not None and not result.task_id:
        try:
            result.task_id = mem.start_task(result.request, route=",".join(result.workers)) or ""
        except Exception as exc:
            persistence_error("task creation", exc)
    if mem is not None and result.task_id:
        try:
            mem.finish_task(result.task_id, result.status, result=result.answer[:1200],
                            plan=result.plan, artifacts=result.artifacts, requirements=result.requirements)
        except Exception as exc:
            persistence_error("task finalization", exc)

    # Update the single short-term task row created before the graph started.
    try:
        state = get_state()
        state.finish_task(status=result.status, result=result.answer[:300], artifacts=result.artifacts)
        if result.errors:
            state.remember_error(str(result.errors[0])[:200])
    except Exception as exc:
        persistence_error("session-state finalization", exc)

    if result.errors and result.status not in {"failed", "error", "blocked", "cancelled", "needs_user", "partial_failure"}:
        result.status = "partial_failure"
    try:
        from ..llm import model_attempts

        result.events = list(result.events) + [{"node": "model_attempts", "events": model_attempts(5)}]
    except Exception:
        pass


async def stream_agent(
    text: str,
    session_id: str = "cli",
    surface: str = "cli_owner",
    memory_user_id: str = "nikhil_mehra",
    use_fast_path: bool = True,
    voice_output: bool = False,
    run_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream progress from the same runner as one-shot calls (fast paths + user context included)."""
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_progress(event) -> None:
        payload = asdict(event) if hasattr(event, "__dataclass_fields__") else dict(event)
        loop.call_soon_threadsafe(queue.put_nowait, {
            "node": "progress", "update_keys": ["events"], "events": [payload], "event": payload,
        })

    async def run() -> AgentResult:
        try:
            return await run_agent(
                text,
                session_id=session_id,
                surface=surface,
                use_fast_path=use_fast_path,
                voice_output=voice_output,
                on_progress=on_progress,
                memory_user_id=memory_user_id,
                run_id=run_id,
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    task = asyncio.create_task(run())
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk
        result = await task
        yield {
            "node": "final",
            "answer": result.answer,
            "spoken": result.spoken,
            "status": result.status,
            "result": result.as_dict(),
        }
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
