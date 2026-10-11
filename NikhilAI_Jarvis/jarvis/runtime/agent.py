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

import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..config import settings as S
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
            "iterations": self.iterations, "critique": self.critique, "seconds": self.seconds,
            "session_id": self.session_id, "surface": self.surface, "status": self.status,
            "blocked_tools": self.blocked_tools,
            "execution_required": self.execution_required,
            "verified_execution": self.verified_execution,
            "tool_evidence": self.tool_calls_flat[:24], "memory_updates": self.memory_updates,
        }


# ---------------------------------------------------------------------------
# time / date intent — local clock se answer, kabhi guess nahi
# ---------------------------------------------------------------------------
_TIME_KEYS = ("time", "samay", "date", "tareekh", "tarikh", "din", "day", "timestamp",
              "utc", "timezone", "time zone", "clock", "ghadi", "baje", "baj", "o'clock")
_TIME_BLOCKERS = ("mausam", "weather", "temperature", "temp", "news", "schedule", "yaad dila",
                  "remind", "automation", "har subah", "har shaam", "daily", "every ", "khol",
                  "open", "launch", "search", "research", "laao", "browser", "save", "likh",
                  "bana", "create", "code", "file", "note", "mail", "bhej", "call", "video",
                  "invoice", "report", "download", "upload", "install", "scan", "clean")


def _is_time_question(low: str) -> bool:
    """Pure time/date sawaal? (task wali request ko hijack nahi karta.)"""
    t = (low or "").strip()
    if not t or len(t.split()) > 12:
        return False
    if any(b in t for b in _TIME_BLOCKERS):
        return False
    return any(k in t for k in _TIME_KEYS)


def _time_answer(text: str) -> str:
    """Local clock se seedha answer (date / time / din / utc / kal-parso sab)."""
    from datetime import datetime, timedelta, timezone

    from ..tools.web_tools import IST

    low = text.lower()
    now = datetime.now(IST)
    # order matters: "beeta kal"/"yesterday" ko pehle pakdo, warna "kal" branch le lega
    if any(k in low for k in ("beeta kal", "bita kal", "guzra kal", "yesterday")):
        target, label = now - timedelta(days=1), "Kal (beeta)"
    elif any(k in low for k in ("parso", "parson", "day after tomorrow")):
        target, label = now + timedelta(days=2), "Parso"
    elif any(k in low for k in ("kal", "tomorrow")):
        target, label = now + timedelta(days=1), "Kal"
    else:
        target, label = now, "Aaj"
    utc = datetime.now(timezone.utc)
    off = now.utcoffset()
    off_s = (f"UTC{'+' if off.total_seconds() >= 0 else '-'}{abs(off).seconds // 3600:02d}:"
             f"{(abs(off).seconds // 60) % 60:02d}") if off else "UTC"
    hindi_day = {"Monday": "Somvaar", "Tuesday": "Mangalvaar", "Wednesday": "Budhvaar",
                 "Thursday": "Guruvaar", "Friday": "Shukravaar", "Saturday": "Shanivaar",
                 "Sunday": "Ravivaar"}.get(target.strftime("%A"), "")
    want_time = any(k in low for k in ("time", "samay", "baje", "baj", "clock", "ghadi", "utc", "timestamp"))
    want_date = any(k in low for k in ("date", "tareekh", "tarikh", "din", "day"))
    lines: list[str] = []
    if want_time or not want_date:
        lines.append(f"🕒 {label}: {target.strftime('%I:%M %p')} ({now.tzname() or 'IST'}, {off_s})")
    if want_date or not want_time:
        lines.append(f"📅 {label} ki date: {target.strftime('%d %B %Y')} — {target.strftime('%A')}"
                     + (f" ({hindi_day})" if hindi_day else ""))
    lines.append(f"🌍 UTC: {utc.strftime('%I:%M %p')} • epoch: {int(target.timestamp())}")
    lines.append("_(ye local system clock se aaya hai — guess nahi.)_")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# fast-path handlers (LLM se pehle deterministic kaam — sasta + bharosemand)
# ---------------------------------------------------------------------------
async def _fast_paths(text: str, session_id: str, surface: str) -> AgentResult | None:
    from ..config import runtime as R
    from ..memory.manager import get_manager, is_memory_request, strip_trigger

    started = time.time()
    low = text.lower().strip()

    # 0) time / date — local clock, koi LLM nahi (hallucination-proof)
    if _is_time_question(low):
        answer = _time_answer(text)
        return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                           surface=surface, seconds=time.time() - started, status="time",
                           workers=["clock"], reasoning=[f"ACTION: local clock read ({low[:40]})",
                                                      "RESULT: time/date local system clock se (ist + utc)"])

    # 1) memory operations
    op = is_memory_request(text)
    if op:
        mgr = get_manager()
        if op == "remember":
            res = mgr.remember(strip_trigger(text))
            answer = ("🧠 " if res.get("ok") else "⚠️ ") + res["message"]
            return AgentResult(text, answer, spoken=for_speech(answer), session_id=session_id,
                               surface=surface, seconds=time.time() - started, status="memory",
                               workers=["memory"], memory_updates=[res.get("action", "")])
        if op == "forget":
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


