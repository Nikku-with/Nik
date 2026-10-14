"""
jarvis/tools — saare tools ek jagah (registry)
=============================================

Supervisor, sub-agents, voice agent aur API server — sab yahi se tools lete hain.
Tool groups alag-alag rakhe gaye hain taaki har agent ko sirf uska relevant subset mile
(isse LLM ka decision fatigue kam hota hai aur speed badhti hai — OpenClaw ka tool-policy idea).
"""
from __future__ import annotations

from .app_finder import APP_FINDER_TOOLS
from .browser_tools import BROWSER_TOOLS
from .code_engine import CODE_TOOLS
from .file_tools import FILE_TOOLS
from .notes_tools import NOTE_TOOLS
from .system_tools import SYSTEM_TOOLS
from .web_tools import WEB_TOOLS

try:
    from ..runtime.process_manager import PROCESS_TOOLS
except Exception:  # pragma: no cover
    PROCESS_TOOLS = []

try:
    from ..runtime.repl import REPL_TOOLS
except Exception:  # pragma: no cover
    REPL_TOOLS = []

try:
    from ..memory.session_state import SESSION_TOOLS
except Exception:  # pragma: no cover
    SESSION_TOOLS = []

try:
    from ..skills.loader import SKILL_TOOLS
except Exception:  # pragma: no cover
    SKILL_TOOLS = []

try:
    from ..memory.memory_tools import MEMORY_TOOLS
except Exception:  # pragma: no cover
    MEMORY_TOOLS = []  # type: ignore[assignment]

try:
    from ..automation.tools import AUTOMATION_TOOLS
except Exception:  # pragma: no cover
    AUTOMATION_TOOLS = []

try:
    from .system_ops import SYSTEM_OPS_TOOLS
except Exception:  # pragma: no cover
    SYSTEM_OPS_TOOLS = []

__all__ = [
    "APP_FINDER_TOOLS", "BROWSER_TOOLS", "CODE_TOOLS", "FILE_TOOLS", "NOTE_TOOLS",
    "SYSTEM_TOOLS", "WEB_TOOLS", "PROCESS_TOOLS", "REPL_TOOLS", "SESSION_TOOLS",
    "SKILL_TOOLS", "MEMORY_TOOLS", "AUTOMATION_TOOLS", "SYSTEM_OPS_TOOLS",
    "DESKTOP_TOOLS", "RUNTIME_TOOLS", "RESEARCH_TOOLS", "ALL_TOOLS",
    "tools_for", "tool_names", "describe_tools", "tool_policy_summary",
]

# desktop = system + files + notes + app discovery  (local machine ke kaam)
DESKTOP_TOOLS = [*SYSTEM_TOOLS, *FILE_TOOLS, *NOTE_TOOLS, *APP_FINDER_TOOLS]
# runtime = apna code khud chalao (REPL) + short-term state
RUNTIME_TOOLS = [*REPL_TOOLS, *SESSION_TOOLS]
# research = web + browser (internet ke kaam)
RESEARCH_TOOLS = [*WEB_TOOLS, *BROWSER_TOOLS]
CODE_GROUP = [*CODE_TOOLS]
# ops = system maintenance (refresh/clear/status) + scheduled automations
OPS_TOOLS = [*SYSTEM_OPS_TOOLS, *AUTOMATION_TOOLS]

ALL_TOOLS = [*DESKTOP_TOOLS, *RUNTIME_TOOLS, *RESEARCH_TOOLS, *CODE_GROUP,
             *MEMORY_TOOLS, *SKILL_TOOLS, *OPS_TOOLS]

# Agent → tools mapping (supervisor routing isi se hoti hai)
_TOOL_GROUPS: dict[str, list] = {
    "desktop": DESKTOP_TOOLS,
    "runtime": RUNTIME_TOOLS,
    "research": RESEARCH_TOOLS,
    "web": WEB_TOOLS,
    "browser": BROWSER_TOOLS,
    "code": CODE_GROUP,
    "memory": MEMORY_TOOLS,
    "skills": SKILL_TOOLS,
    "ops": OPS_TOOLS,
    "automation": AUTOMATION_TOOLS,
    "system_ops": SYSTEM_OPS_TOOLS,
    "all": ALL_TOOLS,
}


def tools_for(*groups: str) -> list:
    """Groups ke tools dedupe karke do (naam ke hisaab se)."""
    seen: dict[str, object] = {}
    for group in groups or ("all",):
        for t in _TOOL_GROUPS.get(group, []):
            seen[getattr(t, "name", str(t))] = t
    return list(seen.values())


def tool_names(tools: list | None = None) -> list[str]:
    return [getattr(t, "name", str(t)) for t in (tools if tools is not None else ALL_TOOLS)]


def describe_tools() -> str:
    """LLM prompt me daalne ke liye tool catalogue (fast routing ke liye)."""
    lines = []
    for group, tools in _TOOL_GROUPS.items():
        if group == "all":
            continue
        lines.append(f"### {group} ({len(tools)})")
        for t in tools:
            desc = (getattr(t, "description", "") or "").strip().split("\n")[0][:130]
            lines.append(f"- {getattr(t, 'name', '?')}: {desc}")
    return "\n".join(lines)


def tool_policy_summary() -> str:
    """Kaun sa agent kitne tools dekhta hai (policy sanity check)."""
    return (
        f"desktop={len(DESKTOP_TOOLS)} | runtime={len(RUNTIME_TOOLS)} | research={len(RESEARCH_TOOLS)} | "
        f"code={len(CODE_GROUP)} | memory={len(MEMORY_TOOLS)} | skills={len(SKILL_TOOLS)} | "
        f"TOTAL unique={len(tool_names())}"
    )
