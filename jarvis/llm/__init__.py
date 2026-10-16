"""
jarvis.llm — multi-provider brain (Gemini / OpenRouter / Groq / +extras)
=======================================================================

    from jarvis.llm import get_chat_model, LLMFactory, ModelRouter, health
    model = get_chat_model("brain")     # FallbackChatModel ya DemoChatModel
"""
from __future__ import annotations

from . import discovery, providers, roles  # noqa: F401
from .core import (  # noqa: F401
    FallbackChatModel,
    LLMFactory,
    build_chain,
    dspy_role_context,
    get_chat_model,
    init_dspy,
    reset_cache,
)
from .demo import DemoChatModel  # noqa: F401
from .roles import (  # noqa: F401
    ResolvedRoute,
    active_provider,
    health,
)
from .roles import (
    attempts as model_attempts,
)
from .roles import (
    resolve as resolve_role,
)

# convenience aliases (user ke spec me ye naam the)
ProviderManager = LLMFactory
ModelRouter = roles
FallbackManager = FallbackChatModel

__all__ = [
    "get_chat_model", "init_dspy", "dspy_role_context", "reset_cache", "build_chain",
    "LLMFactory", "ProviderManager", "ModelRouter", "FallbackManager",
    "FallbackChatModel", "DemoChatModel", "ResolvedRoute",
    "resolve_role", "active_provider", "model_attempts", "health",
    "providers", "roles", "discovery",
]
