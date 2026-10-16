"""
jarvis/agents/langchain_bridge.py — LangChain ⇄ browser-use model bridge
=======================================================================

Ye browser-use ke official example (`examples/models/langchain/chat.py`) se
inspired ek compact bridge hai: kisi bhi **LangChain chat model** ko browser-use
ke `BaseChatModel` protocol me wrap kar deta hai.

Isse Jarvis ka LangChain stack (Gemini/OpenAI/Anthropic/Groq/Ollama — jo bhi
configured hai) seedha **browser-use Agent ke dimaag** ban jaata hai — matlab
Jarvis apna model browser agent ko de deta hai, alag key/config ki zaroorat nahi.

Usage
-----
    from jarvis.llm import get_chat_model
    from jarvis.agents.langchain_bridge import langchain_to_browser_use_llm

    bu_llm = langchain_to_browser_use_llm(get_chat_model("browser"))
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# browser-use core types — optional dependency (na ho to module phir bhi import hona chahiye;
# browser tools khud check karte hain `has_browser_use()` se)
BU_IMPORT_ERROR: str = ""
try:  # pragma: no cover - environment dependent
    from browser_use.llm.base import BaseChatModel as BUBaseChatModel
    from browser_use.llm.messages import (
        AssistantMessage as BUAssistantMessage,
    )
    from browser_use.llm.messages import (
        BaseMessage as BUBaseMessage,
    )
    from browser_use.llm.messages import (
        ContentPartImageParam,
        ContentPartRefusalParam,
        ContentPartTextParam,
    )
    from browser_use.llm.messages import (
        SystemMessage as BUSystemMessage,
    )
    from browser_use.llm.messages import (
        ToolCall as BUToolCall,
    )
    from browser_use.llm.messages import (
        UserMessage as BUUserMessage,
    )
    from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage

    BU_AVAILABLE = True
except Exception as _exc:  # ModuleNotFoundError ya version mismatch
    BU_AVAILABLE = False
    BU_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"
    BUBaseChatModel = object  # type: ignore[assignment,misc]
    BUAssistantMessage = BUBaseMessage = BUUserMessage = BUSystemMessage = None  # type: ignore[assignment]
    ContentPartImageParam = ContentPartRefusalParam = ContentPartTextParam = None  # type: ignore[assignment]
    BUToolCall = ChatInvokeCompletion = ChatInvokeUsage = None  # type: ignore[assignment]


def browser_use_available() -> bool:
    """browser-use installed hai ya nahi (bridge use karne se pehle check)."""
    return BU_AVAILABLE


from langchain_core.messages import AIMessage as LCAIMessage
from langchain_core.messages import HumanMessage as LCHumanMessage
from langchain_core.messages import SystemMessage as LCSystemMessage


# message conversion:  browser-use  →  langchain
# ---------------------------------------------------------------------------
def _text_from_parts(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    out: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, ContentPartTextParam):
            out.append({"type": "text", "text": part.text})
        elif isinstance(part, ContentPartImageParam):
            out.append({"type": "image_url", "image_url": {"url": part.image_url.url}})
        elif isinstance(part, ContentPartRefusalParam):
            out.append({"type": "text", "text": f"[refusal] {part.refusal}"})
        elif isinstance(part, dict):
            out.append(part)
    if len(out) == 1 and out[0].get("type") == "text":
        return out[0]["text"]
    return out


def serialize_messages(messages: list[BUBaseMessage]) -> list[Any]:
    serialized: list[Any] = []
    for msg in messages:
        if isinstance(msg, BUSystemMessage):
            serialized.append(LCSystemMessage(content=_text_from_parts(msg.content)))
        elif isinstance(msg, BUUserMessage):
            serialized.append(LCHumanMessage(content=_text_from_parts(msg.content)))
        elif isinstance(msg, BUAssistantMessage):
            tool_calls = []
            for tc in (msg.tool_calls or []):
                tool_calls.append(
                    {
                        "name": tc.function.name if isinstance(tc, BUToolCall) else getattr(tc, "name", "tool"),
                        "args": tc.function.arguments if isinstance(tc, BUToolCall) else {},
                        "id": getattr(tc, "id", "call_0"),
                        "type": "tool_call",
                    }
                )
            serialized.append(
                LCAIMessage(content=_text_from_parts(msg.content) if msg.content else "", tool_calls=tool_calls)
            )
        else:  # pragma: no cover - future message types
            serialized.append(LCHumanMessage(content=str(getattr(msg, "content", ""))))
    return serialized


def _usage(response: Any) -> ChatInvokeUsage | None:
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return None
    return ChatInvokeUsage(
        prompt_tokens=usage.get("input_tokens") or 0,
        prompt_cached_tokens=None,
        prompt_cache_creation_tokens=None,
        prompt_image_tokens=None,
        completion_tokens=usage.get("output_tokens") or 0,
        total_tokens=usage.get("total_tokens") or 0,
    )


# ---------------------------------------------------------------------------
# the bridge
# ---------------------------------------------------------------------------
@dataclass
class ChatLangChainBridge(BUBaseChatModel):
    """Kisi bhi LangChain `BaseChatModel` ko browser-use LLM bana do."""

    chat: Any

    @property
    def model(self) -> str:
        return self.name

    @property
    def name(self) -> str:
        for attr in ("model_name", "model", "model_id"):
            value = getattr(self.chat, attr, None)
            if value:
                return str(value)
        return self.chat.__class__.__name__

    @property
    def provider(self) -> str:
        cls = self.chat.__class__.__name__.lower()
        for token in ("openai", "anthropic", "google", "gemini", "groq", "ollama", "deepseek", "mistral"):
            if token in cls:
                return "google" if token == "gemini" else token
        return "langchain"

    async def ainvoke(self, messages: list[BUBaseMessage], output_format: type | None = None, **kwargs: Any):
        lc_messages = serialize_messages(messages)
        try:
            if output_format is None:
                response = await self.chat.ainvoke(lc_messages)
                content = getattr(response, "content", str(response))
                if isinstance(content, list):
                    content = "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
                return ChatInvokeCompletion(completion=str(content), usage=_usage(response))

            try:
                structured = self.chat.with_structured_output(output_format)
                parsed = await structured.ainvoke(lc_messages)
                return ChatInvokeCompletion(completion=parsed, usage=None)
            except AttributeError:
                response = await self.chat.ainvoke(lc_messages)
                content = getattr(response, "content", "")
                import json as _json

                data = _json.loads(content)
                return ChatInvokeCompletion(completion=output_format(**data), usage=_usage(response))
        except Exception as exc:  # surface as browser-use error type
            from browser_use.llm.exceptions import ModelProviderError

            raise ModelProviderError(message=f"LangChain bridge error: {exc}", model=self.name) from exc


def langchain_to_browser_use_llm(chat_model: Any) -> Any:
    """
    Smart chooser:
      * Gemini configured → browser-use ka native ChatGoogle (best vision support)
      * baki providers  → ChatLangChainBridge (LangChain model hi chalao)
      * offline/demo    → None (browser agent ko LLM ke bina use nahi kar sakte;
                          caller ko gracefully handle karna chahiye)
    """
    from ..config import MODEL_ROUTES, PROVIDER, env

    if PROVIDER == "google":
        try:
            from browser_use.llm.google import ChatGoogle

            route = MODEL_ROUTES["browser"]
            return ChatGoogle(
                model=route.model,
                api_key=env("GOOGLE_API_KEY") or env("GEMINI_API_KEY"),
                temperature=route.temperature,
            )
        except Exception:
            pass

    if PROVIDER == "demo":
        return None

    if chat_model is not None:
        return ChatLangChainBridge(chat=chat_model)
    return None
