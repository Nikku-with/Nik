"""Answer synthesis, durable turn persistence, and surface response formatting."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from ..config import OWNER_NAME, PROVIDER, is_offline
from ..dspy_engine import modules as dspy_mods
from ..llm import get_chat_model
from ..memory.session_state import get_state
from ..memory.store import get_memory
from ..prompts import supervisor_prompt
from ..runtime.evidence import evidence_records, verify_execution
from ..runtime.progress import emit
from .state import JarvisState, evidence_digest
from .verification import finalize_requirements, honesty_guard
from .workers.common import _event


async def synthesize_node(state: JarvisState) -> dict[str, Any]:
    emit("thinking", "Findings ko final jawab me likh raha hoon…")
    request = state.get("request", "")
    evidence = evidence_digest(state)
    critique = state.get("critique") or {}
    critique_text = json.dumps(critique, ensure_ascii=False)[:1500] if critique else ""
    refine_hint = str(state.get("refine_hint") or "")
    if refine_hint:
        critique_text = f"{critique_text}\nRefinement focus: {refine_hint}"[:1500]
    iterations = state.get("iterations", 0)

    answer = ""
    new_errors: list[str] = []
    if not (is_offline() or PROVIDER == "demo"):
        answer = await asyncio.to_thread(dspy_mods.final_answer, request, evidence, critique_text)

        if not answer:
            try:
                from langchain_core.messages import SystemMessage

                model = get_chat_model("brain")
                resp = await model.ainvoke(
                    [
                        SystemMessage(content=supervisor_prompt()),
                        HumanMessage(
                            content=(
                                f"Owner ki request: {request}\n\n"
                                f"Workers ka evidence:\n{evidence}\n\n"
                                f"Critic feedback: {critique_text or 'none'}\n\n"
                                f"Ab final Hinglish answer likho (short, factual, {OWNER_NAME} ko address karke)."
                            )
                        ),
                    ]
                )
                answer = str(resp.content).strip()
            except Exception as exc:
                answer = ""
                new_errors.append(f"synthesize: {type(exc).__name__}: {str(exc)[:200]}")

    if not answer:
        answer = _offline_synthesize(state, evidence, iterations)

    # ---- HONESTY GUARD: jhooth se pehle sach ----
    answer, guard_reason = honesty_guard(state, answer)
    extra_events = []
    if guard_reason:
        extra_events.append(_event(state, "honesty_guard", guard_reason))
        new_errors.append(f"honesty_guard: {guard_reason}")

    # ---- HARD INVARIANT (belt & braces): tool-required + zero evidence → kabhi success nahi ----
    # Ye guard ke baad bhi chalta hai, taaki koi naya code path silently plain-text success na bana de.
    verdict = verify_execution(state, answer)
    if verdict.tool_required and not verdict.has_evidence:
        state["status"] = "failed"
        if not guard_reason:
            guard_reason = "execution_required but zero tool evidence (hard invariant)"
            extra_events.append(_event(state, "invariant", guard_reason))
            new_errors.append(f"invariant: {guard_reason}")
            answer = (
                f"⚠️ **Kaam nahi hua, {OWNER_NAME}.** Is request ke liye asli tool execution zaroori "
                f"thi, lekin koi tool call nahi chali — isliye main success claim nahi kar sakta.\n\n"
                f"Reason: {guard_reason}"
            )

    requirements, requirement_status, requirement_note = finalize_requirements(state, answer)
    if requirement_note:
        answer += requirement_note
        extra_events.append(_event(state, "requirements", f"unresolved requirement(s); terminal={requirement_status}"))
    if requirement_status in {"failed", "partial_failure", "needs_user"}:
        state["status"] = requirement_status
        # A locally verified tool call does not make every requirement complete. If the
        # requirement ledger still has an unresolved item, remove any unconditional green
        # headline so the natural-language verdict matches the terminal status.
        if re.match(r"^\s*Verified\s*✅", answer, flags=re.I):
            status_lead = {
                "partial_failure": "⚠️ Partial completion — at least one requirement remains unverified.",
                "failed": "❌ Required work remains incomplete; no overall completion is claimed.",
                "needs_user": "⏳ I need your input before I can claim completion.",
            }[requirement_status]
            answer = re.sub(r"^\s*Verified\s*✅[^\n]*(?:\n|$)", status_lead + "\n", answer,
                            count=1, flags=re.I)

    out: dict[str, Any] = {
        "answer": answer,
        "evidence": evidence,
        "execution_required": bool(verdict.tool_required),
        "requirements": requirements,
        "requirements_unresolved": any(item.get("status") != "done" for item in requirements.values()),
        "events": ([_event(state, "synthesize", f"{len(answer)} chars answer (iter {iterations + 1})")]
                   + extra_events),
    }
    if new_errors:
        out["errors"] = new_errors
    if state.get("status") in ("failed", "partial_failure", "needs_user"):
        out["status"] = state["status"]          # honest status upstream tak (AgentResult.status)
    return out

def _offline_synthesize(state: JarvisState, evidence: str, iterations: int) -> str:
    """Deterministic Hinglish summary — demo/offline mode."""
    request = state.get("request", "")
    findings = state.get("findings", []) or []

    # Direct-answer route (identity/baarish/small talk) → demo brain ka conversational reply
    if not findings and set(state.get("workers") or []) <= {"answer"}:
        try:
            from ..llm import get_chat_model

            model = get_chat_model("brain")
            chat_fn = getattr(model, "_chat", None)
            if callable(chat_fn):
                reply = str(chat_fn(request)).strip()
                if reply:
                    return reply
        except Exception:
            pass
    notes = state.get("notes_saved", []) or []
    artifacts = state.get("artifacts", []) or []

    # Offline synthesis: chhota, kaam-ka, Hinglish — plan/evidence ko screenshot ki tarah nahi dikhata
    lines: list[str] = []
    real_evidence = [
        ln for ln in evidence.splitlines()
        if ln.strip()
        and "koi tool evidence nahi" not in ln.lower()
        and not ln.strip().lower().startswith(("local clock", "utc:", "epoch:"))
    ]
    head = real_evidence[:6] if real_evidence else []

    failed_actions = state.get("action_failures") or []
    verified_actions = state.get("actions_done") or []
    verification_failed = any("VERIFICATION FAILED" in str(f.get("summary", "")) for f in findings)
    # "koi concrete tool action match nahi hua" = humne kuch kiya hi nahi.
    # Us case me "Ho gaya boss ✅" kehna jhooth lagta hai (khaas kar sawaal par).
    no_action_taken = bool(findings) and all(
        ("koi concrete tool action match nahi hua" in str(f.get("summary", ""))
         or "koi tool evidence nahi" in str(f.get("summary", ""))) for f in findings)
    if failed_actions:
        prefix = (f"⚠️ {OWNER_NAME}, kuch action hua par kuch tool call fail hui:"
                  if verified_actions else f"❌ {OWNER_NAME}, ye kaam poora nahi hua — tool ne error diya:")
        lines.append(prefix)
        lines.append("")
        lines += [f"• {c.get('name')} → {str(c.get('output'))[:160]}" for c in failed_actions[:4]]
        lines.append("")
    elif verification_failed:
        lines.append(f"⚠️ {OWNER_NAME}, maine is request par koi tool call nahi kiya — "
                     "matlab machine par kuch nahi hua. Sach bata raha hoon, guess nahi.")
        lines.append("")
    if no_action_taken:
        lines.append(f"Samajh gaya, {OWNER_NAME} — is request par koi tool chalane ki zaroorat nahi padi.")
        lines.append("")
        lines.append("Agar **kaam karwana** hai to seedha bolo, jaise: "
                     "`notepad kholo`, `notepad open karke hello likho`, `screenshot lo`, "
                     "`notes me x naam se save kardo: ...`. "
                     "Aur agar **samajhna** hai to poocho, jaise: `notepad kaise kholte hain`.")
        head = []
    elif head:
        if verified_actions and not failed_actions and not verification_failed:
            lines.append(f"Verified ✅ — {OWNER_NAME} ke liye requested action tool se confirm hua.")
        else:
            lines.append("Yeh raha jo result/evidence mila:")
        lines.append("")
        lines += head
    else:
        lines.append(f"Samajh gaya: _{request[:160]}_")
        lines.append("")
        lines.append("Offline demo mode me main ise rule-based handle kar raha hoon — koi tool "
                     "isi request ke liye match nahi hua.")
    if notes:
        lines.append(f"\n📝 Note save hua: {', '.join(str(n)[:40] for n in notes[:3])}")
    if artifacts:
        lines.append(f"📦 {len(artifacts)} artifact workspace me saved")

    if PROVIDER == "demo":
        lines.append("")
        lines.append(f"_OFFLINE / DEMO MODE_ — key daalte hi yahi request {OWNER_NAME} ke configured brain "
                     "se chalegi: research → browser → code → verify → answer. "
                     "`python -m jarvis doctor` se check karo.")
    return "\n".join(lines)

async def persist_node(state: JarvisState) -> dict[str, Any]:
    """Persist one conversation turn and task state; never retry a successful turn write."""
    request = state.get("request", "")
    answer = state.get("answer", "")
    mem = get_memory()
    persist_status = state.get("status") or "running"
    if persist_status in {"running", "done"}:
        persist_status = "done_with_errors" if state.get("errors") else "done"
    elif state.get("errors") and persist_status not in {"failed", "partial_failure", "error", "blocked", "needs_user"}:
        persist_status = "done_with_errors"

    errors: list[str] = []
    turn_persisted = False
    try:
        mem.add_turn(request, answer, session=state.get("session_id", "default"))
        turn_persisted = True
    except Exception as exc:
        errors.append(f"conversation persist: {type(exc).__name__}: {str(exc)[:180]}")

    try:
        st = get_state()
        st.finish_task(status=persist_status, result=answer[:300],
                       artifacts=state.get("artifacts", []))
        st.set_pending_steps([])
    except Exception as exc:
        errors.append(f"session-state persist: {type(exc).__name__}: {str(exc)[:180]}")

    if state.get("task_id"):
        try:
            mem.finish_task(
                state["task_id"], status=persist_status,
                result=answer[:4000], plan=state.get("plan", []),
                artifacts=state.get("artifacts", []), requirements=state.get("requirements", {}),
            )
        except Exception as exc:
            errors.append(f"task finalization: {type(exc).__name__}: {str(exc)[:180]}")

    out: dict[str, Any] = {
        "memory_persisted": turn_persisted,
        "events": [_event(state, "persist", "conversation turn persisted" if turn_persisted
                           else "conversation persistence failed")],
    }
    if errors:
        out["errors"] = errors
    return out

def build_reasoning_summary(state: JarvisState) -> list[str]:
    """Structured reasoning (PLAN/DECISION/ACTION/RESULT/VERIFICATION) — hidden CoT expose nahi karte."""
    lines: list[str] = []
    plan = state.get("plan") or []
    if plan:
        lines.append("PLAN: " + " → ".join(str(p)[:70] for p in plan[:4]))
    workers = state.get("workers") or []
    if workers:
        lines.append("DECISION: " + ", ".join(workers) + " worker(s) chune gaye")
    tools_used = list(dict.fromkeys(
        str(record.get("name")) for record in evidence_records(state) if record.get("name")
    ))
    if tools_used:
        lines.append("TOOLS: " + ", ".join(tools_used[:8]))
    findings = state.get("findings") or []
    if findings:
        lines.append("ACTION: " + " | ".join(
            f"{f.get('worker', '?')} {f.get('seconds', '?')}s" for f in findings[:5]))
    evidence = state.get("evidence") or ""
    if evidence:
        lines.append(f"RESULT: {len(evidence)} chars evidence, {len(state.get('artifacts') or [])} artifact(s)")
    critique = state.get("critique") or {}
    if critique:
        lines.append("VERIFICATION: " + str(critique.get("verdict") or critique.get("score") or "ok")[:100])
    if state.get("iterations"):
        lines.append(f"ITERATIONS: {state['iterations']} refine loop(s)")
    if state.get("errors"):
        lines.append("ERRORS: " + "; ".join(str(e)[:70] for e in (state.get("errors") or [])[:2]))
    return lines

async def respond_node(state: JarvisState) -> dict[str, Any]:
    answer = state.get("answer", "")
    spoken = ""
    if answer and state.get("surface") == "voice_owner" and not (is_offline() or PROVIDER == "demo"):
        spoken = await asyncio.to_thread(dspy_mods.voice_polish, answer)
    # Text CLI/Telegram/API skip the extra voice-polish model call; cleanup stays local.
    # voice ke liye hamesha emoji/markdown-free text (TTS emoji ka naam nahi bolega)
    try:
        from ..voice.speech import for_speech, summary_for_speech

        spoken = summary_for_speech(spoken or answer, max_sentences=4) if answer else ""
        spoken = for_speech(spoken) or _shorten(answer)
    except Exception:
        spoken = spoken or _shorten(answer)
    # Status ko *kabhi* blindly "done" mat karo — honesty guard/invariant ne failure set kiya ho
    # to wahi upstream (AgentResult.status, Telegram footer, CLI) tak jaana chahiye.
    status = state.get("status") or "running"
    if status in {"failed", "partial_failure", "error", "blocked", "cancelled", "needs_user"}:
        pass
    elif state.get("errors"):
        status = "partial_failure"
    else:
        status = "done"
    return {
        "spoken": spoken,
        "reasoning_summary": build_reasoning_summary(state),
        "status": status,
        "messages": [AIMessage(content=answer)],
        "events": [_event(state, "respond", f"spoken={len(spoken)} chars • status={status}")],
    }

def _shorten(text: str, limit: int = 420) -> str:
    text = re.sub(r"```.*?```", " (code)", text or "", flags=re.S)
    text = re.sub(r"[#*_>]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = cut.rfind(". ")
    return (cut[: dot + 1] if dot > 120 else cut).strip()
