"""
Permissions — kaun sa tool kis level par chalega
==============================================

Blacklist-only approach kamzor hoti hai. Yahan **permission levels** hain:

    READ      → sirf padhna (search, file read, system info)
    WRITE     → kuch badalna (notes, files likhna, scripts)
    EXECUTE   → code/shell chalao, processes, browser automation
    SYSTEM    → machine-level (app launch, desktop control, os ops)
    DANGEROUS → destructive (delete, kill, installers) → owner approval chahiye

Har surface (CLI owner / Telegram owner / Telegram guest) ka apna allowed-max-level hai.
Isliye Telegram pe koi random banda desktop control nahi kar sakta, chahe wo kuch bhi bheje.
"""
from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

_CURRENT_SURFACE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nikhilai_permission_surface", default="cli_owner")


@contextmanager
def use_surface(surface: str) -> Iterator[None]:
    """Bind permission checks to the active interface for the lifetime of one request."""
    token = _CURRENT_SURFACE.set(str(surface or "cli_owner"))
    try:
        yield
    finally:
        _CURRENT_SURFACE.reset(token)


def current_surface() -> str:
    return _CURRENT_SURFACE.get()

LEVELS = ("READ", "WRITE", "EXECUTE", "SYSTEM", "DANGEROUS")

# tool group → level
TOOL_LEVELS: dict[str, str] = {
    # read-only
    "web": "READ", "research": "READ", "memory_read": "READ", "system_info": "READ",
    "db_read": "READ", "api_read": "READ",
    # write (apne workspace me)
    "files": "WRITE", "notes": "WRITE", "memory": "WRITE", "skills": "READ",
    "desktop": "SYSTEM", "apps": "SYSTEM", "settings": "WRITE", "automation": "WRITE",
    # execute
    "code": "EXECUTE", "shell": "EXECUTE", "runtime": "EXECUTE", "process": "EXECUTE",
    "browser": "EXECUTE", "voice": "READ",
    # destructive / system-wide
    "system": "SYSTEM", "dangerous": "DANGEROUS",
}

SURFACES: dict[str, dict[str, str]] = {
    "cli_owner": {"max_level": "DANGEROUS", "label": "CLI (owner)"},
    "telegram_owner": {"max_level": "DANGEROUS", "label": "Telegram (owner)"},
    "telegram_guest": {"max_level": "READ", "label": "Telegram (guest)"},
    "voice_owner": {"max_level": "EXECUTE", "label": "Voice (owner)"},
    "api": {"max_level": "WRITE", "label": "API/web owner"},
    "automation": {"max_level": "EXECUTE", "label": "Scheduled task (owner)"},
}

# These owner-facing desktop controls are the only SYSTEM-level exception on API, voice, and
# scheduled surfaces. Shell/code/process capabilities keep their existing surface ceilings.
OWNER_DESKTOP_SYSTEM_TOOLS = {
    "open_app", "open_any_app", "close_app", "take_screenshot", "desktop_observe", "desktop_ocr",
    "desktop_click", "desktop_move", "desktop_scroll", "desktop_drag", "desktop_type", "desktop_press",
    "desktop_windows", "desktop_focus", "desktop_clipboard_read", "desktop_clipboard_write",
    "type_text", "press_hotkey", "list_windows", "focus_window", "click_text", "wait_for_text",
    "find_image", "describe_screen", "key_down", "key_up", "hold",
}


def level_index(level: str) -> int:
    try:
        return LEVELS.index(level.upper())
    except ValueError:
        return len(LEVELS) - 1


@dataclass
class Decision:
    allowed: bool
    level: str
    max_level: str
    reason: str = ""

    def as_text(self) -> str:
        return "✅ allowed" if self.allowed else f"🚫 blocked — {self.reason}"


