"""
jarvis/tools/browser_tools.py — browser-use integration (Jarvis ke haath aur aankhein)
====================================================================================

Owner ka requirement: "browser-use ko AI deke, Jarvis se usko prompt dilwao aur
browser-use agent khud browser kholke kaam kare."

Yahi yahan hota hai — 3 layers:

  LAYER 1 — `browser_task(task, start_url=...)`
      Jarvis (LangGraph supervisor) → LangChain tool → browser-use Agent(task=...)
      → asli Chromium khulta hai → click/type/scroll → final result wapas Jarvis ko.
      LLM jo browser agent ke andar lagta hai wo bhi wahi model hai jo Jarvis use
      karta hai (LangChain model → browser-use bridge, ya native ChatGoogle).

  LAYER 2 — `browser_research(task, sites=[...])`
      Multi-site PARALLEL: har site ke liye alag browser agent, ek saath (semaphore
      se bounded). Ye "alag-alag websites se xyz info laao" ka heavy-duty path hai.
      Result ke baad DSPy se structure (JSON) bana kar note me save bhi ho jaata hai.

  LAYER 3 — `browser_cli(code)`  (browser-use ka Path-2 CLI / Browser Harness)
      `browser-use <<'PY' ... PY` — direct CDP control, ek attached tab par fast
      deterministic operations. Ye sasta aur ultra-fast hai jab exact steps pata ho.

Offline/demo mode (koi API key nahi): browser agent LLM ke bina nahi chal sakta, isliye
tool saaf-saaf plan + exact command deta hai, aur usi waqt key-free HTTP fast-path se
data nikaalne ki koshish bhi karta hai — taaki demo me bhi asli output mile.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
import json
import os
import re
import shutil
import time
from typing import Any

from langchain_core.tools import tool

from ..config import (
    BROWSER_HEADLESS,
    BROWSER_KEEP_ALIVE,
    BROWSER_MAX_PARALLEL,
    BROWSER_MAX_STEPS,
    BROWSER_MODE,
    BROWSER_TIMEOUT_S,
    BROWSER_USER_DATA_DIR,
    PROVIDER,
    SCREENSHOTS_DIR,
    WORKSPACE_DIR,
    has_browser_use,
    has_browser_use_cli,
    is_offline,
)
from ..prompts import BROWSER_TASK_PREAMBLE

# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
_SESSION: Any = None
_SESSION_LOCK = asyncio.Lock()
_SHARED_BROWSER_TASK_LOCK = asyncio.Lock()
_LAST_RESULT: dict[str, Any] = {}
_BROWSER_RUN_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "jarvis_browser_run", default=None,
)
BROWSER_STUCK_REPEAT_LIMIT = 6


def _browser_state() -> dict[str, Any]:
    return _BROWSER_RUN_CONTEXT.get() or _LAST_RESULT


def _explicit_memory_write_intent(task: str) -> bool:
    patterns = (
        r"\b(?:save|store|remember|note|record|keep)\b.{0,100}\b(?:this|that|it|data|result|report|finding|fact|price)\b",
        r"\b(?:save|store|remember|note|record|keep)\s+(?:it|this|that)\b",
        r"\b(?:yaad\s+rakh(?:o|na)|note\s+kar(?:o|na)|save\s+kar(?:o|na)|likh\s+kar\s+rakh(?:o|na))\b",
    )
    return any(re.search(pattern, str(task or ""), re.I) for pattern in patterns)


def _owner_memory_write_allowed() -> bool:
    state = _BROWSER_RUN_CONTEXT.get()
    return bool(state and state.get("owner_memory_write_allowed"))


def _browser_memory_context(task: str) -> bool:
    if not _explicit_memory_write_intent(task):
        return False
    from ..security.permissions import check

    surface = _current_surface()
    if surface not in {"cli_owner", "telegram_owner", "voice_owner", "api"}:
        return False
    return check("save_note", surface=surface).allowed and check("remember_fact", surface=surface).allowed


def _current_surface() -> str:
    from ..security.permissions import current_surface

    return current_surface()


def _emit_browser(kind: str, text: str, *, status: str = "", detail: str = "", ms: int = 0) -> None:
    try:
        from ..runtime.progress import emit

        emit(kind, text, tool="browser-use", status=status, detail=detail, ms=ms)
    except Exception:
        pass


def browser_status() -> dict[str, Any]:
    return {
        "python_api": has_browser_use(),
        "cli": has_browser_use_cli(),
        "cli_path": shutil.which("browser-use"),
        "mode": BROWSER_MODE,
        "headless": BROWSER_HEADLESS,
        "provider": PROVIDER,
        "session_live": _SESSION is not None,
    }


# ---------------------------------------------------------------------------
# session (browser ek hi baar khule, kaam ke beech reuse ho)
# ---------------------------------------------------------------------------
def _make_browser_session(*, isolated: bool = False):
    from browser_use import BrowserSession
    from browser_use.browser.profile import BrowserProfile

    profile_kwargs: dict[str, Any] = {
        "headless": BROWSER_HEADLESS,
        "keep_alive": False if isolated else BROWSER_KEEP_ALIVE,
        "minimum_wait_page_load_time": 0.25,
        "wait_for_network_idle_page_load_time": 0.75,
        "viewport": {"width": 1440, "height": 900},
        "highlight_elements": True,
    }
    # Parallel research workers never share a persistent profile/session.
    if BROWSER_USER_DATA_DIR and not isolated:
        profile_kwargs["user_data_dir"] = BROWSER_USER_DATA_DIR
    profile_kwargs["chromium_sandbox"] = os.environ.get("JARVIS_BROWSER_CHROMIUM_SANDBOX", "0") == "1"
    return BrowserSession(browser_profile=BrowserProfile(**profile_kwargs))


async def _close_session(session: Any) -> None:
    if session is None:
        return
    try:
        await session.kill()
    except Exception:
        try:
            await session.close()
        except Exception:
            pass


async def get_browser_session(force_new: bool = False):
    global _SESSION
    if force_new:
        return _make_browser_session(isolated=True)
    if _SESSION is not None:
        return _SESSION
    async with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = _make_browser_session()
        return _SESSION


async def close_browser_session() -> str:
    global _SESSION
    if _SESSION is None:
        return "ℹ️ Koi browser session khula nahi tha."
    session = _SESSION
    _SESSION = None
    await _close_session(session)
    return "🛑 Browser band kar diya."


# ---------------------------------------------------------------------------
# secret passthrough (LLM ko password nahi dikhta — browser-use `sensitive_data`)
# ---------------------------------------------------------------------------
def _collect_sensitive_data() -> dict[str, str] | None:
    data: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith("JARVIS_SECRET_") and value:
            data[key.replace("JARVIS_SECRET_", "").lower()] = value
    return data or None


# ---------------------------------------------------------------------------
# custom actions — browser agent Jarvis ke tools bhi khud use kar sakta hai
# ---------------------------------------------------------------------------
def _browser_registry() -> dict[str, Any]:
    from . import ALL_TOOLS

    return {str(getattr(item, "name", "")): item for item in ALL_TOOLS if getattr(item, "name", "")}


def _approval_preview(value: Any, key: str = "") -> Any:
    if re.search(r"password|secret|token|credential|api.?key|authorization", key, re.I):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _approval_preview(v, str(k)) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [_approval_preview(item) for item in value[:40]]
    if isinstance(value, str):
        text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", value)
        text = re.sub(r"(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+",
                      r"\1[REDACTED]", text)
        text = re.sub(r"(?i)([?&](?:token|api[_-]?key|password|secret|auth|code)=)[^&#]+",
                      r"\1[REDACTED]", text)
        return text[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:200]


async def _authorize_browser_delegated_tool(tool_name: str, payload: dict[str, Any], *, code: str = "") -> str:
    """Enforce the current surface and route dangerous/code requests through the human Gate."""
    from ..security.gate import authorize_command
    from ..security.permissions import check, current_surface, tool_level

    surface = current_surface()
    decision = check(tool_name, surface=surface)
    if not decision.allowed:
        return f"🚫 Permission blocked: {decision.reason}"
    if tool_name in {"save_note", "remember_fact"} and not _owner_memory_write_allowed():
        return "🚫 Browser page se note/memory write nahi ki; owner ne is turn me save/remember explicitly nahi kaha."

    internal_gate_tools = {
        "run_shell_command", "start_background_process", "process_stop", "clear_process_tool",
        "close_app", "install_package", "restart_jarvis_tool", "folder_file", "write_file",
        "run_script_file", "run_python", "code_engine",
    }
    level = tool_level(tool_name)
    needs_force_gate = (level == "DANGEROUS" and tool_name not in internal_gate_tools)
    needs_code_inspection = bool(code) and tool_name in {"python_repl", "run_python_isolated", "browser_cli"}
    needs_force_gate = needs_force_gate or tool_name == "process_input"
    should_gate = needs_force_gate or needs_code_inspection or tool_name == "clear_cache_tool"
    if tool_name == "clear_cache_tool" and payload.get("dry_run") is True:
        from ..security.gate import approval_mode

        should_gate = approval_mode() == "all"
    force_approval = needs_force_gate and not (tool_name == "clear_cache_tool" and payload.get("dry_run") is True)
    if should_gate:
        from ..config import WORKSPACE_DIR

        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()[:16]
        preview = json.dumps(_approval_preview(payload), ensure_ascii=False, default=str)[:1000]
        operation = f"browser delegated tool {tool_name} args_sha256={digest} args={preview}"
        safe_assessment = "echo ok" if code or (tool_name == "clear_cache_tool" and payload.get("dry_run") is True) else None
        approval = await authorize_command(
            operation, str(WORKSPACE_DIR), force=force_approval, inspection_text=code,
            assessment_command=safe_assessment, surface=surface,
            reason="browser agent requested a destructive or shell-capable Jarvis tool",
        )
        if not approval.allowed:
            return f"🚫 Request tool nahi chalaya: {approval.reason}"
    return ""


def _build_browser_tools(output_model_fields: list[str] | None = None):
    """browser-use `Tools` registry me Jarvis ke apne actions register karo."""
    from browser_use import Tools

    tools = Tools()

    @tools.registry.action("Use only a registered Jarvis tool; arguments_json must be a JSON object.")
    async def jarvis_request_tool(tool_name: str, arguments_json: str = "{}") -> str:
        tool_name = str(tool_name or "").strip()
        arguments_json = str(arguments_json or "{}")
        if len(arguments_json) > 20000:
            return "❌ Tool arguments too large; request was not run."
        try:
            payload = json.loads(arguments_json or "{}")
        except (TypeError, json.JSONDecodeError):
            return "❌ arguments_json parse nahi hua; valid JSON object bhejo."
        if not isinstance(payload, dict):
            return "❌ arguments_json ek JSON object hona chahiye."
        registry = _browser_registry()
        selected = registry.get(tool_name)
        if selected is None:
            return f"❌ '{tool_name}' registered Jarvis tool nahi hai; koi action nahi chala."
        if tool_name in {"browser_task", "browser_research", "browser_cli", "browser_close"}:
            return "🚫 Nested browser-agent tool call rok di; current browser task pehle finish karo."
        denied = await _authorize_browser_delegated_tool(tool_name, payload, code=str(payload.get("code", "")))
        if denied:
            return denied
        try:
            result = await asyncio.wait_for(selected.ainvoke(payload), timeout=min(120, max(1, BROWSER_TIMEOUT_S)))
        except asyncio.TimeoutError:
            return f"⏱️ Registered tool '{tool_name}' time budget cross kar gaya; result verify nahi hua."
        except Exception as exc:
            _emit_browser("error", f"Delegated tool {tool_name} failed ({type(exc).__name__}).", status="fail")
            return f"❌ Registered tool '{tool_name}' failed ({type(exc).__name__}); result verify nahi hua."
        safe_detail = str(_approval_preview({"result": str(result)}).get("result", ""))[:300]
        _emit_browser("tool_end", f"Delegated tool {tool_name} returned.", status="ok", detail=safe_detail)
        return str(result)[:5000]

    @tools.registry.action("Save a note to disk for the owner (use when the task says to save/note the data).")
    async def jarvis_save_note(name: str, content: str) -> str:
        if not _owner_memory_write_allowed():
            return "🚫 Browser page se note save nahi kiya; owner ne is turn me save explicitly nahi kaha."
        return await _save_browser_note(name, content, tags="browser-agent")

    @tools.registry.action("Remember an important fact about the owner in long-term memory.")
    async def jarvis_remember(fact: str) -> str:
        if not _owner_memory_write_allowed():
            return "🚫 Browser page se memory update nahi ki; owner ne is turn me remember explicitly nahi kaha."
        from ..memory.store import get_memory
        from ..security.permissions import check, current_surface

        decision = check("remember_fact", surface=current_surface())
        if not decision.allowed:
            return f"🚫 Memory write permission blocked: {decision.reason}"
        get_memory().remember_fact(fact, tags="browser-agent")
        return f"remembered: {fact[:120]}"

    @tools.registry.action("Report concise progress to the Jarvis supervisor after a sub-goal.")
    async def jarvis_progress(note: str) -> str:
        _browser_state().setdefault("progress", []).append(note[:300])
        _emit_browser("thinking", f"Browser sub-goal: {note[:180]}")
        return "ok"

    return tools


def _structured_model(fields: list[str]):
    """Field names se dynamic pydantic model — browser-use structured output ke liye."""
    from pydantic import create_model

    annotations = {f: (str | None, None) for f in fields if f.strip()}
    if not annotations:
        return None
    return create_model("JarvisExtraction", **annotations)  # type: ignore[call-overload]


# ---------------------------------------------------------------------------
# agent builder
# ---------------------------------------------------------------------------
def _browser_llm():
    from ..agents.langchain_bridge import langchain_to_browser_use_llm
    from ..llm import get_chat_model

    chat = None if (is_offline() or PROVIDER == "demo") else get_chat_model("browser")
    return langchain_to_browser_use_llm(chat)


def _supports_keyword(callable_obj: Any, keyword: str) -> bool:
    try:
        params = inspect.signature(callable_obj).parameters.values()
        return any(item.name == keyword or item.kind == inspect.Parameter.VAR_KEYWORD for item in params)
    except Exception:
        return True


async def build_browser_agent(
    task: str,
    output_fields: list[str] | None = None,
    max_steps: int | None = None,
    start_url: str = "",
    *,
    isolated_session: bool = False,
):
    """Ek taiyaar browser-use Agent banao. None return karega agar LLM available nahi."""
    if not has_browser_use():
        return None
    llm = _browser_llm()
    if llm is None:
        return None

    from browser_use import Agent

    session = await get_browser_session(force_new=True) if isolated_session else await get_browser_session()
    if isolated_session:
        _browser_state()["session"] = session
    model = _structured_model(output_fields or [])

    full_task = task.strip()
    if start_url:
        full_task = f"Start at {start_url}.\n{full_task}"
    full_task += (
        f"\n\nWhen done, report: (1) exact data found, (2) source URLs, "
        f"(3) anything that failed. Owner: {os.environ.get('JARVIS_OWNER_NAME', 'Nikhil Mehra')}."
    )

    agent_args: dict[str, Any] = {
        "task": full_task,
        "llm": llm,
        "browser_session": session,
        "tools": _build_browser_tools(output_fields),
        "output_model_schema": model,
        "max_actions_per_step": 4,
        "use_vision": True,
        "use_thinking": True,
        "enable_planning": True,
        "extend_system_message": BROWSER_TASK_PREAMBLE,
        "sensitive_data": _collect_sensitive_data(),
        "max_failures": 3,
        "step_timeout": 180,
        "generate_gif": False,
        "register_new_step_callback": None,
    }
    if not _supports_keyword(Agent, "register_new_step_callback"):
        agent_args.pop("register_new_step_callback", None)
    if not _supports_keyword(Agent.run, "on_step_end") and _supports_keyword(Agent, "on_step_end"):
        agent_args["on_step_end"] = _browser_step_hook
    agent = Agent(**agent_args)
    return agent


async def _browser_step_hook(agent: Any) -> None:
    state = _browser_state()
    try:
        step_n = int(getattr(getattr(agent, "state", None), "n_steps", 0) or 0)
        summary = await agent.browser_session.get_browser_state_summary()
        url = str(getattr(summary, "url", "") or "")
        title = str(getattr(summary, "title", "") or "")
        dom = (getattr(summary, "dom_state", None) or getattr(summary, "element_tree", None)
               or getattr(summary, "selector_map", None) or "")
        if isinstance(summary, dict):
            url = str(summary.get("url", url))
            title = str(summary.get("title", title))
            dom = summary.get("dom_state") or summary.get("element_tree") or summary.get("content") or dom
        if not url:
            url = str(await agent.browser_session.get_current_page_url())
        if not title:
            title = str(await agent.browser_session.get_current_page_title())
        url = str(_approval_preview({"url": url}).get("url", url))
        history = getattr(agent, "history", None)
        actions = history.model_actions() if history and callable(getattr(history, "model_actions", None)) else []
        last_action = repr(actions[-1])[:1000] if actions else ""
        signature_source = f"{url}\n{title}\n{str(dom)[:6000]}\n{last_action}"
        signature = hashlib.sha256(signature_source.encode("utf-8", errors="replace")).hexdigest()
        if signature_source.strip("\n"):
            if signature == state.get("last_signature"):
                state["repeat_count"] = int(state.get("repeat_count", 0)) + 1
            else:
                state["last_signature"] = signature
                state["repeat_count"] = 1
        entry = {"step": step_n or len(state.setdefault("steps", [])) + 1, "url": url,
                 "title": title, "ts": time.time()}
        state.setdefault("steps", []).append(entry)
        _emit_browser("thinking", f"Browser step {entry['step']}" + (f" — {title[:100]}" if title else ""),
                      detail=url[:500])
        callback = state.get("on_step")
        if callback:
            try:
                result = callback(dict(entry))
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
        if int(state.get("repeat_count", 0)) >= BROWSER_STUCK_REPEAT_LIMIT:
            state["stuck_reason"] = (
                f"Same page/action state repeated {state['repeat_count']} steps; "
                "stopped to avoid an unproductive browser loop."
            )
            event = state.get("stop_event")
            if event is not None:
                event.set()
            _emit_browser("warn", "Browser loop jaisa lag raha tha; agent ko rok raha hoon.",
                          status="skipped", detail=url[:500])
            pause = getattr(agent, "pause", None)
            if callable(pause):
                paused = pause()
                if inspect.isawaitable(paused):
                    await paused
    except Exception as exc:
        # A telemetry failure must not hide the browser action result.
        _emit_browser("warn", f"Browser progress snapshot unavailable ({type(exc).__name__}).")


# ---------------------------------------------------------------------------
# core runner
# ---------------------------------------------------------------------------
def _offline_plan(task: str, sites: list[str] | None = None) -> str:
    sites = sites or []
    lines = [
        "🧠 **Offline mode** — browser-use agent ko chalane ke liye LLM key chahiye.",
        "Jo command ready hai (key daalte hi chalu):",
        "",
        "```python",
        "from browser_use import Agent, BrowserSession, BrowserProfile",
        "from jarvis.agents.langchain_bridge import langchain_to_browser_use_llm",
        "from jarvis.llm import get_chat_model",
        "",
        f"task = {task[:120]!r}",
        "agent = Agent(task=task, llm=langchain_to_browser_use_llm(get_chat_model('browser')),",
        "              browser_session=BrowserSession(browser_profile=BrowserProfile(headless=True, keep_alive=True)))",
        "history = await agent.run(max_steps=60)",
        "print(history.final_result())",
        "```",
    ]
    if sites:
        lines += ["", f"🎯 Target sites ({len(sites)}): " + ", ".join(sites[:8])]
    lines += ["", "ℹ️ `.env` me `GOOGLE_API_KEY=...` daalo → phir yahi tool asli browser khol dega."]
    return "\n".join(lines)


async def _http_fallback(task: str, sites: list[str] | None = None) -> str:
    """Key ke bina bhi kuch asli data mile — key-free HTTP fast path."""
    try:
        from .web_tools import deep_research

        if sites:
            result = await deep_research.ainvoke({"query": task, "sites": sites, "max_pages": min(len(sites), 5)})
        else:
            head = " ".join((task or "").split()[:12])
            result = await deep_research.ainvoke({"query": head, "max_pages": 3})
        return str(result)[:4000]
    except Exception as exc:
        return f"(http fast-path bhi fail hua: {type(exc).__name__})"


def _browser_task_risk_reason(task: str) -> str:
    patterns = (
        r"\b(?:delete|remove|erase|destroy)\b",
        r"\b(?:buy|purchase|checkout|pay\s+for|transfer\s+(?:money|funds)|place\s+(?:an?\s+)?order)\b",
        r"\b(?:send|submit)\s+(?:the\s+)?(?:email|message|form|application|order|payment|funds|money|file|invite)\b",
        r"\b(?:publish|post)\s+(?:the\s+)?(?:update|message|comment|article|tweet|content)\b",
        r"\b(?:cancel|unsubscribe)\s+(?:the\s+)?(?:subscription|booking|order|account|plan|service)\b",
    )
    text = task or ""
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            before = text[max(0, match.start() - 65):match.start()]
            if re.search(r"(?i)\b(?:no|not|never|without|avoid|dont|don't|do not)\s+(?:[\w'-]+\s+){0,2}$", before):
                continue
            if re.search(r"(?i)\b(?:how to|steps to|instructions to|guide to|explain|what is|compare)\s+(?:[\w'-]+\s+){0,3}$", before):
                continue
            return "browser action may delete, purchase, send, submit, publish, cancel, or transfer data"
    return ""

async def _authorize_browser_task(task: str) -> str:
    reason = _browser_task_risk_reason(task)
    if not reason:
        return ""
    from ..security.gate import authorize_command
    from ..security.permissions import current_surface

    digest = hashlib.sha256(task.encode("utf-8", errors="replace")).hexdigest()[:16]
    preview = _approval_preview({"task": task}).get("task", "")
    approval = await authorize_command(
        f"browser action sha256={digest}: {preview}", str(WORKSPACE_DIR), force=True,
        surface=current_surface(), reason=reason,
    )
    return "" if approval.allowed else f"🚫 Browser action nahi chala: {approval.reason}"


async def _save_browser_note(
    name: str,
    content: str,
    *,
    tags: str,
    owner_write_allowed: bool | None = None,
) -> str:
    if owner_write_allowed is None:
        owner_write_allowed = _owner_memory_write_allowed()
    if not owner_write_allowed:
        return "🚫 Browser content ko note me save nahi kiya; owner turn me explicit save intent nahi mila."
    from ..security.permissions import check, current_surface
    from .notes_tools import save_note

    decision = check("save_note", surface=current_surface())
    if not decision.allowed:
        return f"🚫 Note save permission blocked: {decision.reason}"
    try:
        return await save_note.ainvoke({"name": name, "content": content,
                                        "open_in_notepad": False, "tags": tags})
    except Exception as exc:
        return f"❌ Note save failed ({type(exc).__name__}); no direct-write fallback was used."


async def run_browser_task(
    task: str,
    start_url: str = "",
    output_fields: list[str] | None = None,
    max_steps: int | None = None,
    save_note_as: str = "",
    timeout: int = BROWSER_TIMEOUT_S,
    *,
    on_step=None,
    isolated_session: bool = False,
) -> dict[str, Any]:
    """Run one browser agent under step/time budgets, reporting progress and halting stuck loops."""
    step_budget = min(max(1, int(max_steps or BROWSER_MAX_STEPS)), max(1, int(BROWSER_MAX_STEPS)))
    time_budget = min(max(1, int(timeout or BROWSER_TIMEOUT_S)), max(1, int(BROWSER_TIMEOUT_S)))
    state: dict[str, Any] = {
        "task": task, "started": time.time(), "steps": [], "progress": [],
        "stop_event": asyncio.Event(), "repeat_count": 0, "last_signature": "", "on_step": on_step,
        "owner_memory_write_allowed": _browser_memory_context(task),
    }
    token = _BROWSER_RUN_CONTEXT.set(state)
    started = time.monotonic()
    safe_task = str(_approval_preview({"task": task}).get("task", ""))[:300]
    _emit_browser("tool_start", "Browser task shuru ho raha hai.", detail=safe_task)
    try:
        if isolated_session:
            outcome = await _run_browser_task_impl(
                task, start_url, output_fields, step_budget, save_note_as, time_budget,
                state=state, isolated_session=True,
            )
        else:
            async with _SHARED_BROWSER_TASK_LOCK:
                outcome = await _run_browser_task_impl(
                    task, start_url, output_fields, step_budget, save_note_as, time_budget,
                    state=state, isolated_session=False,
                )
        state.update({"ok": bool(outcome.get("ok")), "offline": bool(outcome.get("offline")),
                      "result": str(outcome.get("result", ""))[:2000]})
        status = "ok" if outcome.get("ok") else "fail"
        kind = "tool_end" if outcome.get("ok") else "warn"
        _emit_browser(kind, "Browser task complete hua." if outcome.get("ok") else
                      "Browser task ka result incomplete/failed hai.", status=status,
                      detail=str(outcome.get("result", ""))[:350],
                      ms=int((time.monotonic() - started) * 1000))
        return outcome
    except Exception as exc:
        state.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:250]}"})
        _emit_browser("error", f"Browser task fail hua ({type(exc).__name__}).", status="fail")
        return {"ok": False, "result": f"❌ Browser task fail hua ({type(exc).__name__}); result verify nahi hua.",
                "urls": [], "structured": None, "steps": len(state["steps"])}
    finally:
        session = state.get("session")
        if isolated_session and session is not None:
            await _close_session(session)
        _LAST_RESULT.clear()
        _LAST_RESULT.update({key: value for key, value in state.items()
                             if key not in {"stop_event", "on_step", "session"}})
        _BROWSER_RUN_CONTEXT.reset(token)


async def _run_browser_task_impl(
    task: str,
    start_url: str,
    output_fields: list[str] | None,
    max_steps: int,
    save_note_as: str,
    timeout: int,
    *,
    state: dict[str, Any],
    isolated_session: bool,
) -> dict[str, Any]:
    build_timeout = min(60, timeout)
    try:
        agent = await asyncio.wait_for(
            build_browser_agent(task, output_fields, max_steps, start_url, isolated_session=isolated_session),
            timeout=build_timeout,
        )
    except Exception as exc:
        agent = None
        state["build_error"] = type(exc).__name__

    if agent is None:
        fetched = await _http_fallback(task, [start_url] if start_url else [])
        if state.get("build_error"):
            reason = f"❌ Browser agent setup failed ({state['build_error']}); browser automation did not run."
            offline = False
        elif not has_browser_use():
            reason = "⚠️ `browser-use` Python API installed nahi hai; browser automation nahi chali."
            offline = True
        else:
            reason = _offline_plan(task, [start_url] if start_url else [])
            offline = True
        return {
            "ok": False, "offline": offline,
            "result": reason + "\n\n---\n### 🌐 HTTP fast-path output (browser activity nahi hui):\n" + fetched,
            "urls": [], "structured": None, "steps": len(state["steps"]),
        }

    state["session"] = getattr(agent, "browser_session", None) or state.get("session")
    try:
        try:
            run_coro = agent.run(max_steps=max_steps, on_step_end=_browser_step_hook)
        except TypeError:
            # Compatibility with older browser-use releases that expose the hook in Agent(...).
            run_coro = agent.run(max_steps=max_steps)
        run_task = asyncio.create_task(run_coro)
        stop_task = asyncio.create_task(state["stop_event"].wait())
        done, pending = await asyncio.wait(
            {run_task, stop_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
        )
        if run_task in done:
            history = run_task.result()
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        elif stop_task in done and state.get("stuck_reason"):
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            state.update({"ok": False, "error": state["stuck_reason"]})
            return {"ok": False, "result": f"⚠️ Browser rok diya: {state['stuck_reason']}",
                    "urls": [], "structured": None, "steps": len(state["steps"])}
        else:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            state.update({"ok": False, "error": f"timeout {timeout}s"})
            return {"ok": False,
                    "result": f"⏱️ Browser agent {timeout}s budget cross kar gaya (max_steps={max_steps}); result verify nahi hua.",
                    "urls": [], "structured": None, "steps": len(state["steps"])}
        for pending_task in pending:
            pending_task.cancel()
    except asyncio.CancelledError:
        child_tasks = [task_obj for task_obj in (locals().get("run_task"), locals().get("stop_task"))
                       if task_obj is not None]
        for task_obj in child_tasks:
            if not task_obj.done():
                task_obj.cancel()
        await asyncio.gather(*child_tasks, return_exceptions=True)
        raise
    except Exception as exc:
        child_tasks = [task_obj for task_obj in (locals().get("run_task"), locals().get("stop_task"))
                       if task_obj is not None]
        for task_obj in child_tasks:
            if not task_obj.done():
                task_obj.cancel()
        await asyncio.gather(*child_tasks, return_exceptions=True)
        state.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        return {"ok": False, "result": f"❌ Browser agent error ({type(exc).__name__}); result verify nahi hua.",
                "urls": [], "structured": None, "steps": len(state["steps"])}

    final = ""
    structured = None
    urls: list[str] = []
    try:
        final = str(history.final_result() or "")
    except Exception:
        pass
    try:
        urls = [str(_approval_preview({"url": url}).get("url", url)) for url in (history.urls() or [])]
    except Exception:
        pass
    try:
        structured = history.structured_output
    except Exception:
        structured = None
    if structured is not None:
        try:
            structured = structured.model_dump() if hasattr(structured, "model_dump") else dict(structured)
        except Exception:
            structured = str(structured)

    payload = {
        "ok": bool(final), "result": final or "(agent ne koi final result nahi diya)",
        "urls": urls[:12], "structured": structured, "steps": len(state["steps"]), "notes_saved": "",
    }
    if save_note_as and payload["ok"]:
        body = payload["result"]
        if structured:
            body += "\n\n## Structured data (JSON)\n```json\n" + json.dumps(structured, indent=2, ensure_ascii=False) + "\n```"
        if urls:
            body += "\n\n## Sources\n" + "\n".join(f"- {url}" for url in urls[:12])
        payload["notes_saved"] = await _save_browser_note(save_note_as, body, tags="browser-use")
    return payload


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------
@tool
async def browser_task(
    task: str,
    start_url: str = "",
    output_fields: list[str] | None = None,
    max_steps: int = 0,
    save_note_as: str = "",
) -> str:
    """Give a task to a real browser agent (browser-use). Ye Chromium khud kholta hai,
    page padhta hai, click/type/scroll karta hai, form bharta hai, aur data wapas laata hai.

    Use jab:
      - website par interaction chahiye (login, form, submit, pagination, "Load more")
      - JS-heavy / bot-protected page ho (simple fetch se kaam na chale)
      - "browser khol ke ye kaam karo" type request
    start_url: kahan se shuru karna hai (optional).
    output_fields: chahiye data ke field names, e.g. ["company","price","contact"] → structured JSON milega.
    save_note_as: result ko is naam se note me save kar do (optional).
    """
    from ..security.permissions import check, current_surface

    decision = check("browser_task", surface=current_surface())
    if not decision.allowed:
        return f"🚫 Browser permission blocked: {decision.reason}"
    gate_result = await _authorize_browser_task(task)
    if gate_result:
        return gate_result
    outcome = await run_browser_task(
        task=task,
        start_url=start_url,
        output_fields=output_fields,
        max_steps=max_steps or None,
        save_note_as=save_note_as,
    )
    head = "🌐 Browser agent report" + (" (offline)" if outcome.get("offline") else "")
    parts = [head, "=" * 40, str(outcome["result"])[:6000]]
    if outcome.get("structured"):
        parts += ["", "## Structured output", "```json", json.dumps(outcome["structured"], indent=2, ensure_ascii=False)[:3000], "```"]
    if outcome.get("urls"):
        parts += ["", "## Pages visited"] + [f"- {u}" for u in outcome["urls"][:10]]
    parts.append(f"\n⏱️ steps: {outcome.get('steps', 0)}")
    if outcome.get("notes_saved"):
        parts.append(f"📝 {outcome['notes_saved']}")
    return "\n".join(parts)


@tool
async def browser_research(task: str, sites: list[str] | None = None, fields: list[str] | None = None,
                           save_note_as: str = "", max_parallel: int = 0) -> str:
    """Research multiple sites concurrently with isolated browser sessions and an honest report."""
    from ..security.permissions import check, current_surface

    decision = check("browser_research", surface=current_surface())
    if not decision.allowed:
        return f"🚫 Browser research permission blocked: {decision.reason}"
    parallel_limit = min(max(1, int(max_parallel or BROWSER_MAX_PARALLEL)), max(1, int(BROWSER_MAX_PARALLEL)))
    target_sites = list(dict.fromkeys(s.strip() for s in (sites or []) if s and s.strip()))
    if not target_sites:
        from .web_tools import web_search_results

        try:
            results = await web_search_results(task, num=min(5, parallel_limit))
            target_sites = list(dict.fromkeys(r["url"] for r in results if r.get("url")))[:parallel_limit]
        except Exception as exc:
            _emit_browser("error", f"Browser research site search failed ({type(exc).__name__}).", status="fail")
            return f"❌ Research ke liye sites search fail hui ({type(exc).__name__}); koi browser agent nahi chala."
        if not target_sites:
            return "❌ Koi site nahi mili research ke liye — sites explicitly do."
    else:
        target_sites = target_sites[:max(1, min(12, parallel_limit * 4))]

    _emit_browser("thinking", f"Multi-site research: {len(target_sites)} sites, max {parallel_limit} parallel.")
    semaphore = asyncio.Semaphore(parallel_limit)

    async def _one(site: str, index: int) -> dict[str, Any]:
        async with semaphore:
            safe_site = str(_approval_preview({"url": site}).get("url", site))
            _emit_browser("thinking", f"Research site {index + 1}/{len(target_sites)}: {safe_site[:160]}")
            sub_task = f"{task}\nFocus only on this site: {site}. Extract the requested fields if present."
            try:
                outcome = await run_browser_task(
                    task=sub_task, start_url=site, output_fields=fields or [],
                    max_steps=BROWSER_MAX_STEPS, isolated_session=True,
                )
            except Exception as exc:
                _emit_browser("error", f"Research site failed ({type(exc).__name__}).", status="fail", detail=site)
                return {"ok": False, "result": f"agent error ({type(exc).__name__})", "site": site, "urls": []}
            return {**outcome, "site": site}

    started = time.monotonic()
    outcomes = await asyncio.gather(
        *(_one(site, i) for i, site in enumerate(target_sites)), return_exceptions=True,
    )
    elapsed = time.monotonic() - started
    blocks: list[str] = []
    structured_rows: list[dict[str, Any]] = []
    success_count = 0
    for site, outcome in zip(target_sites, outcomes, strict=True):
        display_site = str(_approval_preview({"url": site}).get("url", site))
        if isinstance(outcome, Exception):
            blocks.append(f"### ⚠️ {display_site} — failed\n{type(outcome).__name__}: {str(outcome)[:250]}")
            continue
        urls = [str(_approval_preview({"url": url}).get("url", url)) for url in outcome.get("urls", []) if url]
        if outcome.get("ok"):
            success_count += 1
            blocks.append(f"### ✅ {display_site}\n{str(outcome.get('result', ''))[:2200]}")
        else:
            blocks.append(f"### ⚠️ {display_site} — incomplete/failed\n{str(outcome.get('result', 'No verified result'))[:1200]}")
        if urls:
            blocks.append("Sources:\n" + "\n".join(f"- {url}" for url in urls[:8]))
        structured = outcome.get("structured")
        if outcome.get("ok") and isinstance(structured, dict) and structured:
            structured_rows.append({"site": site, **structured})

    normalization_warning = ""
    if structured_rows and not (is_offline() or PROVIDER == "demo"):
        _emit_browser("thinking", f"Normalizing {len(structured_rows)} structured research rows.")
        try:
            from ..dspy_engine.modules import normalize_rows

            normalized = await asyncio.to_thread(normalize_rows, structured_rows, fields or [])
            if isinstance(normalized, list) and all(isinstance(row, dict) for row in normalized):
                if normalized:
                    structured_rows = normalized
            else:
                normalization_warning = "Normalization returned no valid rows; raw structured rows are retained."
        except Exception as exc:
            normalization_warning = f"Normalization failed ({type(exc).__name__}); raw structured rows are retained."
        if normalization_warning:
            _emit_browser("warn", normalization_warning, status="skipped")

    report = [
        f"🌐 **Multi-site research** — {success_count}/{len(target_sites)} sites returned results; "
        f"{len(outcomes)} agents, {elapsed:.1f}s (parallel).",
        "=" * 50,
        "\n\n".join(blocks)[:9000],
    ]
    if structured_rows:
        report += ["", "## Combined structured data (JSON)", "```json",
                   json.dumps(structured_rows, indent=2, ensure_ascii=False)[:4000], "```"]
    if normalization_warning:
        report += ["", f"⚠️ {normalization_warning}"]
    if save_note_as:
        body = "\n".join(report) + "\n\n## Raw JSON\n```json\n" + json.dumps(
            structured_rows, indent=2, ensure_ascii=False,
        ) + "\n```"
        saved = await _save_browser_note(
            save_note_as, body, tags="browser-research,multi-site",
            owner_write_allowed=_browser_memory_context(task),
        )
        report.append(f"\n📝 {saved}")
    result = "\n".join(report)
    _emit_browser("tool_end" if success_count else "warn",
                  f"Multi-site report ready: {success_count}/{len(target_sites)} sites returned results.",
                  status="ok" if success_count else "fail", detail=result[:300],
                  ms=int(elapsed * 1000))
    return result


@tool
async def browser_cli(code: str, timeout: int = 180) -> str:
    """Run browser-use ka CLI (Path-2 / Browser Harness) — direct CDP control, ultra fast.

    `browser-use` CLI Python code stdin se leta hai aur attached Chrome tab par chalata hai.
    Helpers pre-imported hote hain: new_tab(url), page_info(), click(...), type_text(...),
    scroll(...), list_tabs(), switch_tab(), screenshot() ...

    Example code:
        new_tab("https://news.ycombinator.com")
        print(page_info())

    Use jab exact steps pata hon (fast, sasta). Exploratory/unknown kaam ke liye `browser_task` behtar hai.
    """
    if not has_browser_use_cli():
        return (
            "⚠️ `browser-use` CLI machine par nahi mila.\n"
            "Install:\n```bash\npip install browser-use\nbrowser-use install   # chromium + deps\n```\n"
            "Bina CLI ke bhi `browser_task` (Python API) kaam karta hai — wahi use karo."
        )
    denied = await _authorize_browser_delegated_tool("browser_cli", {"code": code, "timeout": timeout}, code=code)
    if denied:
        return denied
    timeout = min(max(1, int(timeout)), max(1, int(BROWSER_TIMEOUT_S)))
    proc = await asyncio.create_subprocess_exec(
        "browser-use",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=code.encode()), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"⏱️ browser-use CLI {timeout}s me complete nahi hua."
    stdout = (out or b"").decode(errors="ignore")
    stderr = (err or b"").decode(errors="ignore")
    body = f"$ browser-use <<'PY'\n{code[:600]}\nPY\n[exit {proc.returncode}]\n{stdout[-4000:]}"
    if stderr.strip():
        body += f"\n--- stderr ---\n{stderr[-1500:]}"
    return body


@tool
async def browser_screenshot(name: str = "page") -> str:
    """Current browser page ka screenshot lo (browser-use session se)."""
    if not has_browser_use():
        return "❌ browser-use installed nahi hai."
    try:
        session = await get_browser_session()
        page = await session.get_current_page()
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w-]", "_", name)[:40]
        path = SCREENSHOTS_DIR / f"{safe_name}_{int(time.time())}.png"
        data = await page.screenshot()
        path.write_bytes(data)
        return f"📸 Screenshot: {path} (url: {page.url})"
    except Exception as exc:
        return f"❌ Screenshot fail: {exc}"


@tool
async def browser_close() -> str:
    """Close the browser session (RAM free karo)."""
    return await close_browser_session()


@tool
async def browser_doctor() -> str:
    """browser-use setup ki health check: python API, CLI, chromium, mode, provider."""
    info = browser_status()
    lines = [
        "🩺 browser-use status",
        f"• python API      : {'✅' if info['python_api'] else '❌'}",
        f"• CLI (Path-2)    : {'✅' if info['cli'] else '❌'} {info['cli_path'] or ''}",
        f"• mode            : {info['mode']}",
        f"• headless        : {info['headless']}",
        f"• LLM provider    : {info['provider']}",
        f"• session live    : {info['session_live']}",
    ]
    try:
        import importlib.metadata as md
        lines.append(f"• browser-use ver : {md.version('browser-use')}")
    except Exception:
        pass
    if not info["cli"]:
        lines.append("\nCLI install: `pip install browser-use && browser-use install`")
    return "\n".join(lines)


BROWSER_TOOLS = [
    browser_task,
    browser_research,
    browser_cli,
    browser_screenshot,
    browser_close,
    browser_doctor,
]
