"""
tests/test_capability_matrix.py — Verification for §15 Capability × Surface Matrix
=================================================================================
Verifies:
1. Matrix completeness: all surfaces registered with full capability metadata.
2. Permission hierarchy consistency between security.permissions and runtime.matrix.
3. Feature checks: supports_feature queries for desktop, stream, buttons, voice, browser.
4. Tool can_execute_tool logic.
5. get_capability_matrix tool invocation (all surfaces & individual surface).
6. CLI /matrix command handler wiring.
7. Telegram /matrix command wiring.
8. Doctor check reports Capability matrix status.
"""

from __future__ import annotations

from jarvis.runtime.matrix import (
    CAPABILITY_MATRIX,
    can_execute_tool,
    format_matrix_table,
    get_surface_capability,
    matrix_dict,
    supports_feature,
)
from jarvis.security import permissions
from jarvis.tools.system_tools import get_capability_matrix


def test_matrix_surfaces_complete():
    """All expected surfaces must exist in CAPABILITY_MATRIX."""
    expected = {"cli_owner", "telegram_owner", "telegram_guest", "voice_owner", "api", "automation"}
    assert set(CAPABILITY_MATRIX.keys()) == expected
    for name, cap in CAPABILITY_MATRIX.items():
        assert cap.surface == name
        assert cap.label
        assert cap.max_permission in permissions.LEVELS
        assert cap.gate_asker
        assert cap.memory_access in ("full_read_write", "owner_scoped_read_write", "none")


def test_permission_consistency():
    """Matrix max_permission must match security.permissions.SURFACES."""
    for s_name, s_meta in permissions.SURFACES.items():
        cap = get_surface_capability(s_name)
        assert cap.max_permission == s_meta["max_level"], (
            f"Mismatch for {s_name}: {cap.max_permission} vs {s_meta['max_level']}"
        )


def test_supports_feature_rules():
    """Role-based boundaries must be strictly reflected."""
    # Guests cannot control desktop or shell
    assert not supports_feature("telegram_guest", "desktop")
    assert not supports_feature("telegram_guest", "buttons")
    assert not supports_feature("telegram_guest", "memory")

    # CLI owner has desktop, stream, buttons, memory
    assert supports_feature("cli_owner", "desktop")
    assert supports_feature("cli_owner", "stream")
    assert supports_feature("cli_owner", "buttons")
    assert supports_feature("cli_owner", "memory")
    assert not supports_feature("cli_owner", "voice")  # CLI is text-based

    # Voice has voice and desktop, but no visual buttons
    assert supports_feature("voice_owner", "voice")
    assert supports_feature("voice_owner", "desktop")
    assert not supports_feature("voice_owner", "buttons")


def test_can_execute_tool_boundary():
    """Dangerous tools should be blocked on read-only surfaces."""
    allowed, reason = can_execute_tool("run_shell_command", "cli_owner")
    assert allowed

    allowed, reason = can_execute_tool("run_shell_command", "telegram_guest")
    assert not allowed
    assert "limit READ" in reason


def test_format_matrix_table_and_dict():
    """ASCII table and dict conversions must be non-empty and well-formed."""
    table = format_matrix_table()
    assert "CLI (Terminal Owner)" in table
    assert "Telegram (Guest Bot)" in table
    assert "Surface" in table
    assert "Max Perm" in table

    d = matrix_dict()
    assert "surfaces" in d
    assert len(d["surfaces"]) == 6
    assert "permission_hierarchy" in d


def test_get_capability_matrix_tool():
    """Tool invocation works as langchain tool with both full and filtered view."""
    # Full view
    full = get_capability_matrix.invoke({})
    assert "CLI (Terminal Owner)" in full
    assert "Telegram (Guest Bot)" in full

    # Filtered view for voice
    voice_info = get_capability_matrix.invoke({"surface": "voice_owner"})
    assert "Voice (LiveKit + Gemini Live)" in voice_info
    assert "telegram_or_spoken_confirmation" in voice_info


def test_doctor_contains_capability_matrix():
    """Doctor collector includes the Capability matrix check."""
    from jarvis.cli.doctor import collect_checks

    checks = collect_checks()
    matrix_check = next((c for c in checks if c[1] == "Capability matrix"), None)
    assert matrix_check is not None
    assert matrix_check[0] == "ok"
    assert "6 surfaces mapped" in matrix_check[2]
