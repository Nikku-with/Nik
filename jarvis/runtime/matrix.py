"""
jarvis/runtime/matrix.py — Capability × Surface Matrix (§15)
============================================================
Single source of truth for what each surface is capable of:
  - CLI (owner)
  - Telegram (owner)
  - Telegram (guest)
  - Voice (owner)
  - HTTP API
  - Automation (scheduler)

Capabilities tracked:
  - max_permission_level: READ < WRITE < EXECUTE < SYSTEM < DANGEROUS
  - gate_approval_asker: how approval is asked on dangerous steps
  - memory_access: read/write/none/owner_scoped
  - progress_stream: live progress sink available
  - buttons_interactive: can show/respond to inline buttons
  - voice_speech: TTS/STT capability
  - desktop_control: can move mouse/keyboard/apps directly
  - supervised_browser: can run browser automation tasks
  - persistent_session: state preserved across turns
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..security import permissions


@dataclass(frozen=True)
class SurfaceCapability:
    surface: str
    label: str
    max_permission: str
    gate_asker: str
    memory_access: str
    progress_stream: bool
    buttons_interactive: bool
    voice_speech: bool
    desktop_control: bool
    supervised_browser: bool
    persistent_session: bool
    description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


CAPABILITY_MATRIX: dict[str, SurfaceCapability] = {
    "cli_owner": SurfaceCapability(
        surface="cli_owner",
        label="CLI (Terminal Owner)",
        max_permission="DANGEROUS",
        gate_asker="interactive_terminal_y_n",
        memory_access="full_read_write",
        progress_stream=True,
        buttons_interactive=True,  # key_menu pro_ui
        voice_speech=False,  # text-in/text-out (voice command launches voice mode)
        desktop_control=True,
        supervised_browser=True,
        persistent_session=True,
        description="Full power terminal operator — direct terminal prompts, rich dashboard.",
    ),
    "telegram_owner": SurfaceCapability(
        surface="telegram_owner",
        label="Telegram (Owner Bot)",
        max_permission="DANGEROUS",
        gate_asker="inline_buttons_yes_no",
        memory_access="owner_scoped_read_write",
        progress_stream=True,  # live-edited progress message
        buttons_interactive=True,  # InlineKeyboards + tg_send_buttons
        voice_speech=True,  # voice notes (transcribed / voice replies)
        desktop_control=True,
        supervised_browser=True,
        persistent_session=True,
        description="Owner phone control via Telegram — inline approval buttons, live edits.",
    ),
    "telegram_guest": SurfaceCapability(
        surface="telegram_guest",
        label="Telegram (Guest Bot)",
        max_permission="READ",
        gate_asker="auto_deny",
        memory_access="none",
        progress_stream=True,
        buttons_interactive=False,
        voice_speech=False,
        desktop_control=False,
        supervised_browser=False,
        persistent_session=False,
        description="Public/guest safe boundary — strictly read-only, no desktop/shell, auto-deny gate.",
    ),
    "voice_owner": SurfaceCapability(
        surface="voice_owner",
        label="Voice (LiveKit + Gemini Live)",
        max_permission="EXECUTE",
        gate_asker="telegram_or_spoken_confirmation",
        memory_access="full_read_write",
        progress_stream=False,  # audio stream replaces visual progress
        buttons_interactive=False,
        voice_speech=True,
        desktop_control=True,
        supervised_browser=False,  # heavy browser discouraged via voice; routed to subagent
        persistent_session=True,
        description="Hands-free natural speech agent via LiveKit WebRTC + Gemini Realtime.",
    ),
    "api": SurfaceCapability(
        surface="api",
        label="HTTP API (FastAPI /serve)",
        max_permission="WRITE",
        gate_asker="approval_token_header",
        memory_access="full_read_write",
        progress_stream=True,  # SSE /stream endpoint
        buttons_interactive=False,
        voice_speech=False,
        desktop_control=False,
        supervised_browser=False,
        persistent_session=True,
        description="Programmatic HTTP/SSE bridge for external webhooks and integrations.",
    ),
    "automation": SurfaceCapability(
        surface="automation",
        label="Automation Scheduler",
        max_permission="EXECUTE",
        gate_asker="telegram_owner_bridge",
        memory_access="full_read_write",
        progress_stream=False,  # runs in background worker thread
        buttons_interactive=False,
        voice_speech=False,
        desktop_control=True,
        supervised_browser=True,
        persistent_session=False,  # runs per scheduled trigger
        description="Background cron/interval runner; delivers results to configured channels.",
    ),
}


def get_surface_capability(surface: str) -> SurfaceCapability:
    """Retrieve capability profile for a surface (defaulting to cli_owner if unknown)."""
    s = (surface or "").strip().lower()
    return CAPABILITY_MATRIX.get(s, CAPABILITY_MATRIX["cli_owner"])


def can_execute_tool(tool_name: str, surface: str) -> tuple[bool, str]:
    """Check if tool can be executed on this surface based on permission level."""
    dec = permissions.check(tool_name, surface)
    return dec.allowed, dec.reason or f"allowed at {dec.level} level"


def supports_feature(surface: str, feature: str) -> bool:
    """Check if surface supports a specific capability flag."""
    cap = get_surface_capability(surface)
    feat = feature.strip().lower()
    if feat in ("stream", "progress", "progress_stream", "streaming"):
        return cap.progress_stream
    if feat in ("buttons", "buttons_interactive", "menu"):
        return cap.buttons_interactive
    if feat in ("voice", "voice_speech", "speech"):
        return cap.voice_speech
    if feat in ("desktop", "desktop_control", "mouse", "keyboard"):
        return cap.desktop_control
    if feat in ("browser", "supervised_browser", "browser_use"):
        return cap.supervised_browser
    if feat in ("session", "persistent_session", "state"):
        return cap.persistent_session
    if feat in ("memory", "memory_access"):
        return cap.memory_access != "none"
    return False


def format_matrix_table() -> str:
    """Render capability x surface matrix as a clean ASCII table."""
    headers = [
        "Surface",
        "Max Perm",
        "Gate Asker",
        "Memory",
        "Stream",
        "Buttons",
        "Voice",
        "Desktop",
    ]
    rows = []
    for cap in CAPABILITY_MATRIX.values():
        rows.append(
            [
                cap.label,
                cap.max_permission,
                cap.gate_asker[:14] + ("…" if len(cap.gate_asker) > 14 else ""),
                "read/write" if "write" in cap.memory_access else ("none" if cap.memory_access == "none" else "read"),
                "✅" if cap.progress_stream else "❌",
                "✅" if cap.buttons_interactive else "❌",
                "✅" if cap.voice_speech else "❌",
                "✅" if cap.desktop_control else "❌",
            ]
        )

    col_widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            col_widths[idx] = max(col_widths[idx], len(cell))

    def make_row(cells: list[str]) -> str:
        return " | ".join(f"{c:<{col_widths[i]}}" for i, c in enumerate(cells))

    sep = "-+-".join("-" * w for w in col_widths)
    lines = [
        make_row(headers),
        sep,
        *[make_row(r) for r in rows],
    ]
    return "\n".join(lines)


def matrix_dict() -> dict[str, Any]:
    """Serialisable dict of capability matrix for doctor / inspect tools."""
    return {
        "surfaces": {k: v.to_dict() for k, v in CAPABILITY_MATRIX.items()},
        "permission_hierarchy": list(permissions.LEVELS),
    }
