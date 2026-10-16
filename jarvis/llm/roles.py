"""
Role-based model router + fallback manager
=========================================

Har role (brain / fast / planner / code / research / extract / vision / browser / critic) ke liye
ek **chain** banti hai:

    1. Runtime settings / env override        (sabse pehle — owner ne jo bola)
    2. Provider ke configured defaults        (free models pehle)
    3. Live discovery se best free model      (agar kuch bhi available na ho)
    4. Doosre providers ka cross-fallback     (brain: OpenRouter → Gemini → Groq)

Isliye ek model 404 de ya quota khatam ho, poora agent rukta nahi — agla model chalta hai.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from ..config import runtime as R
from ..config import settings as S
from . import discovery as D
from . import providers as P

_HISTORY: list[dict[str, Any]] = []          # provider/model attempts (observability)
_LOCK = threading.RLock()
_CROSS_ORDER = ("openrouter", "gemini", "groq", "openai", "anthropic", "xai", "ollama")
_RETIRED_MARKERS = ("2.0-flash", "gemini-2.0", "text-bison", "gpt-3.5-turbo-0301")
_ROLE_TEMP = {"brain": 0.3, "fast": 0.0, "planner": 0.2, "code": 0.1, "research": 0.2,
              "extract": 0.0, "vision": 0.1, "browser": 0.1, "critic": 0.0}


@dataclass
class ModelTarget:
    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass
class ResolvedRoute:
    role: str
    targets: list[ModelTarget] = field(default_factory=list)
    temperature: float = 0.2
    reason: str = ""

    @property
    def primary(self) -> ModelTarget | None:
        return self.targets[0] if self.targets else None

    def describe(self) -> str:
        if not self.targets:
            return "(no model — offline demo mode)"
        head = str(self.targets[0])
        if len(self.targets) > 1:
            return f"{head}  (+{len(self.targets) - 1} fallback)"
        return head

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "targets": [str(t) for t in self.targets],
                "temperature": self.temperature, "reason": self.reason}


def active_provider() -> str:
    """Configured provider first; explicit choices never silently fall through to another brain."""
    override = str(R.get("provider") or "").strip().lower()
    configured = P.canonical(override) if override and override != "auto" else ""
    if not configured:
        env_choice = P.canonical(getattr(S, "BRAIN_PROVIDER_CONFIG", "auto"))
        configured = env_choice if env_choice != "auto" else ""
    if configured:
        # A missing key is reported as demo/unavailable, not replaced by another configured key.
        return configured if P.is_available(configured) else "demo"
    live = S._auto_provider() if hasattr(S, "_auto_provider") else S.autodetect_provider()
    return live if live != "demo" and P.is_available(live) else "demo"


def _retired(model: str) -> bool:
    low = model.lower()
    return any(marker in low for marker in _RETIRED_MARKERS)


def _valid_for_provider(model: str, provider: str) -> bool:
    """Reject retired and obviously cross-provider model IDs before constructing a client."""
    if not model or _retired(model):
        return False
    low = model.strip().lower()
    provider = P.canonical(provider)
    if provider == "openrouter":
        return "/" in low or low == "auto"
    if provider == "gemini":
        return low.startswith(("gemini", "gemma"))
    if provider == "anthropic":
        return low.startswith("claude-")
    if provider == "xai":
        return low.startswith("grok-")
    if provider == "openai":
        return low.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-"))
    if provider == "groq":
        # Groq also hosts namespaced openai/gpt-oss, qwen, and other vendor models.
        # Bare model IDs owned by Gemini/Anthropic/xAI/OpenAI are not Groq routes.
        if "/" not in low and low.startswith(("gemini", "gemma", "claude-", "grok-", "gpt-", "o1", "o3", "o4")):
            return False
        return True
    return True


def cross_provider_chain(role: str) -> list[ModelTarget]:
    """Use each fallback provider's own defaults; never pair one provider with another's model ID."""
    out: list[ModelTarget] = []
    for provider in _CROSS_ORDER:
        if not P.is_available(provider):
            continue
        for model in S.provider_model_defaults(role, provider)[:2]:
            if _valid_for_provider(model, provider):
                out.append(ModelTarget(provider, model))
    return out


def resolve(role: str, allow_discovery: bool = True) -> ResolvedRoute:
    """Role ke liye pura fallback chain resolve karo (offline me khali route)."""
    configured_temperature = R.get("temperature")
    temperature = (_ROLE_TEMP.get(role, 0.2) if configured_temperature is None
                   else float(configured_temperature))
    runtime_choice = P.canonical(str(R.get("provider") or "auto"))
    env_choice = P.canonical(getattr(S, "BRAIN_PROVIDER_CONFIG", "auto"))
    explicit_provider = runtime_choice != "auto" or env_choice != "auto"
    provider = active_provider()
    if provider == "demo":
        chosen = runtime_choice if runtime_choice != "auto" else env_choice
        if chosen != "auto" and not P.is_available(chosen):
            reason = f"configured provider '{chosen}' has no key; no provider switch performed"
        else:
            reason = "offline/demo mode; no configured provider key"
        return ResolvedRoute(role, [], temperature, reason=reason)

    reason_bits: list[str] = []
    local: list[ModelTarget] = []
    runtime_model = str(R.get("model") or "").strip() if role == "brain" else ""
    env_model = S.explicit_model_for(role)
    requested_model = runtime_model or env_model
    if requested_model and not _valid_for_provider(requested_model, provider):
        source = "runtime model override" if runtime_model else f"{role.upper()}_MODEL"
        return ResolvedRoute(
            role, [], temperature,
            reason=f"{source} '{requested_model}' is retired or incompatible with configured provider '{provider}'; no substitute selected",
        )

    if runtime_model:
        local.append(ModelTarget(provider, runtime_model))
        reason_bits.append("runtime override")

    for model in S.model_chain_for(role, provider):
        if _valid_for_provider(model, provider):
            local.append(ModelTarget(provider, model))
    if local and "runtime override" not in reason_bits:
        reason_bits.append(f"{provider} configured route")

    # Live discovery only fills an empty configured route; it never replaces a chosen model.
    if allow_discovery and not local:
        best = D.best_free_model(provider, role)
        if best:
            local.append(ModelTarget(provider, best))
            reason_bits.append("live discovery")

    def _unique(items: list[ModelTarget]) -> list[ModelTarget]:
        out: list[ModelTarget] = []
        seen: set[tuple[str, str]] = set()
        for target in items:
            key = (target.provider, target.model)
            if key not in seen:
                out.append(target)
                seen.add(key)
        return out

    local = _unique(local)
    if not local:
        return ResolvedRoute(role, [], temperature, reason=", ".join(reason_bits) or "no model route")

    # Keep the preferred model first, one same-provider fallback second, then spread the
    # remaining bounded attempts across other configured providers. This avoids spending
    # a whole request budget on several stale models from one provider.
    # Cross-provider fallback is an auto-mode convenience; explicit owner choices stay pinned.
    cross = [] if explicit_provider else _unique(
        [target for target in cross_provider_chain(role) if target.provider != provider]
    )
    ordered = [local[0]]
    if len(local) > 1:
        ordered.append(local[1])
    ordered.extend(cross)
    ordered.extend(local[2:])
    ordered = _unique(ordered)[:S.MAX_MODEL_TARGETS]
    if cross:
        reason_bits.append("cross-provider fallback")
    return ResolvedRoute(role, ordered, temperature, reason=", ".join(reason_bits) or "configured")


# ---------------------------------------------------------------------------
# per-target cooldown (circuit breaker)
# ---------------------------------------------------------------------------
# Kyun: bina iske `FallbackChatModel` har request par wahi exhausted/quota-expired model
# dobara try karta tha — 429 ke baad bhi pehla target pehle call hota tha, isliye har turn
# me extra failed calls aur seconds barbad hote the. Ab ek failing target apni cooldown
# window tak chain se bahar rehta hai, aur success par turant wapas aa jata hai.
#
# Scope note: ye *model health* ka circuit breaker hai (429/ quota / timeout / 5xx).
# Baat-cheet me follow-up sawaal rokne wala "recent-question cooldown" alag cheez hai
# (wo `runtime/agent.py::_chat_fast_path` me hai).
COOLDOWN_DEFAULTS: dict[str, float] = {
    "rate_limited": 60.0,      # 429 / ResourceExhausted / quota
    "timeout": 20.0,
    "server_error": 30.0,      # 5xx
    "auth_error": 300.0,       # 401/403 — retry kar ke fayda nahi
    "unknown": 15.0,
}
_RATE_LIMIT_MARKERS = (
    "429", "resource exhausted", "resourceexhausted", "quota", "rate limit", "rate-limit",
    "too many requests", "ratelimit", "exceeded your current quota", "requests per minute",
)
_AUTH_MARKERS = ("401", "403", "unauthorized", "forbidden", "invalid api key", "permission denied",
                 "authentication")
_TIMEOUT_MARKERS = ("timeout", "timed out", "deadline exceeded", "timeouterror")
_SERVER_MARKERS = ("500", "502", "503", "504", "internal server error", "bad gateway",
                   "service unavailable", "server error", "overloaded")

_COOLDOWNS: dict[tuple[str, str], dict[str, Any]] = {}
_COOLDOWN_ORDER: list[tuple[str, str]] = []
_COOLDOWN_MAX_TARGETS = 512


def _now() -> float:
    import time

    return time.monotonic()


def cooldown_class(error: str | BaseException | None) -> str:
    """Map a provider error to a cooldown class (never raises, never returns None)."""
    text = str(error or "").lower()
    if not text:
        return "unknown"
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return "rate_limited"
    if any(marker in text for marker in _AUTH_MARKERS):
        return "auth_error"
    if any(marker in text for marker in _TIMEOUT_MARKERS):
        return "timeout"
    if any(marker in text for marker in _SERVER_MARKERS):
        return "server_error"
    return "unknown"


def target_key(provider: str, model: str) -> tuple[str, str]:
    return (str(provider or "").strip().lower(), str(model or "").strip().lower())


def _prune_locked(now: float) -> None:
    for key in list(_COOLDOWNS):
        entry = _COOLDOWNS.get(key)
        if entry and float(entry.get("until", 0.0)) <= now:
            _COOLDOWNS.pop(key, None)


def mark_cooldown(provider: str, model: str, error: str | BaseException | None = "",
                  kind: str | None = None, seconds: float | None = None) -> dict[str, Any]:
    """Put one (provider, model) target in the penalty box for its cooldown window."""
    kind = kind or cooldown_class(error)
    window = float(COOLDOWN_DEFAULTS.get(kind, COOLDOWN_DEFAULTS["unknown"])) if seconds is None \
        else float(seconds)
    window = max(0.0, window)
    now = _now()
    with _LOCK:
        _prune_locked(now)
        key = target_key(provider, model)
        previous = _COOLDOWNS.get(key)
        # repeated failures back off instead of resetting to the same short window
        strikes = int(previous.get("strikes", 0)) + 1 if previous else 1
        effective = min(window * (2 ** min(strikes - 1, 3)), 900.0)
        entry = {
            "provider": key[0], "model": key[1], "kind": kind, "strikes": strikes,
            "window": effective, "started": now, "until": now + effective,
            "reason": str(error or "")[:240],
        }
        _COOLDOWNS[key] = entry
        if key in _COOLDOWN_ORDER:
            _COOLDOWN_ORDER.remove(key)
        _COOLDOWN_ORDER.append(key)
        while len(_COOLDOWN_ORDER) > _COOLDOWN_MAX_TARGETS:
            oldest = _COOLDOWN_ORDER.pop(0)
            _COOLDOWNS.pop(oldest, None)
        return dict(entry)


def clear_cooldown(provider: str, model: str) -> bool:
    """A successful call forgives the target immediately."""
    with _LOCK:
        return _COOLDOWNS.pop(target_key(provider, model), None) is not None


def cooldown_remaining(provider: str, model: str) -> float:
    """Seconds left in the cooldown; 0.0 when the target is callable."""
    now = _now()
    with _LOCK:
        entry = _COOLDOWNS.get(target_key(provider, model))
        if not entry:
            return 0.0
        remaining = float(entry.get("until", 0.0)) - now
        if remaining <= 0:
            _COOLDOWNS.pop(target_key(provider, model), None)
            return 0.0
        return remaining


def is_cooled_down(provider: str, model: str) -> bool:
    return cooldown_remaining(provider, model) > 0.0


def cooled_down_targets() -> list[dict[str, Any]]:
    now = _now()
    with _LOCK:
        _prune_locked(now)
        return [dict(entry) | {"remaining_s": round(float(entry.get("until", 0.0)) - now, 1)}
                for entry in _COOLDOWNS.values()]


def reset_cooldowns() -> None:
    with _LOCK:
        _COOLDOWNS.clear()
        _COOLDOWN_ORDER.clear()


# ---------------------------------------------------------------------------
# observability
# ---------------------------------------------------------------------------
def note_attempt(role: str, provider: str, model: str, ok: bool, error: str = "", ms: int = 0) -> None:
    with _LOCK:
        _HISTORY.append({
            "ts": __import__("time").strftime("%H:%M:%S"),
            "role": role, "provider": provider, "model": model,
            "ok": ok, "error": (error or "")[:200], "ms": ms,
        })
        if len(_HISTORY) > 200:
            del _HISTORY[:-200]


def attempts(limit: int = 20) -> list[dict[str, Any]]:
    with _LOCK:
        return list(_HISTORY[-limit:])


def health() -> dict[str, Any]:
    """Provider health — doctor ke liye."""
    out: dict[str, Any] = {"active": active_provider(), "available": P.available(), "roles": {}}
    for role in S.MODEL_ROLES:
        route = resolve(role, allow_discovery=False)
        out["roles"][role] = route.describe()
    return out
