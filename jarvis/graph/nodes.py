"""Compatibility exports for the split graph implementation.

New code should import from routing, workers, verification, synthesis, critic, or refine.
"""
from __future__ import annotations

# Legacy module-level dependencies remain available for downstream integrations.
from ..config import MAX_AUTO_REFINE_PASSES, MAX_GRAPH_ITERATIONS, OFFLINE_MODE, OWNER_NAME, PROVIDER
from ..dspy_engine import modules as dspy_mods
from ..llm import get_chat_model
from ..memory.session_state import get_state
from ..memory.store import get_memory, owner_profile_block
from ..skills.loader import skills_prompt_block
from .critic import after_critic, critic_node
from .refine import (
    _execution_signature,
    _max_refine_passes,
    _requires_user_retry,
    _worker_succeeded,
    refine_dispatch,
    refine_node,
)
from .routing import (
    _worker_sends,
    after_route,
    clarify_node,
    dispatch,
    plan_node,
    recall_node,
    route_node,
)
from .state import JarvisState, evidence_digest
from .synthesis import (
    _offline_synthesize,
    _shorten,
    build_reasoning_summary,
    persist_node,
    respond_node,
    synthesize_node,
)
from .verification import (
    _claims_success,
    decompose_requirements,
    evaluate_requirements,
    finalize_requirements,
    honesty_guard,
    requirement_worker,
    verification_node,
)
from .workers import WORKER_NODES, browser_worker, code_worker, desktop_worker, memory_worker, research_worker
from .workers.common import (
    SubAgentRun,
    _event,
    _extract_urls,
    _finding,
    _looks_like_failure,
    _permission_block,
    _run_react_subagent,
    _short_args,
    _tool_call_record,
    _wrap_tools_with_ledger,
)
from .workers.desktop import _desktop_tools_and_blocked
from .workers.desktop_actions import (
    _ACTION_TOOL_NAMES,
    _execute_desktop_action,
    _needs_action,
    detect_desktop_action,
)
from .workers.offline import (
    _clean_hint,
    _ext_filter,
    _extract_app_hint,
    _extract_file_hint,
    _extract_note_body,
    _extract_note_name,
    _local_file_find,
    _local_file_read,
    _offline_tool_run,
)
from .workers.research import _guess_fields

__all__ = [
    "JarvisState", "WORKER_NODES", "MAX_AUTO_REFINE_PASSES", "MAX_GRAPH_ITERATIONS", "OFFLINE_MODE",
    "OWNER_NAME", "PROVIDER", "dspy_mods", "get_chat_model", "get_state", "get_memory", "owner_profile_block",
    "skills_prompt_block", "evidence_digest", "_event", "_finding", "_permission_block", "_extract_urls",
    "_guess_fields", "browser_worker", "research_worker", "code_worker", "desktop_worker", "memory_worker",
    "_desktop_tools_and_blocked", "detect_desktop_action", "_execute_desktop_action",
    "_ACTION_TOOL_NAMES", "_needs_action", "SubAgentRun", "_short_args", "_looks_like_failure",
    "_tool_call_record", "_wrap_tools_with_ledger", "_run_react_subagent", "_offline_tool_run",
    "_extract_file_hint", "_clean_hint", "_extract_app_hint", "_ext_filter", "_extract_note_body",
    "_extract_note_name", "_local_file_find", "_local_file_read", "_claims_success", "honesty_guard",
    "synthesize_node", "_offline_synthesize", "critic_node", "_execution_signature", "_requires_user_retry",
    "_worker_succeeded", "_max_refine_passes", "refine_node", "persist_node", "build_reasoning_summary",
    "respond_node", "_shorten", "after_route", "_worker_sends", "dispatch", "refine_dispatch", "after_critic",
    "recall_node", "route_node", "clarify_node", "plan_node", "verification_node",
    "decompose_requirements", "evaluate_requirements", "finalize_requirements", "requirement_worker",
]
