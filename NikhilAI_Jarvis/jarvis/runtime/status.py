"""
Status helpers — CLI/Telegram/API sab ek hi jagah se agent ka status lete hain
===========================================================================
"""
from __future__ import annotations

import time
from typing import Any

from ..config import settings as S
from ..config import runtime as R


def agent_status(compact: bool = False) -> dict[str, Any]:
    """Ek dict me sab kuch: brain, model, tools, memory, browser, voice, telegram, automations."""
    out: dict[str, Any] = {
        "status": "ONLINE",
        "assistant": R.get("assistant_name", S.ASSISTANT_NAME),
        "owner": R.get("owner_name", S.OWNER_NAME),
        "provider": S.PROVIDER,
        "model": S.MODEL_ROUTES["brain"].model or "(demo)",
        "mode": R.get("mode", S.DEFAULT_MODE),
        "workspace": str(S.WORKSPACE_DIR),
        "offline": S.OFFLINE_MODE,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        from ..llm import health, resolve_role

        h = health()
        out["provider"] = h["active"]
        out["model"] = resolve_role("brain").describe()
        out["available_providers"] = h["available"]
        out["role_models"] = h["roles"]
    except Exception as exc:
        out["llm_error"] = str(exc)[:120]

    try:
        from ..tools import tool_names, tool_policy_summary

        names = tool_names()
        out["tools"] = len(names) if compact else tool_policy_summary()
        out["tool_count"] = len(names)
    except Exception:
        out["tool_count"] = 0

    try:
        from ..memory.store import get_memory

        stats = get_memory().stats()
        out["memory"] = f"{stats.get('facts', 0)} facts, {stats.get('tasks', 0)} tasks, {stats.get('notes', 0)} notes"
    except Exception:
        out["memory"] = "n/a"

    out["browser"] = bool(S.has_browser_use() or S.has_browser_use_cli())
    out["browser_cli"] = S.has_browser_use_cli()
    out["livekit"] = bool(S.has_livekit() and S.LIVEKIT_URL and S.LIVEKIT_API_KEY)
    out["telegram"] = bool(S.TELEGRAM_BOT_TOKEN)
    try:
        from ..automation import scheduler as sch

        st = sch.status()
        out["automations"] = f"{st['enabled']}/{st['total']} on" + (" (scheduler live)" if st["running"] else "")
    except Exception:
        out["automations"] = "n/a"
    return out


def agent_status_text(compact: bool = True) -> str:
    """Markdown text — Telegram/CLI dono ke liye."""
    st = agent_status(compact=compact)
    lines = [
        f"*{st['assistant']}* — {'OFFLINE / DEMO' if st['offline'] else 'ONLINE'}",
        f"• Brain: `{st['provider']}` → `{st['model']}`",
        f"• Mode: `{st['mode']}`  •  Tools: {st.get('tool_count', '?')}",
        f"• Memory: {st['memory']}",
        f"• Browser: {'ready' if st['browser'] else 'off'}  •  LiveKit: {'ready' if st['livekit'] else 'off'}"
        f"  •  Telegram: {'ready' if st['telegram'] else 'off'}",
        f"• Automations: {st['automations']}",
        f"• Workspace: `{st['workspace']}`",
    ]
    if st.get("available_providers"):
        lines.append(f"• Providers with keys: {', '.join(st['available_providers'])}")
    return "\n".join(lines)


def voice_status_text() -> str:
    """Voice mode ka status (Telegram /voice ke liye)."""
    ok = S.has_livekit()
    keys = [k for k, v in (("LIVEKIT_URL", S.LIVEKIT_URL), ("LIVEKIT_API_KEY", S.LIVEKIT_API_KEY),
                           ("LIVEKIT_API_SECRET", S.LIVEKIT_API_SECRET)) if v]
    gem = bool(S.GEMINI_API_KEY)
    lines = [
        "🎙️ *Voice mode*",
        f"• livekit-agents: {'installed' if ok else 'NOT installed (`pip install livekit-agents`)'}",
        f"• keys set: {', '.join(keys) if keys else 'koi nahi'}",
        f"• Gemini Live (realtime brain): {'ready' if gem else 'GEMINI_API_KEY chahiye'}",
        f"• voice: `{R.get('voice_name', S.VOICE_NAME)}`  •  model: `{R.get('voice_model', S.REALTIME_MODEL)}`",
    ]
    if ok and len(keys) == 3 and gem:
        lines.append("\nChalane ke liye: `python -m jarvis voice` (ya `python -m jarvis voice console`)")
    else:
        lines.append("\nSetup: cloud.livekit.io → project → Settings → Keys (URL + key + secret)")
    return "\n".join(lines)
