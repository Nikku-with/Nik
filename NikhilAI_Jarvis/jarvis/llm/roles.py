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
    """Runtime override → env → auto-detect (live re-check, taaki naya key turant pakde)."""
    override = str(R.get("provider") or "").strip().lower()
    forced = P.canonical(override) if override and override != "auto" else ""
    if forced and forced != "demo":
        if P.is_available(forced):
            return forced
        return S.autodetect_provider()      # key nahi hai to chalne wale provider par girao
    # 'demo' ya auto: import-time detection purani ho sakti hai (user ne abhi key daali ho) —
    # isliye live dobara check karo.
    live = S._auto_provider() if hasattr(S, "_auto_provider") else S.autodetect_provider()
    if live != "demo":
        return live
    return S.autodetect_provider()


def _retired(model: str) -> bool:
    low = model.lower()
    return any(marker in low for marker in _RETIRED_MARKERS)


def _valid_for_provider(model: str, provider: str) -> bool:
    """Model naam us provider ka lagta hai? (galti se doosre provider ka naam na chala jaye)"""
    if not model:
        return False
    if _retired(model):
        return False
    low = model.lower()
    if provider == "openrouter":
        return "/" in low or low.endswith(":free") or low in {"auto"}
    if provider == "groq":
        return "/" not in low or "-" in low
    if provider == "gemini":
        return low.startswith("gemini") or low.startswith("gemma")
    return True


def cross_provider_chain(role: str) -> list[ModelTarget]:
    """Agar primary provider ke saare models fail ho jayein to doosre providers try karo."""
    out: list[ModelTarget] = []
    for provider in _CROSS_ORDER:
        if not P.is_available(provider):
            continue
        chain = S.model_chain_for(role, provider)
        for model in chain[:2]:
            out.append(ModelTarget(provider, model))
    return out


def resolve(role: str, allow_discovery: bool = True) -> ResolvedRoute:
    """Role ke liye pura fallback chain resolve karo (offline me khali route)."""
    temperature = R.get("temperature") or _ROLE_TEMP.get(role, 0.2)
    provider = active_provider()
    if provider == "demo":
        # koi key nahi — par UI/doctor me dikhado ki key aane par kya chalega
        preview = S.model_chain_for(role, "gemini")
        targets = [ModelTarget("gemini", preview[0])] if preview else []
        return ResolvedRoute(role, targets, temperature,
                             reason="offline/demo mode (key aane par gemini chalega)")

    targets: list[ModelTarget] = []
    reason_bits: list[str] = []

    # 1) runtime/env override (sirf brain role ke liye global 'model')
    if role == "brain":
        override = str(R.get("model") or "").strip()
        if override and _valid_for_provider(override, provider):
            targets.append(ModelTarget(provider, override))
            reason_bits.append("runtime override")

    # 2) provider ke configured defaults + fallbacks
    for model in S.model_chain_for(role, provider):
        if _valid_for_provider(model, provider):
            targets.append(ModelTarget(provider, model))
    if targets and "runtime override" not in reason_bits:
        reason_bits.append(f"{provider} defaults")

    # 3) live discovery — sabse fit free model
    if allow_discovery and not targets:
        best = D.best_free_model(provider, role)
        if best:
            targets.append(ModelTarget(provider, best))
            reason_bits.append("live discovery (free model)")

    # 4) cross-provider fallback
    seen = {str(t) for t in targets}
    for target in cross_provider_chain(role):
        if str(target) not in seen:
            targets.append(target)
            seen.add(str(target))
    if len(targets) > 1:
        reason_bits.append("cross-provider fallback")

    # dedupe (order preserve)
    ordered: list[ModelTarget] = []
    for t in targets:
        if not any(t.provider == o.provider and t.model == o.model for o in ordered):
            ordered.append(t)

    return ResolvedRoute(role, ordered[:6], temperature, reason=", ".join(reason_bits) or "none")


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
