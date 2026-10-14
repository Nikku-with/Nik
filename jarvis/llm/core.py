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

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ..config import OFFLINE_MODE, PROVIDER
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

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        last_err: Exception | None = None
        for idx, model in enumerate(self.chain):
            label = self.targets[idx] if idx < len(self.targets) else f"#{idx}"
            started = time.time()
            try:
                out = self._call_one(model, messages, stop, run_manager, **kwargs)
                provider, _, model_name = label.partition(":")
                R.note_attempt(self.role, provider, model_name, True, ms=int((time.time() - started) * 1000))
                return out
            except Exception as exc:
                last_err = exc
                provider, _, model_name = label.partition(":")
                R.note_attempt(self.role, provider, model_name, False, str(exc),
                               int((time.time() - started) * 1000))
                continue
        raise RuntimeError(f"saare models fail ({self.role}): {last_err}")

    # --------------------------------------------------------------- async
    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        last_err: Exception | None = None
        for idx, model in enumerate(self.chain):
            label = self.targets[idx] if idx < len(self.targets) else f"#{idx}"
            started = time.time()
            try:
                if hasattr(model, "_agenerate"):
                    out = await model._agenerate(messages, stop, run_manager, **kwargs)
                else:
                    result = await model.ainvoke(messages)
                    out = ChatResult(generations=[ChatGeneration(message=result)])
                provider, _, model_name = label.partition(":")
                R.note_attempt(self.role, provider, model_name, True, ms=int((time.time() - started) * 1000))
                return out
            except Exception as exc:
                last_err = exc
                provider, _, model_name = label.partition(":")
                R.note_attempt(self.role, provider, model_name, False, str(exc),
                               int((time.time() - started) * 1000))
                continue
        raise RuntimeError(f"saare models fail ({self.role}): {last_err}")


def build_chain(role: str, allow_discovery: bool = True) -> tuple[list[Any], list[str]]:
    """Role ke liye asli model instances + labels banao (jo build na ho, skip ho jaye)."""
    route = R.resolve(role, allow_discovery=allow_discovery)
    models: list[Any] = []
    labels: list[str] = []
    for target in route.targets:
        key = (target.provider, target.model, round(route.temperature, 2))
        try:
            model = _MODEL_CACHE.get(key)
            if model is None:
                model = LLMFactory.create(target.provider, target.model, route.temperature)
                _MODEL_CACHE[key] = model
            models.append(model)
            labels.append(str(target))
        except Exception as exc:
            R.note_attempt(role, target.provider, target.model, False, f"build failed: {exc}")
            continue
    return models, labels


def get_chat_model(role: str = "brain", **kwargs) -> BaseChatModel:
    """Role ka chat model lo — key nahi hai to offline demo brain."""
    if OFFLINE_MODE or PROVIDER == "demo" or not P.available():
        from .demo import DemoChatModel

        return DemoChatModel(role=role)

    cache_key = ("role", role)
    if cache_key not in _MODEL_CACHE:
        models, labels = build_chain(role)
        if not models:
            # Live mode me bhi koi provider build na hua → demo brain deterministic replies deta hai.
            # Ye "silent" nahi hona chahiye: log + attempts + progress warn, taaki user ko pata chale
            # ki asli model nahi chal raha (fake success ka ek aur darwaza band).
            _LOG.error("koi provider model build nahi hua (role=%s) — demo brain par gir rahe hain", role)
            try:
                R.note_attempt(role, PROVIDER, "(demo)", False,
                               "koi provider build nahi hua — demo brain fallback")
            except Exception:  # noqa: BLE001
                pass
            try:
                from ..runtime.progress import emit

                emit("warn", f"{role}: koi provider model build nahi hua — demo brain chal raha hai "
                             f"(asli jawab nahi milega)", status="fail")
            except Exception:  # noqa: BLE001
                pass
            from .demo import DemoChatModel

            return DemoChatModel(role=role)
        _MODEL_CACHE[cache_key] = FallbackChatModel(role=role, chain=models, targets=labels)
    return _MODEL_CACHE[cache_key]


def reset_cache() -> None:
    """Settings/reload ke baad cache clear — naye models pick ho jayein."""
    _MODEL_CACHE.clear()
    try:  # session state / graph bhi naye model use karein
        from ..graph import build as _build  # noqa: F401

        _build.reset_graph()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# DSPy bridge
# ---------------------------------------------------------------------------
_dspy_configured = False


def init_dspy(role: str = "fast", force: bool = False):
    """DSPy ka LM configure karo (wahi provider chain, cache=True se sasta)."""
    global _dspy_configured
    import dspy  # type: ignore

    route = R.resolve(role)
    if not route.targets:
        raise RuntimeError("koi provider key nahi — DSPy offline mode me nahi chalta")

    primary = route.targets[0]
    model_id = f"{primary.provider}/{primary.model}"
    lm = dspy.LM(model_id, temperature=route.temperature or 0.0, cache=True)
    if force or not _dspy_configured:
        dspy.configure(lm=lm)
        _dspy_configured = True
    return dspy, lm
