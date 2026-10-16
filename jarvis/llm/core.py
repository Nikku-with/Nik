"""
LLM core — LangChain chat models with multi-provider fallback
============================================================

    get_chat_model("brain")   → FallbackChatModel (OpenRouter → Gemini → Groq ...)
    get_chat_model("fast")    → sasta/tez model (router, extraction)
    init_dspy("fast")         → DSPy LM configure (wahi chain use karta hai)

`FallbackChatModel` ek LangChain `BaseChatModel` hai jo andar ek *chain* rakhta hai. Har call
par pehla model try hota hai; 404 / deprecated / quota / timeout par automatic agla. Isliye
agent code ko kabhi pata nahi chalta ki peeche konsa provider chal raha hai.
"""
from __future__ import annotations

import logging
import time
from typing import Any

_LOG = logging.getLogger("jarvis.llm")

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult

from ..config import PROVIDER, is_offline
from ..config import runtime as Runtime
from ..config import settings as S
from . import providers as P
from . import roles as R

_MODEL_CACHE: dict[tuple, Any] = {}


class ToolBindingError(RuntimeError):
    """Koi bhi model tools ko bind nahi kar paya — loud fail, silent degrade nahi.

    PEHLE KA BUG (user ne pakda): `bind_tools()` me `except Exception: bound.append(model)` tha.
    Matlab jis model par tools bind nahi hote, wo **bina tools** ke chain me reh jata tha. Phir
    ReAct agent / `with_structured_output` us model se poochta aur wo *tools ke bina* jawab de
    deta — bilkul chup-chaap. Agar primary model quota/404 se gir jata, fallback model bina
    tools ka "convincing" jawab de deta aur run `ok` dikhta. Ab:
      * bind fail hone wale models **chain se hata diye jate** hain (kabhi silent unbound nahi),
      * fail hone ka record `roles.note_attempt()` + progress warn me jaata hai (dikhta hai),
      * agar *saare* fail ho jayein → yahi error raise hota hai → caller honest "kaam fail hua" deta hai.

    Message ka core (spec): "No configured model successfully supports tool calling for this
    execution." — tool-required run me plain-text generation kabhi valid outcome nahi hai.
    """


def _emit_bind_warn(role: str, label: str, exc: Exception) -> None:
    """Progress bus par warn bhejo (CLI/Telegram ke live feed me dikhega) — best effort."""
    try:
        from ..runtime.progress import emit

        emit("warn", f"{role}: '{label}' tools bind nahi kar paya — skip kiya "
                     f"({type(exc).__name__})", tool=label, status="skipped",
             detail=str(exc)[:200])
    except Exception:  # noqa: BLE001 — progress kabhi main kaam na roke
        pass


def _emit_model_attempt(kind: str, role: str, label: str, status: str = "", ms: int = 0,
                         error_type: str = "") -> None:
    """Show actual provider attempts without exposing exception text or hidden reasoning."""
    try:
        from ..runtime.progress import emit

        if kind == "model_start":
            emit(kind, f"{role} model call → {label}", tool=label, status="running")
        elif status == "ok":
            emit(kind, f"{label} response received", tool=label, status="ok", ms=ms)
        else:
            emit(kind, f"{label} request failed ({error_type or 'provider error'})",
                 tool=label, status="fail", ms=ms)
    except Exception:  # noqa: BLE001
        pass


class LLMFactory:
    """Provider registry ke through langchain model banao (agent code provider ko touch nahi karta)."""

    @staticmethod
    def create(provider: str, model: str, temperature: float = 0.2, **kwargs):
        return P.build_model(provider, model, temperature, **kwargs)

    @staticmethod
    def available_providers() -> list[str]:
        return P.available()

    @staticmethod
    def supports(provider: str) -> bool:
        return P.is_available(provider)


