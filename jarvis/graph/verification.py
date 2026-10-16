"""Evidence-backed completion and honesty guard."""
from __future__ import annotations

import re
from typing import Any

from ..config import OWNER_NAME
from ..runtime.evidence import (
    claims_without_support,
    evidence_records,
    has_unnegated_claim,
    supporting_records,
    verify_execution,
)
from .state import JarvisState
from .workers.desktop_actions import _needs_action

_REQ_STOPWORDS = {"the", "a", "an", "and", "then", "to", "for", "me", "my", "it", "this", "that",
                  "please", "jarvis", "could", "can", "would", "is", "in", "on", "with", "from", "as",
                  "aur", "phir", "karke", "kar", "do", "mujhe", "mere"}


def _requirement_tokens(text: str) -> set[str]:
    return {word for word in re.findall(r"[\w\u0900-\u097F]+", str(text or "").lower())
            if len(word) > 2 and word not in _REQ_STOPWORDS}


_RESPONSE_ONLY_PLAN_RX = re.compile(
    r"\b(?:summari[sz]e|summari[sz]ation|summary|short\s+answer|final\s+answer|report\s+back|"
    r"answer\s+(?:the\s+)?(?:owner|user)|owner\s+ko.{0,50}(?:summary|answer)|"
    r"results?\s+ko.{0,50}(?:summary|answer))\b",
    re.I,
)
_CONCRETE_TOOL_ACTION_RX = re.compile(
    r"\b(?:open|launch|close|save|write|create|make|build|delete|remove|rename|move|download|upload|"
    r"search|find|read|type|send|message|schedule|install|run|execute|click|press|screenshot|"
    r"likh\w*|bana\w*|khol\w*|chala\w*|bhej\w*|dhundh\w*|hata\w*)\b",
    re.I,
)


def _is_response_only_plan_step(text: str) -> bool:
    """Omit the planner's narration/summarization step from tool-backed requirements.

    A generated item such as “verify results and give the owner a short summary” is fulfilled
    by the final synthesis, not by another desktop/browser tool call. Keep it out of the ledger
    unless it also asks for a concrete external action (for example, saving that summary to a file).
    """
    return bool(_RESPONSE_ONLY_PLAN_RX.search(text or "")
                and not _CONCRETE_TOOL_ACTION_RX.search(text or ""))


def requirement_worker(text: str, workers: list[str] | None = None) -> str:
    """Best-effort worker tag for a requirement; evidence still decides completion."""
    low = str(text or "").lower()
    if any(word in low for word in ("notepad", "chrome", "desktop", "screen", "click", "type", "keyboard",
                                    "mouse", "launch", "open app", "window", "folder", "file", "save as")):
        return "desktop" if "desktop" in (workers or []) or "open" in low or "type" in low else ""
    if any(word in low for word in ("browser", "website", "web page", "site", "url", "online")):
        return "browser" if "browser" in (workers or []) else "research" if "research" in (workers or []) else ""
    if any(word in low for word in ("search", "research", "latest", "news", "weather", "price", "compare")):
        return "research" if "research" in (workers or []) else "browser" if "browser" in (workers or []) else ""
    if any(word in low for word in ("code", "script", "program", "build", "compile", "run test")):
        return "code" if "code" in (workers or []) else ""
    if any(word in low for word in ("remember", "memory", "forget", "profile")):
        return "memory" if "memory" in (workers or []) else ""
    picked = list(workers or [])
    return picked[0] if len(picked) == 1 else ""


