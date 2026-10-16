"""Code generation, execution, and verified artifact worker."""
from __future__ import annotations

import re
import time
from typing import Any

from ..state import JarvisState
from .common import _event, _finding, _permission_block

# ---------------------------------------------------------------------------
# "python install krde" — package-install intent
# ---------------------------------------------------------------------------
# Kyun: pehle har install request `code_engine` me chali jaati thi. Code engine ek script
# generate karta hai, phir Gate script likhne par poochhta hai aur phir use chalane par —
# yani do approvals aur ~2 minute, jabki owner ne sirf `pip install <pkg>` maanga tha.
# Ab seedha `install_package` chal ta hai: ek Gate approval, ek baar.
_INSTALL_RX = re.compile(
    r"\b(?:pip\s+install|install\s+kar(?:\s*do|\s*de|\s*ke)?|install\s+karo|install\s+krde|"
    r"install\s+kar\s+do|install\s+kar\s+de|install|lagao|lagana|daal\s*do)\b", re.I,
)
# words that name a runtime/app rather than a pip-installable distribution
_RUNTIME_WORDS = {
    "python", "python3", "python2", "node", "nodejs", "npm", "java", "jdk", "go", "golang",
    "rust", "cargo", "git", "chrome", "google-chrome", "vscode", "vs code", "docker",
    "ffmpeg", "mysql", "postgres", "postgresql", "mongodb", "redis", "ruby", "php", "dotnet",
}
_PKG_TOKEN_RX = re.compile(r"\b([A-Za-z][A-Za-z0-9._-]{1,40})\b")
_VERB_TAIL_RX = re.compile(
    r"\b([A-Za-z][A-Za-z0-9._-]{1,40})\s+(?:install|lagao|lagana|daal\s*do)\b", re.I)
_VERB_HEAD_RX = re.compile(
    r"\b(?:pip\s+)?install\s+(?:kar\s*(?:do|de)?|karo|krde)?\s*([A-Za-z][A-Za-z0-9._-]{1,40})\b", re.I)
_NOISE_WORDS = {
    "install", "pip", "kar", "karo", "krde", "do", "de", "ke", "please", "plz", "ek", "the",
    "package", "library", "module", "jarvis", "bhai", "boss", "mujhe", "mere", "liye", "aur",
    "bhi", "jarurat", "chahiye", "karna", "karke", "krdo", "kardo", "lagao", "lagana", "daal",
}


def install_intent(request: str) -> dict[str, Any]:
    """Decide whether `request` is a package install and whether the package is unambiguous.

    Returns ``{"detected", "package", "ambiguous", "reason"}``. `ambiguous` drives exactly one
    clarifying question; the caller must never ask twice.
    """
    text = " ".join((request or "").split())
    low = text.lower()
    if not _INSTALL_RX.search(low):
        return {"detected": False, "package": None, "ambiguous": False, "reason": ""}

    candidate = ""
    match = _VERB_TAIL_RX.search(text)          # "requests install kar do"
    if match and match.group(1).lower() not in _NOISE_WORDS:
        candidate = match.group(1)
    if not candidate:
        match = _VERB_HEAD_RX.search(text)      # "pip install requests" / "install karo numpy"
        if match and match.group(1).lower() not in _NOISE_WORDS:
            candidate = match.group(1)
    if not candidate:
        tokens = [t for t in _PKG_TOKEN_RX.findall(text) if t.lower() not in _NOISE_WORDS]
        candidate = tokens[0] if len(tokens) == 1 else ""

    candidate = (candidate or "").strip().strip(".,!?")
    if not candidate:
        return {"detected": True, "package": None, "ambiguous": True,
                "reason": "request me install to hai par package ka naam nahi mila"}
    if candidate.lower() in _RUNTIME_WORDS:
        return {"detected": True, "package": candidate, "ambiguous": True,
                "reason": (f"'{candidate}' ek Python package nahi lagta — ye runtime/app ka naam "
                           "hai, jise pip install nahi kar sakta")}
    return {"detected": True, "package": candidate, "ambiguous": False, "reason": ""}


def install_clarification(intent: dict[str, Any]) -> str:
    """One question, one decision — never a second follow-up."""
    package = intent.get("package")
    if not package:
        return ("🤔 Kaunsa Python package install karun? Ek naam bolo "
                "(jaise `requests`) to main `pip install <naam>` ek baar chalaa dunga.")
    return (f"🤔 '{package}' Python package nahi lagta (runtime/app ka naam hai), isliye main "
            "andaza nahi laga raha. Ek Python package ka naam bolo — ya bolo "
            f"`pip install {package}` hi chala dun?")


async def code_worker(state: JarvisState) -> dict[str, Any]:
    """Code likho → chalao → error par khud fix karo (self-healing)."""
    blocked = _permission_block(state, "code", "code_engine")
    if blocked:
        return blocked
    request = state.get("request", "")
    refine_hint = state.get("refine_hint", "")
    task = f"{request}\n\nRefine hint: {refine_hint}" if refine_hint else request
    done_actions = dict(state.get("done_actions", {}) or {})
    action_args = {"task": task, "language": "python", "run": True}
    started = time.time()
    from ...tools.code_engine import code_engine_run
    from ..idempotency import completed_action, remember_completed_action

    cached = completed_action(done_actions, "code_engine", action_args)
    if cached and isinstance(cached.get("result"), dict):
        outcome = dict(cached["result"])
    else:
        outcome = await code_engine_run(task=task, language="python", run=True)
        if outcome.get("ok"):
            remember_completed_action(
                done_actions, "code_engine", action_args, outcome,
                output=str(outcome.get("report", "")), worker="code",
            )
    summary = (
        f"task: {request[:200]}\n"
        f"script: {outcome.get('script')}\n"
        f"attempts: {outcome.get('attempts')}\n"
        f"ok: {outcome.get('ok')}\n\n"
        f"{outcome.get('report', '')[:5000]}"
    )
    code_evidence = [{
        "worker": "code", "name": "code_engine", "args": action_args,
        "output": str(outcome.get("report", ""))[:800] or "(koi output nahi)",
        "ok": bool(outcome.get("ok")), "source": "deterministic",
    }]
    return {
        "findings": [_finding("code", summary, artifacts=outcome.get("artifacts", []),
                              seconds=round(time.time() - started, 1), ok=outcome.get("ok"),
                              tool_calls=code_evidence)],
        "artifacts": outcome.get("artifacts", []),
        "execution_required": True,           # code worker hamesha asli execution hai
        "tool_evidence": code_evidence,
        "done_actions": done_actions,
        "events": [_event(state, "code_worker", f"{outcome.get('attempts')} attempts • ok={outcome.get('ok')}")],
    }
