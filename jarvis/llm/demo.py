"""
Offline demo brain — bina API key bhi poora agent chalta hai
===========================================================

Jab koi provider key nahi hoti, `DemoChatModel` rule-based brain banta hai: asli LangGraph
graph, asli tools (files, notes, code runner, browser hooks), asli memory — bas LLM ki jagah
ye deterministic brain. Isse system offline test hota hai aur demo bhi dikhta hai.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ..config import OWNER_NAME

# OFFLINE DEMO BRAIN
# ---------------------------------------------------------------------------
_NOTE_RX = re.compile(
    r"(?:note\s*pad|notes?|file)\s*(?:me|mein|में)?\s*(?:ye|yeh|this)?\s*(?P<name>[^,।]*?)\s*(?:naam|name|naam\s*se|ke naam se)?\s*(?:se)?\s*(?:save|likh|rakh|store|likh\s*do|save\s*karo|save\s*kar\s*do)",
    re.IGNORECASE,
)
_SITES_RX = re.compile(r"\b(https?://[^\s,]+|(?:[a-z0-9-]+\.)+(?:com|in|org|net|io|ai|co|dev|gov|edu)(?:/[^\s,]*)?)\b", re.IGNORECASE)


class DemoChatModel(BaseChatModel):
    """
    Offline rule-based brain — API key ke bina poora Jarvis chalane ke liye.

    Ye ek asli LangChain `BaseChatModel` hai:
      * bind_tools() support karta hai (so LangGraph ReAct agents ise use kar sakte hain)
      * Hinglish intents ko tool_calls me convert karta hai
      * tool results ko padh kar final Hinglish answer banata hai
    """

    role: str = "brain"
    bound_tools: dict[str, Any] = {}
    model_config = {"arbitrary_types_allowed": True}

    # ---- langchain plumbing -------------------------------------------------
    @property
    def _llm_type(self) -> str:
        return "nikhilai-demo-brain"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"role": self.role, "tools": list(self.bound_tools)}

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any):  # type: ignore[override]
        mapping: dict[str, Any] = {}
        for t in tools or []:
            name = getattr(t, "name", None) or getattr(t, "__name__", None)
            if name:
                mapping[str(name)] = t
        # keep previously bound tools if new list empty
        return self.model_copy(update={"bound_tools": mapping or self.bound_tools})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._decide(messages))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop, run_manager, **kwargs)

    # ---- decision logic -----------------------------------------------------
    def _decide(self, messages: list[BaseMessage]) -> AIMessage:
        human = self._last_human(messages)
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]

        # Step 2: tools already chale — ab final answer banao
        if tool_msgs:
            return AIMessage(content=self._summarize(human, tool_msgs))

        # Step 1: intent → tool call (agar tool available hai)
        call = self._plan_tool_call(human)
        if call and call["name"] in self.bound_tools:
            return AIMessage(content="", tool_calls=[call])

        # Koi tool nahi chahiye — direct Hinglish answer
        return AIMessage(content=self._chat(human))

    @staticmethod
    def _last_human(messages: list[BaseMessage]) -> str:
        for m in reversed(messages):
            if m.type == "human":
                c = m.content
                if isinstance(c, list):
                    c = " ".join(str(p.get("text", p)) if isinstance(p, dict) else str(p) for p in c)
                return str(c)
            if m.type == "system":
                continue
        return ""

    def _plan_tool_call(self, text: str) -> dict[str, Any] | None:
        t = text.lower()
        has = self.bound_tools.__contains__

        # --- notes / notepad -------------------------------------------------
        if has("save_note") and any(k in t for k in ("note", "notepad", "नोट", "save kar", "save karo", "likh ke rakh")):
            name = "jarvis_note"
            m = _NOTE_RX.search(text)
            if m and m.group("name"):
                name = m.group("name").strip(" .-") or name
            body = text
            if ":" in text:
                body = text.split(":", 1)[1].strip() or text
            return {
                "name": "save_note",
                "args": {"name": name[:60], "content": body, "open_in_notepad": False},
                "id": "demo-note-1",
                "type": "tool_call",
            }

        # --- browser / multi-site research ----------------------------------
        urls = _SITES_RX.findall(text)
        browser_words = any(k in t for k in ("browser", "website", "site", "scrape", "web se", "online", "http"))
        if has("browser_research") and (
            urls
            or (
                browser_words
                and any(k in t for k in ("laao", "lao", "fetch", "nikalo", "collect", "check karo", "dekho", "data"))
            )
        ):
            return {
                "name": "browser_research",
                "args": {"task": text, "sites": urls[:6] if urls else []},
                "id": "demo-browser-1",
                "type": "tool_call",
            }

        # --- code engine -----------------------------------------------------
        if has("code_engine") and any(k in t for k in ("code likh", "script bana", "program bana", "python likh", "code likho", "run karo", "chala do", "automate")):
            return {
                "name": "code_engine",
                "args": {"task": text, "language": "python", "run": True},
                "id": "demo-code-1",
                "type": "tool_call",
            }

        # --- web search ------------------------------------------------------
        if has("google_search") and any(k in t for k in ("search", "khoj", "dhoondh", "google kar", "kya hai", "kaun hai", "kitna")):
            return {
                "name": "google_search",
                "args": {"query": text},
                "id": "demo-search-1",
                "type": "tool_call",
            }

        # --- weather ---------------------------------------------------------
        if has("get_weather") and any(k in t for k in ("weather", "mausam", "मौसम", "barish", "rain", "temperature")):
            city = ""
            m = re.search(r"(?:in|ka|ki|me|mein)\s+([A-Z][a-zA-Z]+)", text)
            if m:
                city = m.group(1)
            return {"name": "get_weather", "args": {"city": city}, "id": "demo-wx-1", "type": "tool_call"}

        # --- time ------------------------------------------------------------
        if has("get_current_datetime") and any(k in t for k in ("time kya", "samay", "date kya", "aaj kaun", "tareekh", "kya time")):
            return {"name": "get_current_datetime", "args": {}, "id": "demo-dt-1", "type": "tool_call"}

        # --- desktop / files -------------------------------------------------
        if has("open_app"):
            for app in ("notepad", "chrome", "calculator", "vs code", "vscode", "vlc", "explorer", "cmd", "terminal", "paint"):
                if app in t and any(k in t for k in ("khol", "open", "chala", "launch", "start")):
                    return {"name": "open_app", "args": {"app_title": app}, "id": "demo-app-1", "type": "tool_call"}
        if has("folder_file"):
            for kw in ("folder bana", "folder create", "file dhoondh", "file khol", "delete kar"):
                if kw in t:
                    return {"name": "folder_file", "args": {"command": text}, "id": "demo-file-1", "type": "tool_call"}

        # --- memory ----------------------------------------------------------
        if has("remember_fact") and any(k in t for k in ("yaad rakh", "note kar le", "remember", "याद रख")):
            return {"name": "remember_fact", "args": {"fact": text, "tags": "owner"}, "id": "demo-mem-1", "type": "tool_call"}
        if has("recall_facts") and any(k in t for k in ("yaad hai", "kya pata", "recall", "mujhe bata")):
            return {"name": "recall_facts", "args": {"query": text}, "id": "demo-mem-2", "type": "tool_call"}

        return None

    # ---- final answer synthesis --------------------------------------------
    def _summarize(self, human: str, tool_msgs: list[ToolMessage]) -> str:
        parts: list[str] = []
        for m in tool_msgs:
            name = getattr(m, "name", "tool")
            content = m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False, default=str)
            content = (content or "").strip()
            if len(content) > 900:
                content = content[:900] + " … (truncated)"
            parts.append(f"• `{name}` → {content}")

        joined = "\n".join(parts)
        return (
            f"हो गया boss ✅ — offline demo brain se execute kiya gaya:\n\n{joined}\n\n"
            "_(Ye DEMO BRAIN ka output hai — real API key (.env me GOOGLE_API_KEY) daal do to yahi flow "
            "Gemini + DSPy + browser-use ke saath full intelligence par chalega.)_"
        )

    def _chat(self, text: str) -> str:
        t = text.lower()
        if any(k in t for k in ("kaun ho", "tum kon", "who are you", "apna naam", "introduce")):
            return (
                f"मैं Jarvis हूं — NikhilAI का personal AI operator, जिसे {OWNER_NAME} के लिए banaya gaya hai. "
                "LangGraph supervisor + DSPy reasoning + browser-use + code engine — sab mere andar wired hai. "
                "Aap bolo, main करता हूं. 🚀"
            )
        if any(k in t for k in ("hello", "hi ", "hey", "namaste", "नमस्ते")) or t.strip() in {"hi", "hey", "hello"}:
            return f"Hey {OWNER_NAME.split()[0]}! Main yahin hoon — full focus, zero drama."
        if any(k in t for k in ("how are you", "kaise ho", "kaisa hai", "kya haal")):
            return "Main badhiya—logs shaant hain, jo rare luxury hai 😄"
        if any(k in t for k in ("thank", "shukriya", "thanks")):
            return "Anytime, Nikhil!"
        if "kya kar sakte" in t or "capabilities" in t or "features" in t:
            return (
                "Mere paas ye powers hain, boss:\n"
                "1. 🌐 **Browser-use agents** — asli browser kholke websites se data laana, form bharna, login karna.\n"
                "2. 🧠 **DSPy reasoning** — multi-step planning, parallel multi-site extraction, self-refine.\n"
                "3. 💻 **Code engine** — mai khud code likhta hoon, chalata hoon, error aaya to khud fix karke re-run karta hoon.\n"
                "4. 🖥️ **Desktop control** — apps, windows, files, notepad notes.\n"
                "5. 🗃️ **Long-term memory** — aapke facts yaad rakhta hoon (SQLite).\n"
                "Bolo kya banayein?"
            )
        if any(k in t for k in ("joke", "chutkula")):
            return "AI ka best trait? Kabhi chai nahi girata—bas kabhi-kabhi confidence se galat hota hai 😄"
        if any(k in t for k in ("sad", "upset", "tension", "stressed", "pareshan", "thak gaya", "bura lag")):
            return "Sunke bura laga, Nikhil. Thoda pause lena bilkul valid hai—main baat ko halka nahi karunga."
        if any(k in t for k in ("what do you think", "tumhara kya khayal", "opinion")):
            return "Mera take: useful tech wahi hai jo real friction kam kare—sirf shiny demo na ho."
        return "Samajh gaya. Is point ko seedha rakhte hain—bina extra lecture ke."
