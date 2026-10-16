"""
Provider registry — ek jagah har brain provider ki definition
=============================================================

Add/remove provider karne ke liye sirf yahan ek entry badalni hai; agent code chhoota bhi nahi.
Sab providers LangChain chat models dete hain, isliye agent logic bilkul same rehta hai.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import settings as S


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    kind: str                 # "google" | "openai-compatible" | "anthropic" | "ollama"
    key_env: tuple[str, ...]
    base_url: str = ""
    models_endpoint: str = ""  # discovery ke liye
    free_models: bool = False
    docs: str = ""


PROVIDERS: dict[str, ProviderSpec] = {
    "gemini": ProviderSpec(
        name="gemini", label="Google Gemini", kind="google",
        key_env=("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"),
        models_endpoint="https://generativelanguage.googleapis.com/v1beta/models",
        free_models=True, docs="https://aistudio.google.com/apikey",
    ),
    "openrouter": ProviderSpec(
        name="openrouter", label="OpenRouter", kind="openai-compatible",
        key_env=("OPENROUTER_API_KEY",),
        base_url=S.OPENROUTER_BASE_URL or "https://openrouter.ai/api/v1",
        models_endpoint="https://openrouter.ai/api/v1/models",
        free_models=True, docs="https://openrouter.ai/keys",
    ),
    "groq": ProviderSpec(
        name="groq", label="Groq", kind="openai-compatible",
        key_env=("GROQ_API_KEY",),
        base_url=S.GROQ_BASE_URL or "https://api.groq.com/openai/v1",
        models_endpoint="https://api.groq.com/openai/v1/models",
        free_models=True, docs="https://console.groq.com/keys",
    ),
    "openai": ProviderSpec(
        name="openai", label="OpenAI", kind="openai-compatible",
        key_env=("OPENAI_API_KEY",), base_url=S.OPENAI_BASE_URL or "https://api.openai.com/v1",
        models_endpoint="https://api.openai.com/v1/models", docs="https://platform.openai.com/api-keys",
    ),
    "anthropic": ProviderSpec(
        name="anthropic", label="Anthropic", kind="anthropic",
        key_env=("ANTHROPIC_API_KEY",), docs="https://console.anthropic.com/settings/keys",
    ),
    "xai": ProviderSpec(
        name="xai", label="xAI Grok", kind="openai-compatible",
        key_env=("XAI_API_KEY",), base_url="https://api.x.ai/v1",
        models_endpoint="https://api.x.ai/v1/models", docs="https://console.x.ai",
    ),
    "ollama": ProviderSpec(
        name="ollama", label="Ollama (local)", kind="ollama",
        key_env=("OLLAMA_HOST",), base_url=S.OLLAMA_HOST or "http://localhost:11434",
        models_endpoint="", free_models=True, docs="https://ollama.com/download",
    ),
}

# alias → canonical provider (env me log alag naam likhte hain)
ALIASES: dict[str, str] = {
    "google": "gemini", "google-genai": "gemini", "gemini": "gemini", "googleai": "gemini",
    "or": "openrouter", "open-router": "openrouter", "openrouter": "openrouter",
    "groq": "groq", "groqcloud": "groq",
    "openai": "openai", "oa": "openai",
    "anthropic": "anthropic", "claude": "anthropic",
    "xai": "xai", "grok": "xai",
    "ollama": "ollama", "local": "ollama",
    "demo": "demo", "offline": "demo", "auto": "auto", "": "auto",
}

# openai-compatible providers ko langchain_openai use karta hai
_OPENAI_COMPAT_EXTRA = {
    "openrouter": {
        "default_headers": {
            "HTTP-Referer": "https://github.com/nikhilai",
            "X-Title": "NikhilAI",
        }
    },
    "groq": {},
    "openai": {},
    "xai": {},
}


def canonical(name: str | None) -> str:
    return ALIASES.get((name or "").strip().lower(), (name or "").strip().lower() or "auto")


def api_key(provider: str) -> str:
    spec = PROVIDERS.get(canonical(provider))
    if not spec:
        return ""
    for env_name in spec.key_env:
        import os

        val = os.environ.get(env_name, "").strip()
        if val:
            return val
    return ""


def available() -> list[str]:
    return [name for name in PROVIDERS if api_key(name)]


def is_available(provider: str) -> bool:
    return bool(api_key(provider))


def build_model(provider: str, model: str, temperature: float = 0.2, **kwargs):
    """Provider-specific LangChain chat model banao (graceful import errors ke saath)."""
    name = canonical(provider)
    spec = PROVIDERS.get(name)
    if spec is None:
        raise RuntimeError(f"unknown provider '{provider}' (available: {', '.join(PROVIDERS)})")

    if spec.kind == "google":
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Gemini ke liye `pip install langchain-google-genai` chahiye") from exc
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key(name),
            temperature=temperature,
            timeout=kwargs.get("timeout", S.LLM_TIMEOUT_S),
            retries=kwargs.get("retries", S.LLM_RETRIES),
            convert_system_message_to_human=False,
        )

    if spec.kind == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Anthropic ke liye `pip install langchain-anthropic` chahiye") from exc
        return ChatAnthropic(model=model, api_key=api_key(name), temperature=temperature,
                             timeout=kwargs.get("timeout", S.LLM_TIMEOUT_S),
                             max_retries=kwargs.get("retries", S.LLM_RETRIES))

    if spec.kind == "ollama":
        try:
            from langchain_ollama import ChatOllama
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Ollama ke liye `pip install langchain-ollama` chahiye") from exc
        timeout = kwargs.get("timeout", S.LLM_TIMEOUT_S)
        return ChatOllama(
            model=model,
            temperature=temperature,
            base_url=spec.base_url or None,
            client_kwargs={"timeout": timeout},
        )

    # openai-compatible (openrouter / groq / openai / xai)
    try:
        from langchain_openai import ChatOpenAI
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("OpenRouter/Groq ke liye `pip install langchain-openai` chahiye") from exc
    extra = dict(_OPENAI_COMPAT_EXTRA.get(name, {}))
    return ChatOpenAI(
        model=model,
        api_key=api_key(name),
        base_url=spec.base_url or None,
        temperature=temperature,
        timeout=kwargs.get("timeout", S.LLM_TIMEOUT_S),
        max_retries=kwargs.get("retries", S.LLM_RETRIES),
        **extra,
    )
