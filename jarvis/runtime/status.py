"""Truthful runtime/readiness status shared by CLI, Telegram, and API."""
from __future__ import annotations

import time
from typing import Any

from ..config import runtime as R
from ..config import settings as S


def agent_status(compact: bool = False, surface: str = "unspecified") -> dict[str, Any]:
    """Report configured/readiness facts, never infer a connection from an installed package."""
    runtime_provider = str(R.get("provider") or "").strip().lower()
    env_provider = str(getattr(S, "BRAIN_PROVIDER_CONFIG", "auto") or "auto").strip().lower()
    configured_provider = runtime_provider if runtime_provider not in {"", "auto"} else env_provider
    try:
        from .controls import gate_mode_details

        gate_info = gate_mode_details()
    except Exception:
        gate_info = {"mode": "unknown", "source": "status unavailable"}
    out: dict[str, Any] = {
        "status": "DEMO" if S.is_offline() else "CONFIGURED (connectivity not tested)",
        "gate_mode": gate_info.get("mode", "unknown"),
        "gate_mode_source": gate_info.get("source", "status unavailable"),
        "assistant": R.get("assistant_name", S.ASSISTANT_NAME),
        "owner": R.get("owner_name", S.OWNER_NAME),
        "provider": "demo",
        "configured_provider": configured_provider,
        "model": "(no active model)",
        "fast_model": "(no active model)",
        "mode": R.get("mode", S.DEFAULT_MODE),  # preference only, not a running interface
        "interface": surface,
        "workspace": str(S.WORKSPACE_DIR),
        "offline": bool(S.is_offline()),
        "connection_tested": False,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        from ..llm import health, resolve_role

        h = health()
        out["provider"] = h.get("active", "demo")
        brain_route = resolve_role("brain", allow_discovery=False)
        fast_route = resolve_role("fast", allow_discovery=False)
        out["model"] = brain_route.describe() if brain_route.targets else f"(unavailable: {brain_route.reason})"
        out["fast_model"] = fast_route.describe() if fast_route.targets else f"(unavailable: {fast_route.reason})"
        out["model_route_reason"] = brain_route.reason
        out["available_providers"] = h.get("available", [])
        out["role_models"] = h.get("roles", {})
        forced = str(out["configured_provider"]).lower()
        forced_unavailable = forced not in {"", "auto", "demo"} and out["provider"] == "demo"
        if forced_unavailable:
            out["provider_status"] = f"{forced} selected but key unavailable; no provider switch"
            out["offline"] = False
            out["status"] = "MODEL UNAVAILABLE"
        elif out["provider"] == "demo":
            out["provider_status"] = "no provider key; deterministic demo mode"
            out["offline"] = True
            out["status"] = "DEMO"
        elif S.is_offline():
            out["provider_status"] = "offline demo forced; provider API not called"
            out["offline"] = True
            out["status"] = "DEMO"
        elif not brain_route.targets:
            out["provider_status"] = f"brain model route unavailable: {brain_route.reason}"
            out["offline"] = bool(S.is_offline())
            out["status"] = "MODEL UNAVAILABLE"
        elif not fast_route.targets:
            out["provider_status"] = f"brain route configured; fast model route unavailable: {fast_route.reason}"
            out["offline"] = bool(S.is_offline())
            out["status"] = "PARTIAL MODEL CONFIGURATION"
        else:
            out["provider_status"] = "key configured; API connectivity not tested"
            out["offline"] = bool(S.is_offline())
            out["status"] = "DEMO" if out["offline"] else "CONFIGURED (connectivity not tested)"
    except Exception as exc:
        out["llm_error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
        out["provider_status"] = "status unavailable; run doctor"

    try:
        from ..tools import tool_names, tool_policy_summary

        names = tool_names()
        out["tool_count"] = len(names)
        out["tools"] = len(names) if compact else tool_policy_summary()
        out["tools_status"] = "registered tool schemas; runtime permissions still apply"
    except Exception:
        out["tool_count"] = 0
        out["tools"] = 0
        out["tools_status"] = "tool registry unavailable"

    try:
        from ..memory.store import get_memory

        stats = get_memory().stats()
        out["memory_stats"] = stats
        out["memory"] = (f"{stats.get('facts', 0)} facts, {stats.get('episodes', 0)} episode records, "
                         f"{stats.get('notes', 0)} notes, {stats.get('tasks', 0)} tasks")
    except Exception:
        out["memory"] = "unavailable"
        out["memory_stats"] = {}

    browser_pkg = S.has_browser_use()
    browser_cli = S.has_browser_use_cli()
    out["browser_cli"] = browser_cli
    if browser_pkg and browser_cli:
        out["browser"] = "browser-use API + CLI installed; not currently running"
    elif browser_pkg:
        out["browser"] = "browser-use API installed; not currently running"
    elif browser_cli:
        out["browser"] = "browser-use CLI installed; not currently running"
    else:
        out["browser"] = "not installed"

    try:
        from ..voice.livekit_agent import voice_status

        voice = voice_status()
        if voice.get("console_ready") and voice.get("cloud_ready"):
            out["livekit_status"] = "local console + cloud agent ready to start; no active connection checked"
        elif voice.get("console_ready"):
            out["livekit_status"] = "local console ready; cloud-room keys missing; no active connection checked"
        else:
            out["livekit_status"] = "not ready: " + "; ".join(voice.get("missing", [])[:2])
        out["livekit"] = bool(voice.get("console_ready"))
    except Exception:
        out["livekit"] = False
        out["livekit_status"] = "status unavailable"

    try:
        configured = bool(S.TELEGRAM_BOT_TOKEN)
        authorized = bool(S.TELEGRAM_OWNER_ID or S.TELEGRAM_ALLOWED_CHAT_IDS or S.TELEGRAM_ALLOWED_USERS)
        installed = S.has_telegram()
        out["telegram"] = configured and authorized and installed
        if not configured:
            out["telegram_status"] = "token not configured"
        elif not authorized:
            out["telegram_status"] = "authorization IDs missing; bot must stay closed"
        elif not installed:
            out["telegram_status"] = "token/allowlist configured; package not installed"
        else:
            out["telegram_status"] = "token + allowlist configured; polling state not checked"
    except Exception:
        out["telegram"] = False
        out["telegram_status"] = "status unavailable"

    try:
        from ..automation import scheduler as sch

        st = sch.status()
        out["automations"] = (f"{st['enabled']}/{st['total']} enabled; scheduler "
                              f"{'running' if st['running'] else 'not running'}")
    except Exception:
        out["automations"] = "status unavailable"
    return out


def agent_status_text(compact: bool = True, surface: str = "Telegram") -> str:
    """Markdown text — explicitly labels readiness, preference, and the current interface."""
    st = agent_status(compact=compact, surface=surface)
    lines = [
        f"*{st['assistant']}* — {st['status']}",
        f"• Brain: `{st['provider']}` → `{st['model']}`",
        f"• Fast role: `{st['fast_model']}`",
        f"• Provider status: {st.get('provider_status', 'unknown')}",
        f"• Interface now: `{surface}`  •  default preference: `{st['mode']}`",
        f"• Dangerous-Command Gate: `{st.get('gate_mode', 'unknown')}` ({st.get('gate_mode_source', 'unknown')})",
        f"• Tools: {st.get('tool_count', '?')} schemas registered (permissions apply per interface)",
        f"• Memory: {st['memory']}",
        f"• Browser: {st['browser']}",
        f"• LiveKit: {st['livekit_status']}",
        f"• Telegram: {st['telegram_status']}",
        f"• Automations: {st['automations']}",
    ]
    if st.get("available_providers"):
        lines.append(f"• Providers with keys: {', '.join(st['available_providers'])}")
    lines.append("• API/provider connectivity has not been tested by this status command.")
    return "\n".join(lines)


def voice_status_text() -> str:
    """Voice startup readiness; explicitly not an active LiveKit connection check."""
    from ..voice.livekit_agent import voice_status

    st = voice_status()
    lines = [
        "🎙️ *Voice readiness (not current connection state)*",
        f"• Ready to start: {'✅ yes' if st['ready'] else '❌ no'}",
        f"• Voice: `{st['voice']}`  •  realtime model: `{st['model']}`",
        "• Local `console` mode does not connect to a LiveKit room; `dev`/`start` do.",
    ]
    if st["missing"]:
        lines.append("• Missing:")
        lines += [f"   – {m}" for m in st["missing"]]
    else:
        lines.append("• Start explicitly: `python -m jarvis voice console` or `python -m jarvis voice dev`.")
    return "\n".join(lines)