class FallbackChatModel(BaseChatModel):
    """
    Ek role ke liye multiple models ki chain (provider-mixed).

    Kyun: providers ke model names badalte rehte hain, keys ke limits khatam ho jate hain,
    aur ek free model hamesha ke liye free nahi rehta. Chain hone se agent kabhi rukka nahi.
    """

    role: str = "brain"
    targets: list[str] = []          # ["openrouter:deepseek/...:free", "gemini:gemini-3.8-flash"]
    chain: list[Any] = []
    tools_bound: bool = False        # True = is chain ke sab models par tools bind ho chuke hain
    bind_failures: list[dict[str, Any]] = []   # [{"target": "...", "error": "..."}] — dikhane ke liye

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return f"nikhilai-fallback({self.role})"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"role": self.role, "models": len(self.chain), "targets": self.targets}

    def bind_tools(self, tools, **kwargs):  # type: ignore[override]
        """Saare chain models par tools bind karo.

        Rules (silent degradation band):
          * bind fail → us model ko chain se **hata do** (kabhi bhi unbound model use na ho),
            fail ki wajah record karo (roles.note_attempt + progress warn),
          * kuch bind ho gaye → aage badho, par `bind_failures` me dikhao ki kaun chhoot gaya,
          * koi bhi nahi bind hua → `ToolBindingError` raise karo (chup-chaap tools ke bina
            jawab dene se behtar hai saaf failure).

        NOTE: `with_structured_output()` bhi isi method par aata hai (LangChain default usko
        bind_tools se banata hai) — isliye ye fix dono paths ko cover karta hai.
        """
        if not tools:
            return self.model_copy(update={"tools_bound": True})

        # ⛔ SILENT-FALLBACK BAN — yahan `bound.append(model)` (bina bind) kabhi nahi hona chahiye.
        # Har model ya to successfully bind hokar chain me aata hai, ya chain se bahar reh jata hai.
        # Ye rule tests/test_tool_invariant.py me source-level bhi pinned hai.
        bound: list[Any] = []
        labels: list[str] = []
        failures: list[dict[str, Any]] = []
        for idx, model in enumerate(self.chain):
            label = self.targets[idx] if idx < len(self.targets) else f"#{idx}"
            try:
                bound_model = model.bind_tools(tools, **kwargs)
            except Exception as exc:  # noqa: BLE001 — per-model degrade, par *dikh kar* (silent nahi)
                provider, _, model_name = label.partition(":")
                msg = f"bind_tools failed: {type(exc).__name__}: {exc}"
                _LOG.warning("tool-binding FAILED role=%s model=%s tools=%d error=%s: %s",
                             self.role, label, len(tools), type(exc).__name__, exc)
                try:
                    R.note_attempt(self.role, provider, model_name, False, msg)
                except Exception:  # noqa: BLE001
                    pass
                failures.append({"target": label, "error": msg[:300]})
                _emit_bind_warn(self.role, label, exc)
                continue                      # ← chain se HATA diya: unbound model use nahi hoga
            # bind hua — tab hi chain me shamil
            bound.append(bound_model)
            labels.append(label)
            _LOG.info("tool-binding OK role=%s model=%s tools=%d impl=%s",
                      self.role, label, len(tools), type(bound_model).__name__)

        # invariant: har chain member ya bound hua ya failure me gaya (koi chupchup silently drop na ho)
        if len(bound) + len(failures) != len(self.chain):
            raise RuntimeError(
                f"bind_tools invariant toota ({self.role}): chain={len(self.chain)} "
                f"bound={len(bound)} failed={len(failures)} — ye code bug hai, silent fallback nahi"
            )

        _LOG.info("tool-binding summary role=%s tools=%d bound=%d failed=%d targets=%s",
                  self.role, len(tools), len(bound), len(failures), labels)

        if not bound:
            details = "; ".join(f"{f['target']} → {f['error']}" for f in failures) or "(chain khali)"
            raise ToolBindingError(
                "No configured model successfully supports tool calling for this execution. "
                f"(role={self.role}, tools={len(tools)}) — details: {details}. "
                "Bina tools ke jawab dena silent galat hoga — isliye yahan ruk rahe hain. "
                "Fix: provider ka tool-calling support check karo, `python -m jarvis doctor`, "
                "ya `/model` se doosra model chuno."
            )

        return self.model_copy(update={"chain": bound, "targets": labels,
                                       "bind_failures": failures, "tools_bound": True})

    @property
    def bound_model_count(self) -> int:
        """Kitne models tools ke saath successfully bound hain (logging/report ke liye)."""
        return len(self.chain) if self.tools_bound else 0

    def binding_report(self) -> dict[str, Any]:
        """Observability: kitne models tools ke saath chalu hain, kaun chhoot gaya."""
        return {"role": self.role, "tools_bound": self.tools_bound,
                "bound_models": self.bound_model_count,
                "active": len(self.chain), "targets": list(self.targets),
                "failed": list(self.bind_failures)}

    # ---------------------------------------------------------------- sync
    def _call_one(self, model: Any, messages, stop, run_manager, **kwargs) -> ChatResult:
        if hasattr(model, "_generate"):
            return model._generate(messages, stop, run_manager, **kwargs)
        result = model.invoke(messages)
        return ChatResult(generations=[ChatGeneration(message=result)])

    def _skipped_targets(self) -> list[str]:
        """Targets still inside their cooldown window — these are never retried."""
        cooled: list[str] = []
        for _idx, label in enumerate(self.targets):
            provider, _, model_name = label.partition(":")
            if R.is_cooled_down(provider, model_name):
                cooled.append(label)
        return cooled

    def _aggregate_failure(self, last_err: Exception | None, failed: list[str],
                           skipped: list[str], attempted: int) -> RuntimeError:
        """One honest sentence naming every target that did not answer."""
        if not attempted and skipped:
            return RuntimeError(
                f"saare models cooldown me hain ({self.role}): {'; '.join(skipped)}. "
                "Cooldown khatam hone ke baad dobara try karein.")
        detail = "; ".join(failed) or "(koi target nahi bacha)"
        skipped_detail = f" | skip: {'; '.join(skipped)}" if skipped else ""
        return RuntimeError(
            f"saare models fail ({self.role}): {detail}{skipped_detail}. "
            f"aakhri error: {last_err}. "
            "Jo bhi model fail hua, wo ab circuit-breaker cooldown me hai — "
            "cooldown khatam hone tak dobara try nahi hoga.")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        last_err: Exception | None = None
        attempted = 0
        skipped: list[str] = []
        failed: list[str] = []
        for idx, model in enumerate(self.chain):
            label = self.targets[idx] if idx < len(self.targets) else f"#{idx}"
            provider, _, model_name = label.partition(":")
            remaining = R.cooldown_remaining(provider, model_name)
            if remaining > 0:
                skipped.append(f"{label} ({remaining:.0f}s cooldown)")
                _emit_model_attempt("model_skip", self.role, label, status="cooldown")
                continue
            started = time.time()
            attempted += 1
            _emit_model_attempt("model_start", self.role, label)
            try:
                out = self._call_one(model, messages, stop, run_manager, **kwargs)
                elapsed = int((time.time() - started) * 1000)
                R.clear_cooldown(provider, model_name)
                R.note_attempt(self.role, provider, model_name, True, ms=elapsed)
                _emit_model_attempt("model_end", self.role, label, status="ok", ms=elapsed)
                return out
            except Exception as exc:
                last_err = exc
                elapsed = int((time.time() - started) * 1000)
                R.mark_cooldown(provider, model_name, exc)
                failed.append(f"{label} → {type(exc).__name__}: {str(exc)[:120]}")
                R.note_attempt(self.role, provider, model_name, False, str(exc), elapsed)
                _emit_model_attempt("model_end", self.role, label, status="fail", ms=elapsed,
                                    error_type=type(exc).__name__)
                continue
        raise self._aggregate_failure(last_err, failed, skipped, attempted)

    # --------------------------------------------------------------- async
    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        last_err: Exception | None = None
        attempted = 0
        skipped: list[str] = []
        failed: list[str] = []
        for idx, model in enumerate(self.chain):
            label = self.targets[idx] if idx < len(self.targets) else f"#{idx}"
            provider, _, model_name = label.partition(":")
            remaining = R.cooldown_remaining(provider, model_name)
            if remaining > 0:
                # A cooled-down model is skipped, not retried: retrying a 429 target on every
                # turn is what turned a chat reply into a multi-call, multi-second request.
                skipped.append(f"{label} ({remaining:.0f}s cooldown)")
                _emit_model_attempt("model_skip", self.role, label, status="cooldown")
                continue
            started = time.time()
            attempted += 1
            _emit_model_attempt("model_start", self.role, label)
            try:
                if hasattr(model, "_agenerate"):
                    out = await model._agenerate(messages, stop, run_manager, **kwargs)
                else:
                    result = await model.ainvoke(messages)
                    out = ChatResult(generations=[ChatGeneration(message=result)])
                elapsed = int((time.time() - started) * 1000)
                R.clear_cooldown(provider, model_name)
                R.note_attempt(self.role, provider, model_name, True, ms=elapsed)
                _emit_model_attempt("model_end", self.role, label, status="ok", ms=elapsed)
                return out
            except Exception as exc:
                last_err = exc
                elapsed = int((time.time() - started) * 1000)
                R.mark_cooldown(provider, model_name, exc)
                R.note_attempt(self.role, provider, model_name, False, str(exc), elapsed)
                _emit_model_attempt("model_end", self.role, label, status="fail", ms=elapsed,
                                    error_type=type(exc).__name__)
                failed.append(f"{label} → {type(exc).__name__}: {str(exc)[:120]}")
                continue
        raise self._aggregate_failure(last_err, failed, skipped, attempted)