def _reasoning_from_state(state: dict[str, Any]) -> list[str]:
    """Structured reasoning summary — PLAN/DECISION/ACTION/RESULT/VERIFICATION/NEXT."""
    lines: list[str] = []
    plan = state.get("plan") or []
    if plan:
        lines.append("PLAN: " + " → ".join(str(p)[:80] for p in plan[:4]))
    workers = state.get("workers") or []
    if workers:
        lines.append("DECISION: workers = " + ", ".join(workers))
    findings = state.get("findings") or []
    if findings:
        acts = [f"{f.get('worker', '?')} ({f.get('seconds', '?')}s)" for f in findings[:4]]
        lines.append("ACTION: " + " | ".join(acts))
    evidence_len = len(state.get("evidence") or "")
    if evidence_len:
        lines.append(f"RESULT: {evidence_len} chars evidence collected")
    critique = state.get("critique") or {}
    if critique:
        verdict = critique.get("verdict") or critique.get("score") or "checked"
        lines.append(f"VERIFICATION: {str(verdict)[:120]}")
    if state.get("iterations"):
        lines.append(f"ITERATIONS: {state['iterations']} refine loop(s)")
    if state.get("errors"):
        lines.append("ERRORS: " + "; ".join(str(e)[:80] for e in (state.get("errors") or [])[:3]))
    return lines


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
) -> AgentResult:
    """Ek request chalao — fast paths → LangGraph → structured result.

    `on_progress`: optional callable(ProgressEvent) — CLI/Telegram isse live
    "thinking / planning / tool chal raha hai" feed render karte hain.
    """
    started = time.time()
    from .progress import CallbackSink, emit, use_sink
    return await _run_agent_inner(text, session_id, surface, use_fast_path, voice_output,
                                  started, on_progress, CallbackSink, emit, use_sink)


async def _run_agent_inner(text, session_id, surface, use_fast_path, voice_output,
                           started, on_progress, CallbackSink, emit, use_sink):
    # agar caller ne apna callback diya to wahi sink use karo, warna bahar wala (contextvar) sink
    sink = CallbackSink(on_progress) if on_progress else None
    if sink is None:
        emit("start", f"Request mila: {text[:80]}", detail=text)
        return await _run_agent_body(text, session_id, surface, use_fast_path, voice_output,
                                     started, emit)
    with use_sink(sink):
        emit("start", f"Request mila: {text[:80]}", detail=text)
        return await _run_agent_body(text, session_id, surface, use_fast_path, voice_output,
                                     started, emit)


async def _run_agent_body(text, session_id, surface, use_fast_path, voice_output, started, emit):
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
        fast = await _fast_paths(text, session_id, surface)
        if fast is not None:
            fast.seconds = time.time() - started
            emit("done", f"Fast-path jawab ({fast.seconds:.1f}s)", status="ok",
                 ms=int(fast.seconds * 1000))
            _post_run(fast, session_id)
            return fast

    # permissions: guest ko sirf read-level
    try:
        from ..security.permissions import SURFACES

        result.blocked_tools = [] if surface in SURFACES else []
    except Exception:
        pass

    try:
        from ..graph.build import jarvis_ainvoke

        emit("thinking", "LangGraph workflow start (recall → route → plan → act → verify)…")
        state = await jarvis_ainvoke(text, session_id=session_id)
        result.answer = state.get("answer") or state.get("response") or ""
        result.plan = state.get("plan") or []
        result.workers = state.get("workers") or []
        result.findings = state.get("findings") or []
        result.artifacts = state.get("artifacts") or []
        result.errors = state.get("errors") or []
        result.iterations = state.get("iterations") or 0
        result.critique = state.get("critique") or {}
        result.events = state.get("events") or []
        result.reasoning = state.get("reasoning_summary") or _reasoning_from_state(state)
        result.status = state.get("status") or "done"
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
    result.seconds = time.time() - started
    if result.errors:
        emit("warn", f"{len(result.errors)} error record hua", detail=str(result.errors[0])[:200],
             status="fail")
    emit("done", f"Kaam khatam ({result.seconds:.1f}s)", status=result.status,
         ms=int(result.seconds * 1000))
    _post_run(result, session_id)
    return result


def _post_run(result: AgentResult, session_id: str) -> None:
    """Har run ke baad: memory update, task close, events trace."""
    try:
        state = get_state()
        state.finish_task(result.answer[:200], status=result.status)
        if result.errors:
            state.remember_error(str(result.errors[0])[:200])
    except Exception:
        pass
    try:
        from ..memory.store import get_memory

        mem = get_memory()
        mem.add_turn(result.request, result.answer[:1500], session=session_id)
        task_id = None
        try:
            task_id = get_memory().start_task(result.request, route=",".join(result.workers))
        except Exception:
            pass
        if task_id:
            get_memory().finish_task(task_id, result.status, result=result.answer[:1200],
                                     plan=result.plan, artifacts=result.artifacts)
    except Exception:
        pass
    try:
        from ..llm import model_attempts

        result.events = list(result.events) + [{"node": "model_attempts", "events": model_attempts(5)}]
    except Exception:
        pass


async def stream_agent(text: str, session_id: str = "cli", surface: str = "cli_owner") -> AsyncIterator[dict[str, Any]]:
    """Live events stream (CLI spinner + Telegram progress ke liye)."""
    from ..graph.build import jarvis_stream

    async for chunk in jarvis_stream(text, session_id=session_id):
        yield chunk
