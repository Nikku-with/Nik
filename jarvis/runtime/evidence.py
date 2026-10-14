"""Execution evidence + hard invariants — "success bina proof ke nahi".

Is module ka poora maqsad ek hai: **tool-required kaam me success ka sirf ek hi proof hai —
asli tool call + uska asli result.** LLM ka text ("ho gaya", "opened", "maine kar diya") kabhi
evidence nahi hai.

Hard invariant (graph ise enforce karta hai):

    TOOL REQUIRED → MODEL MUST BE TOOL-BOUND → ACTUAL TOOL CALL → ACTUAL TOOL RESULT → TAB SUCCESS

Kahan use hota hai:
    * `jarvis/llm/core.py::FallbackChatModel.bind_tools()` — tool-bound na ho paane par
      `ToolBindingError` (fail-closed; unbound model par fallback **kabhi nahi**),
    * `jarvis/graph/nodes.py::honesty_guard()` — final answer ko evidence ke saath align karta hai,
    * `jarvis/graph/nodes.py::desktop_worker()` / `_execute_desktop_action()` — deterministic
      path ke records bhi `state["tool_evidence"]` me jaate hain,
    * `jarvis/runtime/agent.py::AgentResult` — caller ko `verified_execution` flag milta hai.

Anti-regression: `tests/test_tool_invariant.py` is module ke rules ke saath-saath source-level
bhi check karta hai (banned pattern `bound.append(model)` dobara na aaye, aur tool-required
paths ke bina text-only success na bane).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ToolExecutionInvariantError",
    "TOOL_REQUIRED_WORKERS",
    "EXECUTION_CLAIMS",
    "NEGATION_WORDS",
    "evidence_records",
    "has_verified_evidence",
    "failed_records",
    "is_tool_required",
    "claims_execution",
    "require_tool_bound",
    "ExecutionVerdict",
    "verify_execution",
    "supporting_records",
    "finding_is_blind",
    "detect_blind_handoffs",
    "session_evidence_summary",
]


class ToolExecutionInvariantError(RuntimeError):
    """Tool-required execution bina asli tool evidence ke "success" banane ki koshish."""


# Workers jinme asli machine/action kaam hona hi hai (inme text-only answer = jhooth ho sakta hai)
TOOL_REQUIRED_WORKERS = frozenset({"desktop", "code", "browser", "ops", "automation", "system"})

# Execution claims (Hinglish + English). "done" jaisa generic shabd jaan-bujh kar list me nahi —
# warna normal chit-chat par false positive aata hai.
EXECUTION_CLAIMS = (
    "kar diya",
    "kar di",
    "kr diya",
    "kr di",
    "khol diya",
    "khol di",
    "khul gaya",
    "khul gya",
    "likh diya",
    "likh di",
    "type kar diya",
    "save kar diya",
    "save ho gaya",
    "band kar diya",
    "bhej diya",
    "chala diya",
    "launch kar diya",
    "install kar diya",
    "create kar diya",
    "opened",
    "launched",
    "executed",
    "ran successfully",
    "i have opened",
    "i've opened",
    "i opened",
    "i have run",
    "i've run",
    "i ran",
    "i have executed",
    "i've executed",
    "i saved",
    "i have saved",
    "i typed",
    "i wrote",
    "i deleted",
    "i sent",
    "i installed",
)
NEGATION_WORDS = (
    "nahi",
    "nahin",
    "not ",
    "n't",
    "fail",
    "fail hua",
    "couldn't",
    "could not",
    "unable",
    "nahi hua",
    "nahi ho paya",
)


def supporting_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wo ok-records jo *kaam* ko support karte hain (khali search ≠ kaam hua).

    Deterministic search `found=False` ke saath ok=True hota hai (tool chala, kuch nahi mila) —
    usse "padh liya / mil gaya" ka proof nahi maana ja sakta.
    """
    return [r for r in records if r.get("ok") and r.get("found") is not False]


@dataclass
class ExecutionVerdict:
    """Ek run ka honest verdict — kya evidence hai, kya claim ho raha hai."""

    tool_required: bool = False
    evidence: list[dict[str, Any]] = field(default_factory=list)
    ok_records: list[dict[str, Any]] = field(default_factory=list)
    failed_records: list[dict[str, Any]] = field(default_factory=list)
    claim_words: list[str] = field(default_factory=list)

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidence)

    @property
    def verified(self) -> bool:
        """Success ka proof: kam se kam ek asli tool record + koi failure nahi."""
        return bool(self.ok_records) and not self.failed_records

    @property
    def failure_reason(self) -> str:
        if not self.has_evidence:
            return "zero tool calls"
        if not self.ok_records:
            return "all tool calls failed"
        if self.failed_records:
            return "partial failure"
        return ""