def tool_level(tool_name: str) -> str:
    """Tool ka name dekh kar level guess karo (registry se bhi override ho sakta hai)."""
    name = (tool_name or "").lower()
    if name == "tg_send_buttons":
        return "WRITE"
    tokens = set(name.split("_"))
    dangerous_tokens = {"delete", "remove", "kill", "uninstall", "wipe", "terminate", "destroy"}
    if (name in {"process_stop", "clear_process_tool", "clear_cache_tool", "install_package"}
            or tokens & dangerous_tokens or "clear_cache" in name):
        return "DANGEROUS"
    # Restarting the assistant kills the running process — it must not sit at READ, or a
    # guest surface (max READ) can restart the owner's assistant at will.
    if tokens & {"restart", "reboot", "shutdown"} or name in {"restart_jarvis_tool", "quit_tool"}:
        return "DANGEROUS"
    # Handing a path to the OS shell opens/executes whatever is there.
    if name in {"play_file", "open_path", "open_file", "launch_file", "run_file"}:
        return "EXECUTE"
    if any(k in name for k in ("run_shell", "shell", "exec", "python_repl", "run_python", "run_script",
                               "code_engine", "process", "browser", "playwright", "run_code")):
        return "EXECUTE"
    if name in {"click_text", "wait_for_text", "find_image", "describe_screen", "key_down", "key_up", "hold",
                "drag", "desktop_drag", "desktop_scroll"}:
        return "SYSTEM"
    if any(k in name for k in ("open_app", "open_any_app", "close_app", "launch", "window", "screenshot", "type_text",
                               "press_hotkey", "volume", "desktop", "system_info", "refresh_system")):
        return "SYSTEM"
    if name in {"undo_file_operation", "propose_self_improvement"}:
        return "WRITE"
    if any(k in name for k in ("write", "save", "note", "mkdir", "move", "rename", "set_", "settings",
                               "automation", "remember", "forget", "cron", "create_project")):
        return "WRITE"
    # These rewrite persistent state even though their names read like reads.
    if name in {"memory_compact", "refresh_app_index", "reload_config_tool", "rebuild_index"}:
        return "WRITE"
    return "READ"


# Not every name handed to `check()` is a registered tool: workers gate whole capabilities
# ("browser", "research") and some internal functions ("web_search", "fetch_url_text"). These
# are the exact set of such names in the tree — tests/test_wiring.py asserts the list is
# neither stale nor padded, so a new capability name cannot silently widen the gate.
CAPABILITY_ALIASES = frozenset({
    "browser", "code", "memory", "memory_read", "research", "run_script", "settings",
    "web_search",
})

_unknown_tool_cache: dict[str, bool] = {}


def _is_known_tool(tool_name: str) -> bool:
    """True when `tool_name` is a registered tool, a capability alias, or a known prefix.

    The default of `tool_level()` is READ, so an unrecognised name would otherwise sail
    through every surface, including guest (max READ). A tool added to the registry without
    a classification rule must not inherit the weakest level by silence.
    """
    name = str(tool_name or "").strip().lower()
    if not name or name in CAPABILITY_ALIASES:
        return True
    cached = _unknown_tool_cache.get(name)
    if cached is not None:
        return cached
    known = True
    try:  # lazy: jarvis.tools imports this module, so importing it here would cycle
        from ..tools import ALL_TOOLS

        registered = {str(getattr(t, "name", "")).lower() for t in ALL_TOOLS}
        known = name in registered or any(r.startswith(name) for r in registered if r)
    except Exception:
        known = True  # registry unavailable → keep the previous permissive behaviour
    _unknown_tool_cache[name] = known
    return known


def check(tool_name: str, surface: str | None = None, explicit_level: str | None = None) -> Decision:
    """Kya ye tool active surface par allowed hai? Unknown surfaces aur unknown tools fail closed."""
    level = (explicit_level or tool_level(tool_name)).upper()
    surface = str(surface or current_surface())
    surface_config = SURFACES.get(surface)
    if not _is_known_tool(tool_name):
        return Decision(False, level, surface_config["max_level"] if surface_config else "NONE",
                        reason=f"'{tool_name}' koi registered tool nahi hai — fail closed")
    if surface_config is None:
        return Decision(False, level, "NONE", reason=f"unknown permission surface '{surface}'")
    max_level = surface_config["max_level"]
    normalized_name = str(tool_name or "").lower()
    if (surface in {"api", "voice_owner", "automation"}
            and normalized_name in OWNER_DESKTOP_SYSTEM_TOOLS and level == "SYSTEM"):
        return Decision(True, level, max_level, reason="owner desktop-control exception")
    if level_index(level) <= level_index(max_level):
        return Decision(True, level, max_level)
    return Decision(False, level, max_level,
                    reason=f"'{tool_name}' = {level} level, par '{surface}' ki limit {max_level} hai")


def allowed_surfaces(tool_name: str) -> list[str]:
    return [s for s in SURFACES if check(tool_name, s).allowed]


def summary() -> str:
    lines = ["Permission matrix:"]
    for _surface, meta in SURFACES.items():
        lines.append(f"  {meta['label']:<20} max: {meta['max_level']}")
    lines.append("Levels: " + " < ".join(LEVELS))
    return "\n".join(lines)


def set_surface_level(surface: str, level: str) -> bool:
    if surface not in SURFACES or level.upper() not in LEVELS:
        return False
    SURFACES[surface]["max_level"] = level.upper()
    return True