def build_chain(role: str, allow_discovery: bool = True) -> tuple[list[Any], list[str]]:
    """Role ke liye asli model instances + labels banao (jo build na ho, skip ho jaye)."""
    route = R.resolve(role, allow_discovery=allow_discovery)
    models: list[Any] = []
    labels: list[str] = []
    timeout = S.FAST_LLM_TIMEOUT_S if role in {"fast", "extract", "critic"} else S.LLM_TIMEOUT_S
    for target in route.targets:
        key = (target.provider, target.model, round(route.temperature, 2), timeout, S.LLM_RETRIES)
        try:
            model = _MODEL_CACHE.get(key)
            if model is None:
                model = LLMFactory.create(target.provider, target.model, route.temperature,
                                          timeout=timeout, retries=S.LLM_RETRIES)
                _MODEL_CACHE[key] = model
            models.append(model)
            labels.append(str(target))
        except Exception as exc:
            R.note_attempt(role, target.provider, target.model, False, f"build failed: {exc}")
            continue
    return models, labels


def get_chat_model(role: str = "brain", **kwargs) -> BaseChatModel:
    """Resolve the configured route; intentional demo mode is distinct from a missing chosen key."""
    runtime_choice = P.canonical(str(Runtime.get("provider") or "auto"))
    env_choice = P.canonical(getattr(S, "BRAIN_PROVIDER_CONFIG", "auto"))
    configured_choice = runtime_choice if runtime_choice != "auto" else env_choice
    if configured_choice not in {"auto", "demo"} and not P.is_available(configured_choice):
        route = R.resolve(role, allow_discovery=False)
        message = route.reason or f"configured provider '{configured_choice}' has no key"
        try:
            from ..runtime.progress import emit

            emit("error", message, status="fail")
        except Exception:
            pass
        raise RuntimeError(message)
    if is_offline() or configured_choice == "demo" or PROVIDER == "demo" or not P.available():
        from .demo import DemoChatModel

        return DemoChatModel(role=role)

    cache_key = ("role", role)
    if cache_key not in _MODEL_CACHE:
        models, labels = build_chain(role)
        if not models:
            route = R.resolve(role, allow_discovery=False)
            detail = route.reason or "no configured model could be built"
            message = f"Live model route unavailable for role '{role}': {detail}"
            _LOG.error(message)
            try:
                R.note_attempt(role, route.primary.provider if route.primary else PROVIDER,
                               route.primary.model if route.primary else "(unavailable)", False, detail)
            except Exception:  # noqa: BLE001
                pass
            try:
                from ..runtime.progress import emit

                emit("error", message, status="fail")
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(message)
        _MODEL_CACHE[cache_key] = FallbackChatModel(role=role, chain=models, targets=labels)
    return _MODEL_CACHE[cache_key]