# ---------------------------------------------------------------------------
# evidence extraction
# ---------------------------------------------------------------------------
# Outcome keys jo record ke saath carry karni hain (warna `found=False` jaisa signal kho jata
# hai aur khali search galti se "success evidence" ban jati hai).
_KEEP_KEYS = ("found", "path", "error", "seconds", "offline", "notes_saved", "sites")


def _clean_record(rec: Any) -> dict[str, Any] | None:
    if not isinstance(rec, dict) or not rec.get("name"):
        return None
    cleaned = {
        "name": str(rec.get("name")),
        "args": rec.get("args") or {},
        "output": str(rec.get("output") or "")[:800],
        "ok": bool(rec.get("ok", not _looks_failed(str(rec.get("output") or "")))),
        "source": rec.get("source") or "tool",
    }
    for key in _KEEP_KEYS:
        if key in rec:
            cleaned[key] = rec[key]
    return cleaned


def _looks_failed(output: str) -> bool:
    low = (output or "").strip().lower()
    if not low:
        return False
    bad = (
        "❌",
        "error:",
        "traceback",
        "not found",
        "nahi mila",
        "fail hua",
        "fail:",
        "unavailable",
        "permission denied",
        "module not found",
        "exception",
    )
    return any(b in low for b in bad) and "✅" not in low[:6]


def evidence_records(state: Any) -> list[dict[str, Any]]:
    """State se saare asli tool records nikalo (deterministic + subagent + worker-level).

    Sources (jo bhi mile, sab):
      * `state["tool_evidence"]`  — workers ka canonical record (desktop/code/browser)
      * `state["actions_done"]` / `state["action_failures"]` — desktop verification loop
      * `findings[].tool_calls`   — ReAct subagent ke ToolMessage/AIMessage se bane records
    """
    if not isinstance(state, dict):
        return []
    out: list[dict[str, Any]] = []
    for rec in state.get("tool_evidence") or []:
        cleaned = _clean_record(rec)
        if cleaned:
            out.append(cleaned)
    for key in ("actions_done", "action_failures"):
        for rec in state.get(key) or []:
            cleaned = _clean_record(rec)
            if cleaned:
                out.append(cleaned)
    for finding in state.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        for rec in finding.get("tool_calls") or []:
            cleaned = _clean_record(rec)
            if cleaned:
                out.append(cleaned)
    return out


def has_verified_evidence(state: Any) -> bool:
    return bool([r for r in evidence_records(state) if r["ok"]])


def failed_records(state: Any) -> list[dict[str, Any]]:
    return [r for r in evidence_records(state) if not r["ok"]]


# ---------------------------------------------------------------------------
# tool-required detection + claims
# ---------------------------------------------------------------------------
def is_tool_required(state: Any, request: str = "", workers: list[str] | None = None) -> bool:
    """Kya is run me asli tool execution maangi gayi thi?

    True agar: state me `action_required`/`execution_required` set hai, ya chune gaye workers
    me koi tool-required worker hai (desktop/code/browser/ops/automation).
    """
    if isinstance(state, dict) and (state.get("action_required") or state.get("execution_required")):
        return True
    picked = workers if workers is not None else (state.get("workers") if isinstance(state, dict) else None)
    return bool(set(picked or []) & TOOL_REQUIRED_WORKERS)


def claims_execution(text: str) -> list[str]:
    """Text me execution-success claims dhoondho (negation wale nahi ginte)."""
    low = (text or "").lower()
    if not low:
        return []
    if any(neg in low for neg in NEGATION_WORDS):
        return []
    return [c for c in EXECUTION_CLAIMS if c in low]


def verify_execution(state: Any, answer: str, *, workers: list[str] | None = None) -> ExecutionVerdict:
    """Ek jagah se honest verdict: evidence hai? claim kya hai?"""
    records = evidence_records(state)
    return ExecutionVerdict(
        tool_required=is_tool_required(state, workers=workers),
        evidence=records,
        ok_records=[r for r in records if r["ok"]],
        failed_records=[r for r in records if not r["ok"]],
        claim_words=claims_execution(answer),
    )


def claims_without_support(state: Any, answer: str) -> bool:
    """Answer execution success claim karta hai par evidence usko support nahi karta?

    (Do case: koi evidence hi nahi, ya sirf khali-search records — dono me 'ho gaya' jhooth hai.)
    """
    v = verify_execution(state, answer)
    if not v.claim_words:
        return False
    return not supporting_records(v.ok_records) and not v.failed_records


