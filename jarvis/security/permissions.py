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

from dataclasses import dataclass

LEVELS = ("READ", "WRITE", "EXECUTE", "SYSTEM", "DANGEROUS")

# tool group → level
TOOL_LEVELS: dict[str, str] = {
    # read-only
    "web": "READ",
    "research": "READ",
    "memory_read": "READ",
    "system_info": "READ",
    "db_read": "READ",
    "api_read": "READ",
    # write (apne workspace me)
    "files": "WRITE",
    "notes": "WRITE",
    "memory": "WRITE",
    "skills": "READ",
    "desktop": "SYSTEM",
    "apps": "SYSTEM",
    "settings": "WRITE",
    "automation": "WRITE",
    # execute
    "code": "EXECUTE",
    "shell": "EXECUTE",
    "runtime": "EXECUTE",
    "process": "EXECUTE",
    "browser": "EXECUTE",
    "voice": "READ",
    # destructive / system-wide
    "system": "SYSTEM",
    "dangerous": "DANGEROUS",
}

SURFACES: dict[str, dict[str, str]] = {
    "cli_owner": {"max_level": "DANGEROUS", "label": "CLI (owner)"},
    "telegram_owner": {"max_level": "DANGEROUS", "label": "Telegram (owner)"},
    "telegram_guest": {"max_level": "READ", "label": "Telegram (guest)"},
    "voice_owner": {"max_level": "EXECUTE", "label": "Voice (owner)"},
    "api": {"max_level": "WRITE", "label": "HTTP API"},
    "automation": {"max_level": "EXECUTE", "label": "Scheduled task"},
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
    if name.startswith("tg_"):
        return "WRITE"  # Telegram send tools — guest (READ-only) par blocked
    if any(k in name for k in ("delete", "remove", "kill", "uninstall", "wipe", "clear_cache")):
        return "DANGEROUS"
    if any(
        k in name
        for k in (
            "run_shell",
            "shell",
            "exec",
            "python_repl",
            "run_python",
            "process",
            "browser",
            "playwright",
            "run_code",
        )
    ):
        return "EXECUTE"
    if any(
        k in name
        for k in (
            "open_app",
            "close_app",
            "launch",
            "window",
            "screenshot",
            "type_text",
            "press_hotkey",
            "click",
            "mouse",
            "scroll",
            "screen",
            "volume",
            "desktop",
            "system_info",
            "refresh_system",
        )
    ):
        return "SYSTEM"
    if any(
        k in name
        for k in (
            "write",
            "save",
            "note",
            "mkdir",
            "move",
            "rename",
            "set_",
            "settings",
            "automation",
            "remember",
            "forget",
            "cron",
        )
    ):
        return "WRITE"
    return "READ"


def check(tool_name: str, surface: str = "cli_owner", explicit_level: str | None = None) -> Decision:
    """Kya ye tool is surface par allowed hai?"""
    max_level = SURFACES.get(surface, SURFACES["cli_owner"])["max_level"]
    level = (explicit_level or tool_level(tool_name)).upper()
    if level_index(level) <= level_index(max_level):
        return Decision(True, level, max_level)
    return Decision(
        False, level, max_level, reason=f"'{tool_name}' = {level} level, par '{surface}' ki limit {max_level} hai"
    )


def allowed_surfaces(tool_name: str) -> list[str]:
    return [s for s in SURFACES if check(tool_name, s).allowed]


def summary() -> str:
    lines = ["Permission matrix:"]
    for surface, meta in SURFACES.items():
        lines.append(f"  {meta['label']:<20} max: {meta['max_level']}")
    lines.append("Levels: " + " < ".join(LEVELS))
    return "\n".join(lines)


def set_surface_level(surface: str, level: str) -> bool:
    if surface not in SURFACES or level.upper() not in LEVELS:
        return False
    SURFACES[surface]["max_level"] = level.upper()
    return True
