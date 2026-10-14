"""
jarvis/runtime/modes.py — uniform modes (text / voice / telegram)
===================================================================
Ek hi agent, teen interface. Ye module mode↔surface mapping ek jagah rakhta hai
taaki CLI/Telegram/voice/API sab same meaning use karein (koi special-casing nahi):

    mode "text"     → surface cli_owner        (terminal chat)
    mode "voice"    → surface voice_owner      (LiveKit mic)
    mode "telegram" → surface telegram_owner   (phone bot; guests → telegram_guest)

Mode ka matlab sirf "main kahan baat kar raha hoon" — brain, memory, tools,
Gate — sab same rehte hain.
"""

from __future__ import annotations

MODES = ("text", "voice", "telegram")

_MODE_SURFACE_OWNER = {
    "text": "cli_owner",
    "voice": "voice_owner",
    "telegram": "telegram_owner",
}


def normalize_mode(mode: str | None) -> str:
    """Unknown/khaali mode → 'text' (safe default)."""
    m = (mode or "").strip().lower()
    return m if m in MODES else "text"


def surface_for_mode(mode: str | None, role: str = "owner") -> str:
    """Mode (+role) → gate/permission surface. Guests sirf telegram par hote hain."""
    m = normalize_mode(mode)
    if m == "telegram" and (role or "").strip().lower() != "owner":
        return "telegram_guest"
    return _MODE_SURFACE_OWNER[m]


def current_mode() -> str:
    """Abhi ka mode (runtime setting `mode`, default text)."""
    try:
        from ..config import runtime as R

        return normalize_mode(R.get("mode", "text"))
    except Exception:
        return "text"


def set_mode(mode: str) -> tuple[dict, list[str]]:
    """Mode badlo (runtime setting). Returns (applied, errors) — R.set_many jaisa."""
    from ..config import runtime as R

    return R.set_many(mode=normalize_mode(mode))


def describe_mode(mode: str | None = None) -> str:
    """Human line: ye mode kya hai."""
    m = normalize_mode(mode or current_mode())
    return {
        "text": "text — terminal me chat (CLI)",
        "voice": "voice — mic se baat (LiveKit + Gemini Live)",
        "telegram": "telegram — phone se bot (Telegram)",
    }[m]


def mode_entry_hint(mode: str | None = None) -> str:
    """Is mode ko chalane ka command (CLI/Telegram dono yahi dikhate hain)."""
    m = normalize_mode(mode or current_mode())
    return {
        "text": "text mode: `python -m jarvis cli` (terminal chat)",
        "voice": "voice mode: `python -m jarvis voice` chalao (LiveKit keys chahiye — `voice keys` dekho)",
        "telegram": "telegram mode: `python -m jarvis telegram` chalao, phir phone par /start bhejo",
    }[m]