# ---------------------------------------------------------------------------
# blind-handoff detection (worker ne tool chalaye bina summary pakda di)
# ---------------------------------------------------------------------------
LAZY_SUMMARIES = frozenset(
    {
        "",
        "(khali — worker ne kuch report nahi kiya)",
        "(worker ne khaali result diya)",
    }
)


def finding_is_blind(finding: Any) -> tuple[bool, str]:
    """Tool-required worker ki finding me tool proof hai ya hawa-hawai claim?

    Blind = summary to hai par usko support karta koi ok tool record nahi.
    Non-tool workers (research/memory/answer) yahan blind nahi gine jaate —
    unka output text hota hai, machine action nahi.
    """
    if not isinstance(finding, dict):
        return False, ""
    worker = str(finding.get("worker") or "").lower()
    if worker not in TOOL_REQUIRED_WORKERS:
        return False, ""
    summary = str(finding.get("summary") or "").strip()
    if finding.get("lazy") or summary in LAZY_SUMMARIES:
        return True, "worker ne khaali/lazy result diya (koi actual output nahi)"
    calls = finding.get("tool_calls") or []
    if not calls:
        # state-level canonical records (tool_evidence) bhi proof chalenge —
        # wo check caller (detect_blind_handoffs) karta hai; yahan sirf local signal.
        return True, "summary hai par tool_calls khaali (bina tool chalaye claim)"
    if not any(isinstance(c, dict) and c.get("ok", True) for c in calls):
        return True, "saare tool calls fail — par summary success jaisa"
    return False, ""


def detect_blind_handoffs(state: Any) -> list[dict[str, Any]]:
    """State ki findings me blind handoffs dhoondho (synthesize/critic/refine ke liye).

    State-level `tool_evidence` (canonical worker records) proof ke taur par accept
    hota hai — sirf tab blind jab wahan bhi worker ka koi ok record na ho.
    """
    if not isinstance(state, dict):
        return []
    state_ok_workers = {str(r.get("worker") or "").lower() for r in evidence_records(state) if r.get("ok")}
    # canonical records me worker tag na ho to naam se guess mat karo — blind hi rahega
    out: list[dict[str, Any]] = []
    for finding in state.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        blind, reason = finding_is_blind(finding)
        if blind and "tool_calls khaali" in reason:
            worker = str(finding.get("worker") or "").lower()
            names = {str(r.get("name") or "") for r in evidence_records(state) if r.get("ok")}
            if worker in state_ok_workers or names:
                # kahin to asli tool chala — worker-attribution pakki nahi, isliye
                # blind hi maano (fail-closed), par reason me bata do.
                reason += " (state me kuch tool records hain, par is finding se jude nahi)"
        if blind:
            out.append(
                {"worker": finding.get("worker"), "summary": str(finding.get("summary") or "")[:160], "reason": reason}
            )
    return out


def session_evidence_summary(state: Any) -> dict[str, Any]:
    """Ek run ka evidence balance-sheet (reasoning summary / memory / UI ke liye)."""
    records = evidence_records(state) if isinstance(state, dict) else []
    ok = [r for r in records if r.get("ok")]
    blind = detect_blind_handoffs(state)
    verdict = verify_execution(state, "") if isinstance(state, dict) else None
    if verdict is not None and verdict.tool_required:
        status = verdict.failure_reason or "verified"
    else:
        status = "verified" if ok else ("failed" if records else "no-evidence-needed")
    return {
        "tool_calls": len(records),
        "verified": len(supporting_records(ok)),
        "failed": len(records) - len(ok),
        "blind_findings": [str(b.get("worker")) for b in blind],
        "status": status,
    }


# ---------------------------------------------------------------------------
# model-side invariant (tool-required run me unbound model kabhi nahi)
# ---------------------------------------------------------------------------
def require_tool_bound(model: Any, *, role: str = "brain") -> Any:
    """Tool-required execution ke liye model *actually* tool-bound hona chahiye.

    `FallbackChatModel.bind_tools()` fail-closed hai (bind fail hone wale models chain se hat
    jaate hain, sab fail → `ToolBindingError`). Ye helper uska belt-and-braces check hai:
    agar aage koi code path bina bind kiye model pass kare, wo yahin loud fail hoga —
    silently plain-text generation me nahi girega.
    """
    from ..llm.core import ToolBindingError

    bound_flag = getattr(model, "tools_bound", None)
    chain = getattr(model, "chain", None)
    if bound_flag is False:
        raise ToolBindingError(
            f"No configured model successfully supports tool calling for this execution. "
            f"(role={role}: model chain is not tool-bound)"
        )
    if bound_flag is None and chain is not None:
        raise ToolBindingError(
            f"No configured model successfully supports tool calling for this execution. "
            f"(role={role}: chain type {type(model).__name__} par tools_bound flag nahi hai)"
        )
    return model