def reset_cache() -> None:
    """Settings/reload ke baad cache clear — naye models pick ho jayein."""
    global _dspy_configured
    _MODEL_CACHE.clear()
    if "_DSPY_LMS" in globals():
        with _DSPY_LOCK:
            _DSPY_LMS.clear()
            _dspy_configured = False
    try:  # session state / graph bhi naye model use karein
        from ..graph import build as _build  # noqa: F401

        _build.reset_graph()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# DSPy bridge
# ---------------------------------------------------------------------------
_dspy_configured = False
_DSPY_LMS: dict[tuple, Any] = {}
_DSPY_LOCK = __import__("threading").RLock()


def init_dspy(role: str = "fast", force: bool = False):
    """Create the role-specific DSPy LM from the resolved provider/model route."""
    global _dspy_configured
    import dspy  # type: ignore

    route = R.resolve(role)
    if not route.targets:
        raise RuntimeError(f"DSPy {role}: {route.reason or 'no configured model/key'}")

    # DSPy must never be handed a cooled-down model: it retries internally where our
    # circuit breaker cannot see the individual attempts, so a 429 target would be hit
    # again and again inside one module call.
    primary = next((target for target in route.targets
                    if not R.is_cooled_down(target.provider, target.model)), None)
    if primary is None:
        cooled = ", ".join(f"{t.provider}:{t.model}" for t in route.targets)
        raise RuntimeError(
            f"DSPy {role}: sabhi targets cooldown me hain ({cooled}). "
            "Circuit breaker ne inhe block kiya hai; cooldown khatam hone par dobara try karein.")
    model_id = f"{primary.provider}/{primary.model}"
    timeout = S.FAST_LLM_TIMEOUT_S if role in {"fast", "extract", "critic"} else S.LLM_TIMEOUT_S
    max_tokens = S.DSPY_BRAIN_MAX_TOKENS if role == "brain" else S.DSPY_FAST_MAX_TOKENS
    key = (role, model_id, round(route.temperature, 3), timeout, max_tokens, S.DSPY_NUM_RETRIES)
    with _DSPY_LOCK:
        lm = _DSPY_LMS.get(key)
        if lm is None:
            lm = dspy.LM(model_id, temperature=route.temperature, max_tokens=max_tokens, cache=True,
                         num_retries=S.DSPY_NUM_RETRIES, timeout=timeout)
            _DSPY_LMS[key] = lm
        if force or not _dspy_configured:
            dspy.configure(lm=lm)
            _dspy_configured = True
    return dspy, lm


def dspy_role_context(role: str = "fast"):
    """Use this role's LM for one DSPy call without changing another role's module."""
    dspy, lm = init_dspy(role)
    return dspy.context(lm=lm)
