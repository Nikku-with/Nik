"""Safe source self-improvement workflow: record proposals, never auto-edit source."""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from langchain_core.tools import tool

from ..config import WORKSPACE_DIR

MAX_PROPOSAL_CHARS = 120_000


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".proposal-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


@tool
def propose_self_improvement(title: str, rationale: str, unified_diff: str) -> str:
    """Save a human-reviewable source patch proposal without changing or executing project code."""
    diff = str(unified_diff or "").strip()
    if not diff or len(diff) > MAX_PROPOSAL_CHARS:
        return f"❌ Proposal diff khali ya bahut bada hai (limit {MAX_PROPOSAL_CHARS} chars)."
    has_headers = "diff --git " in diff or ("--- " in diff and "+++ " in diff and "@@" in diff)
    if not has_headers:
        return "❌ Unified diff headers (`---`, `+++`, `@@`) chahiye; source file nahi badli."

    ident = uuid.uuid4().hex[:12]
    folder = Path(WORKSPACE_DIR) / "proposals"
    patch_path = folder / f"self_improvement_{ident}.patch"
    metadata_path = folder / f"self_improvement_{ident}.json"
    metadata = {
        "id": ident,
        "title": str(title or "untitled improvement")[:160],
        "rationale": str(rationale or "")[:1000],
        "patch_file": patch_path.name,
        "created_at": time.time(),
        "applied": False,
        "verification": "not run; review and test manually before applying",
    }
    try:
        _atomic_write(patch_path, diff + "\n")
        _atomic_write(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    except Exception as exc:
        patch_path.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
        return f"❌ Improvement proposal save nahi hui ({type(exc).__name__}); source unchanged."
    return (
        f"✅ Source improvement proposal saved: {patch_path}\n"
        "Jarvis source unchanged hai. Owner review, tests, aur manual apply ke bina patch execute/apply nahi hoga."
    )


__all__ = ["propose_self_improvement"]
