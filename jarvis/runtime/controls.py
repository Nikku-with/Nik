"""Shared, truthful preference controls for CLI, Telegram, and status surfaces."""
from __future__ import annotations

import os
from typing import Any

_GATE_MODES = frozenset({"off", "risky", "all"})


def gate_mode_details() -> dict[str, Any]:
    """Return the effective Gate mode and its source without making a provider/network call."""
    from ..config import runtime as R
    from ..config import settings as S
    from ..security.gate import approval_mode

    env_value = (os.getenv("JARVIS_EXEC_APPROVAL") or os.getenv("EXEC_APPROVAL") or "").strip()
    saved = R.load().get("exec_approval")
    if env_value:
        source = "environment override"
    elif str(saved or "").strip().lower() in _GATE_MODES:
        source = "saved runtime preference"
    elif str(getattr(S, "EXEC_APPROVAL", "risky") or "risky").strip().lower() != "risky":
        source = "configured default"
    else:
        source = "default"
    return {
        "mode": approval_mode(),
        "source": source,
        "environment_override": bool(env_value),
    }


def save_gate_mode(value: str) -> tuple[bool, str]:
    """Save a Gate preference unless an explicit environment value currently overrides it."""
    mode = str(value or "").strip().lower()
    if mode not in _GATE_MODES:
        return False, "Usage: off | risky | all"

    info = gate_mode_details()
    if info["environment_override"]:
        if info["mode"] == mode:
            return True, f"Gate is already {mode} from the environment; no runtime override was written."
        return False, (
            f"Environment configuration currently forces Gate mode '{info['mode']}'; "
            "the saved setting was not changed. Update JARVIS_EXEC_APPROVAL/EXEC_APPROVAL and restart."
        )

    from ..config import runtime as R
    from ..security.gate import approval_mode

    applied, errors = R.set_many(exec_approval=mode)
    if errors or applied.get("exec_approval") != mode:
        detail = "; ".join(errors) or "runtime did not confirm the requested value"
        return False, f"Gate mode save failed; effective mode remains {approval_mode()}: {detail}"
    effective = approval_mode()
    if effective != mode:
        return False, f"Gate mode preference saved as {mode}, but effective mode is {effective}; check environment configuration."
    return True, f"Dangerous-Command Gate preference saved; effective mode is {effective}."


def model_provider_status() -> dict[str, str | list[str]]:
    """Describe configured brain routing; this is not a live connectivity check."""
    from ..config import runtime as R
    from ..config import settings as S
    from ..llm import health

    try:
        result = health()  # health resolves routes with live discovery disabled
        status_error = ""
    except Exception as exc:
        result = {}
        status_error = f"(status unavailable: {type(exc).__name__})"
    return {
        "provider_preference": str(R.get("provider") or getattr(S, "BRAIN_PROVIDER_CONFIG", "auto") or "auto"),
        "model_preference": str(R.get("model") or S.explicit_model_for("brain") or "(provider default)"),
        "active_provider": str(result.get("active", "demo")),
        "brain_route": str((result.get("roles") or {}).get("brain", "")) or status_error or "(unavailable)",
        "available_providers": list(result.get("available") or []),
        "connectivity": "not tested",
    }


def save_model_preference(value: str) -> tuple[bool, str]:
    """Save a model preference and report whether a configured route can be resolved."""
    model = str(value or "").strip()
    if not model:
        return False, "Model name is required."

    from ..config import runtime as R
    from ..llm import reset_cache
    from ..llm.roles import resolve

    applied, errors = R.set_many(model=model)
    if errors or applied.get("model") != model:
        return False, "; ".join(errors) or "runtime did not confirm the model preference"
    try:
        reset_cache()
        route = resolve("brain", allow_discovery=False)
    except Exception as exc:
        return True, f"Model preference saved as '{model}', but route status is unavailable ({type(exc).__name__})."
    if not route.targets:
        return True, (
            f"Model preference saved as '{model}', but no brain route is currently available: "
            f"{route.reason or 'no configured provider/key'}. Connectivity was not tested."
        )
    return True, (
        f"Model preference saved as '{model}'. Configured brain route: {route.describe()}. "
        "Provider connectivity was not tested."
    )


def save_provider_preference(value: str) -> tuple[bool, str]:
    """Save a provider preference and report missing keys without implying a switch succeeded."""
    provider = str(value or "").strip().lower()
    if not provider:
        return False, "Provider name is required."

    from ..config import runtime as R
    from ..llm import health, reset_cache

    applied, errors = R.set_many(provider=provider)
    if errors or applied.get("provider") != provider:
        return False, "; ".join(errors) or "runtime did not confirm the provider preference"
    try:
        reset_cache()
        status = health()
    except Exception as exc:
        return True, f"Provider preference saved as '{provider}', but route status is unavailable ({type(exc).__name__})."
    active = str(status.get("active", "demo"))
    route = str((status.get("roles") or {}).get("brain", "(unavailable)"))
    if provider not in {"auto", active}:
        return True, (
            f"Provider preference saved as '{provider}', but its key is unavailable; no provider switch was made. "
            f"Current reported route: {route}. Connectivity was not tested."
        )
    if active == "demo":
        return True, (
            f"Provider preference saved as '{provider}'; no provider key is available, so deterministic demo mode remains. "
            "Connectivity was not tested."
        )
    return True, (
        f"Provider preference saved as '{provider}'. Active configured provider: {active}; brain route: {route}. "
        "Provider connectivity was not tested."
    )