def decompose_requirements(request: str, plan: list[Any] | None = None,
                           workers: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Create stable R1..Rn work items from the planner or independent request clauses."""
    items = [re.sub(r"^\s*(?:step\s*)?\d+[.)-]\s*", "", str(value)).strip()
             for value in (plan or []) if str(value).strip()]
    if items:
        items = [item for item in items if not _is_response_only_plan_step(item)]
    if not items:
        clauses = re.split(r"\s*(?:;|\bthen\b|\band then\b|\bphir\b|\baur\b)\s*", request or "", flags=re.I)
        items = [part.strip(" ,.") for part in clauses if part.strip(" ,.")]
    if not items:
        items = [str(request or "").strip() or "Respond to the user request"]
    return {
        f"R{index}": {"id": f"R{index}", "requirement": text[:300], "status": "pending",
                       "evidence": [], "reason": "", "question": "",
                       "worker": requirement_worker(text, workers)}
        for index, text in enumerate(items[:12], 1)
    }


def _records_for_requirement(state: JarvisState, requirement: dict[str, Any], requirements: dict[str, Any]) -> list[dict[str, Any]]:
    records = [dict(record) for record in evidence_records(state) if isinstance(record, dict)]
    for finding in state.get("findings", []) or []:
        if not isinstance(finding, dict):
            continue
        worker = str(finding.get("worker") or "")
        for record in finding.get("tool_calls") or []:
            if isinstance(record, dict):
                records.append({**record, "worker": record.get("worker") or worker})
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        key = (str(record.get("name", "")), str(record.get("args", "")), str(record.get("output", "")))
        unique[key] = record
    records = list(unique.values())
    if len(requirements) <= 1:
        return records
    terms = _requirement_tokens(requirement.get("requirement", ""))
    worker = str(requirement.get("worker") or "")
    selected: list[dict[str, Any]] = []
    for record in records:
        record_worker = str(record.get("worker") or "")
        if worker and record_worker and worker != record_worker:
            continue
        searchable = " ".join((str(record.get("name", "")), str(record.get("args", "")),
                               str(record.get("output", "")), str(record.get("summary", ""))))
        overlap = terms & _requirement_tokens(searchable)
        if overlap or not terms:
            selected.append(record)
    return selected


def evaluate_requirements(state: JarvisState, *, answer: str = "") -> dict[str, dict[str, Any]]:
    """Assign done(evidence), failed(reason), needs_user(question), or pending before synthesis."""
    requirements = dict(state.get("requirements") or {})
    if not requirements:
        requirements = decompose_requirements(state.get("request", ""), state.get("plan", []),
                                              state.get("workers", []))
    blocked = bool(state.get("blocked_tools")) or state.get("status") in {"blocked", "needs_clarification", "cancelled"}
    question = str(state.get("clarification_question") or "" ).strip()
    tool_required = bool(verify_execution(state, "").tool_required)
    for ident, raw in requirements.items():
        requirement = dict(raw)
        matches = _records_for_requirement(state, requirement, requirements)
        good = [record for record in matches if record.get("ok") is True]
        bad = [record for record in matches if record.get("ok") is False]
        if blocked:
            requirement.update(status="needs_user", evidence=[], reason="", question=question or
                               "A manual confirmation or missing input is needed before I can continue.")
        elif good and not bad:
            evidence = [f"{record.get('name', 'tool')}: {str(record.get('output', ''))[:240]}" for record in good[:4]]
            requirement.update(status="done", evidence=evidence, reason="", question="")
        elif bad:
            reason = "; ".join(str(record.get("output") or record.get("error") or "tool failed")[:180]
                                for record in bad[:3])
            requirement.update(status="failed", evidence=[], reason=reason, question="")
        elif answer and not tool_required:
            requirement.update(status="done", evidence=["Direct response generated; no external action requested."],
                               reason="", question="")
        elif tool_required:
            requirement.update(status="failed", evidence=[],
                               reason="No matching tool evidence confirms this requirement.", question="")
        else:
            requirement.update(status="pending", evidence=[], reason="", question="")
        requirements[ident] = requirement
    return requirements


async def verification_node(state: JarvisState) -> dict[str, Any]:
    """Look at tool records per R-item before synthesis; unresolved work cannot become done."""
    requirements = evaluate_requirements(state)
    unresolved = [item for item in requirements.values() if item.get("status") != "done"]
    done = len(requirements) - len(unresolved)
    failed = sum(item.get("status") == "failed" for item in unresolved)
    needs_user = sum(item.get("status") == "needs_user" for item in unresolved)
    from .workers.common import _event

    return {
        "requirements": requirements,
        "requirements_unresolved": bool(unresolved),
        "events": [_event(state, "verification", f"{done}/{len(requirements)} requirements supported; "
                           f"failed={failed}, needs_user={needs_user}")],
    }


def finalize_requirements(state: JarvisState, answer: str) -> tuple[dict[str, dict[str, Any]], str, str]:
    """Final pass after synthesis; returns (items, terminal status, user-facing note)."""
    requirements = evaluate_requirements(state, answer=answer)
    unresolved = [item for item in requirements.values() if item.get("status") != "done"]
    if not unresolved:
        return requirements, "done", ""
    needs_user = [item for item in unresolved if item.get("status") == "needs_user"]
    failed = [item for item in unresolved if item.get("status") == "failed"]
    if needs_user:
        note = "\n\n**Needs your input:**\n" + "\n".join(
            f"• {item['id']} {item['requirement']}: {item.get('question') or 'Please clarify.'}"
            for item in needs_user[:4])
        return requirements, "needs_user", note
    note = "\n\n**Unresolved — not marked done:**\n" + "\n".join(
        f"• {item['id']} {item['requirement']}: {item.get('reason') or 'evidence missing'}"
        for item in failed[:6])
    done_count = sum(item.get("status") == "done" for item in requirements.values())
    return requirements, "partial_failure" if done_count else "failed", note


_SUCCESS_CLAIMS = ("kar diya", "kar di", "ho gaya", "ho gya", "done", "likh diya", "likh di",
                   "khol diya", "khol di", "band kar diya", "save kar diya", "bhej diya",
                   "chala diya", "complete", "successfully", "ban gaya", "ban diya")

_RAKH_DOWN = ("khaali", "khali")

def _claims_success(text: str) -> bool:
    return has_unnegated_claim(text, _SUCCESS_CLAIMS)

def honesty_guard(state: JarvisState, answer: str) -> tuple[str, str]:
    """Answer ko sach ke saath align karo. Returns (final_answer, guard_reason).

    Rule (hard invariant): LLM ka text evidence NAHI hai. Success sirf tab maana jata hai jab
    asli tool call hui ho aur uska result mila ho (state["tool_evidence"] / findings[].tool_calls).
    """
    verdict = verify_execution(state, answer)
    required = verdict.tool_required
    ev_done = verdict.ok_records
    ev_failed = verdict.failed_records

    if not required:
        # Action nahi maanga gaya — par agar answer *execution success* claim karta hai aur
        # koi tool evidence nahi hai, to wo claim bhi evidence nahi ban sakta (req: LLM ke
        # "done/opened/executed/maine kar diya" text ko evidence mat maano).
        # Generic success-phrases ("ho gaya / done / complete") bahut broad hain — inhe sirf
        # tab pakdo jab request action-shaped ho (plain chit-chat par false positive na ho).
        actionish = _needs_action(state.get("request", "")) or bool(state.get("action_required"))
        if claims_without_support(state, answer) or (actionish and _claims_success(answer)):
            state["status"] = "failed"
            return (
                f"⚠️ **Ye claim main verify nahi kar sakta, {OWNER_NAME}.**\n\n"
                f"Jawab me '{verdict.claim_words[0]}' jaisa kuch tha, lekin is run me koi tool call "
                f"nahi chali — matlab machine par kuch execute nahi hua.\n\n"
                f"Jo main actually kar sakta hoon wo batao (jaise `notepad kholo`, `notes me x naam se "
                f"save kardo: ...`), phir seedha tool chala kar proof ke saath bataunga.",
                "execution claim without tool evidence",
            )
        return answer, ""

    # "done" sirf un records se jo kaam ko support karte hain (khali search se nahi).
    # `actions_done` legacy channel bhi isi filter se guzarta hai — warna found=False record
    # wahan se wapas "success" ban jata tha (guard bypass).
    done = supporting_records(ev_done) or supporting_records(state.get("actions_done") or [])
    failed = ev_failed or (state.get("action_failures") or [])

    def _fmt(items: list[dict[str, Any]]) -> str:
        out = []
        for c in items[-6:]:
            mark = "✅" if c.get("ok") else "❌"
            out.append(f"{mark} {c.get('name')} → {str(c.get('output'))[:180]}")
        return "\n".join(out)

    if not done and not failed:
        if ev_done:
            # tool(s) chale par kuch mila/nikal nahi (jaise file search: 0 matches).
            # Yahan model ka "ho gaya / kaam complete" claim *hata* diya jata hai aur asli
            # natija likha jata hai. Read-only search ke liye run "failed" nahi — search to
            # chal hi gayi thi; bas result khali tha.
            search_only = all(str(r.get("name") or "") in {"find_files", "read_file"} for r in ev_done)
            state["status"] = "done" if search_only else "failed"
            reason = ("search ran but nothing was found"
                      if search_only else "execution ran but nothing was found / no effect")
            return (
                f"⚠️ **Ye claim verify nahi hua, {OWNER_NAME}.**\n\n"
                f"Tool(s) chale, par kuch mila nahi / koi asar nahi hua:\n{_fmt(ev_done)}\n\n"
                "Jo actually hua wahi bata raha hoon — 'ho gaya' bolna galat hota. "
                "Naam/path thoda exact do (jaise `notepad kholo` ya `notes me save kardo: …`) "
                "to dobara try karta hoon.",
                "" if search_only else reason,
            )
        state["status"] = "failed"
        return (
            f"⚠️ **Kaam nahi hua, {OWNER_NAME}.**\n\n"
            "Is request ke liye koi tool call nahi chala — matlab machine par kuch bhi execute nahi hua.\n"
            "Aise hi 'ho gaya' bolna galat hota hai, isliye main saaf bata raha hoon.\n\n"
            "**Kya try kar sakte ho:** request thoda specific karo jaise "
            "`notepad kholo` ya `notepad open karke 'hello' likho` — phir main seedha tool chalaunga.",
            "action required but zero tool calls",
        )

    if not done and failed:
        state["status"] = "failed"
        return (
            f"❌ **Kaam fail hua, {OWNER_NAME}.**\n\nYe tool call(s) chale par error aaya:\n{_fmt(failed)}\n\n"
            "Wajah usually: app registry me nahi mila, ya typing/hotkey ke liye OS-level input block hai. "
            "Windows par typing ab bina kisi install ke chalti hai; Linux par `xdotool` chahiye.",
            "all actions failed",
        )

    if failed:
        state["status"] = "partial_failure"
        # Warning HAMESHA sabse upar — warna pehli line ("Ho gaya boss ✅") padh kar
        # user ko lagta hai kaam ho gaya, jabki ek step fail hua tha.
        head = (f"⚠️ **Aadha kaam hua, {OWNER_NAME}.**\n\n"
                f"✅ Jo hua:\n{_fmt(done) if done else '—'}\n\n"
                f"❌ Jo fail hua:\n{_fmt(failed)}\n\n---\n")
        body_lines = (answer or "").split("\n")
        # pehli line me jhooti success claim ho to hata do
        if body_lines and _claims_success(body_lines[0]):
            body_lines[0] = ""
        return head + "\n".join(body_lines).strip(), "partial failure reported"

    # sab ok → agar model ne claim nahi kiya to verified facts add kar do
    if not _claims_success(answer):
        answer = answer + f"\n\n**Verified:**\n{_fmt(done)}"
    return answer, ""
