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
import json
import os
import re
import shutil
import time
from pathlib import Path
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
    NOTES_DIR,
    OFFLINE_MODE,
    PROVIDER,
    SCREENSHOTS_DIR,
    has_browser_use,
    has_browser_use_cli,
)
from ..prompts import BROWSER_TASK_PREAMBLE
from ..runtime.progress import emit as _progress_emit

# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
_SESSION: Any = None
_SESSION_LOCK = asyncio.Lock()
_LAST_RESULT: dict[str, Any] = {}

# ---------------------------------------------------------------------------
# supervision: supervised tool-request channel
# ---------------------------------------------------------------------------
# Browser agent page content dekh kar kaam karta hai, aur page content attacker
# controlled ho sakta hai (prompt injection). Isliye browser agent ko Jarvis ka
# koi bhi tool directly nahi milta — sirf ye chhoti allowlist `jarvis_tool`
# action ke through, har call audit ke saath. Shell/desktop/browser-control
# tools jaan-boojh kar allowlist me NAHI hain.
SUPERVISED_TOOL_ALLOWLIST = [
    "google_search",
    "fetch_page",
    "get_current_datetime",
    "get_weather",
    "remember_fact",
    "recall_facts",
    "save_note",
    "read_note",
    "search_notes",
    "list_notes",
]
_SUPERVISED_ARGS_MAX_CHARS = 4096
_SUPERVISED_RESULT_MAX_CHARS = 2000
_ALL_TOOLS_CACHE: dict[str, Any] | None = None


def _all_supervised_tools() -> dict[str, Any]:
    """Full Jarvis registry, lazily cached (circular import se bachne ke liye)."""
    global _ALL_TOOLS_CACHE
    if _ALL_TOOLS_CACHE is None:
        from . import tools_for

        found: dict[str, Any] = {}
        for area in ("research", "memory", "desktop"):
            for t in tools_for(area):
                found.setdefault(t.name, t)
        _ALL_TOOLS_CACHE = found
    return _ALL_TOOLS_CACHE


async def _supervisor_tool_call(tool_name: str, arguments_json: str = "{}", _emit=None) -> str:
    """Execute one allowlisted tool on behalf of the browser agent (audited)."""
    name = (tool_name or "").strip()
    audit = _LAST_RESULT.setdefault("tool_requests", [])
    entry: dict[str, Any] = {"name": name, "ok": False, "at": time.strftime("%H:%M:%S")}

    def _fail(msg: str) -> str:
        entry["error"] = msg[:200]
        audit.append(entry)
        return msg

    if name not in SUPERVISED_TOOL_ALLOWLIST:
        allowed = ", ".join(SUPERVISED_TOOL_ALLOWLIST)
        return _fail(f"❌ '{name}' is not a supervised tool. Allowed: {allowed}")
    args_text = arguments_json or "{}"
    if len(args_text) > _SUPERVISED_ARGS_MAX_CHARS:
        return _fail(f"❌ arguments too large ({len(args_text)} chars, max {_SUPERVISED_ARGS_MAX_CHARS}).")
    try:
        args = json.loads(args_text)
    except json.JSONDecodeError as exc:
        return _fail(f"❌ arguments_json is not valid JSON: {exc}")
    if not isinstance(args, dict):
        return _fail("❌ arguments_json must be a JSON object.")
    registry = _all_supervised_tools()
    target = registry.get(name)
    if target is None:
        return _fail(f"❌ supervised tool '{name}' is not registered right now.")
    try:
        if hasattr(target, "ainvoke"):
            result = await target.ainvoke(args)
        elif hasattr(target, "invoke"):
            result = target.invoke(args)
        else:
            result = target(**args)
    except Exception as exc:  # noqa: BLE001 - supervisor must never crash the agent
        return _fail(f"❌ {name} failed: {exc}")
    text = str(result)
    if len(text) > _SUPERVISED_RESULT_MAX_CHARS:
        text = text[:_SUPERVISED_RESULT_MAX_CHARS] + "… (truncated)"
    entry["ok"] = True
    audit.append(entry)
    if _emit is not None:
        _emit("tool_end", f"jarvis_tool:{name}", tool=name, status="ok")
    return text


def record_browser_step(step_no: int, url: str, _emit=None) -> None:
    """Track one browser-agent step: visited URL list + visible progress event."""
    _LAST_RESULT.setdefault("steps", []).append({"step": int(step_no), "url": url or "", "ts": time.time()})
    if _emit is not None and url:
        host = re.sub(r"^https?://", "", url).split("/")[0]
        _emit("thinking", f"browser step {step_no}: {host}", detail=url)


def _run_logs_dir() -> Path:
    override = os.environ.get("JARVIS_BROWSER_RUNS_DIR", "").strip()
    if override:
        return Path(override)
    return Path(NOTES_DIR).parent / "browser_runs"


def _new_run_id(kind: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rand = os.urandom(2).hex()
    return f"br-{kind}-{stamp}-{rand}"


def _write_run_log(run_id: str, record: dict[str, Any]) -> str:
    """Persist one supervision record per browser run; returns file path (or '')."""
    try:
        log_dir = _run_logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        record = {"run_id": run_id, "at": time.strftime("%Y-%m-%d %H:%M:%S"), **record}
        path = log_dir / f"{run_id}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False)[:200_000], encoding="utf-8")
        return str(path)
    except OSError:
        return ""


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
async def get_browser_session(force_new: bool = False):
    global _SESSION
    if _SESSION is not None and not force_new:
        return _SESSION
    async with _SESSION_LOCK:
        if _SESSION is not None and not force_new:
            return _SESSION
        from browser_use import BrowserSession
        from browser_use.browser.profile import BrowserProfile

        profile_kwargs: dict[str, Any] = {
            "headless": BROWSER_HEADLESS,
            "keep_alive": BROWSER_KEEP_ALIVE,
            "minimum_wait_page_load_time": 0.25,
            "wait_for_network_idle_page_load_time": 0.75,
            "viewport": {"width": 1440, "height": 900},
            "highlight_elements": True,
        }
        if BROWSER_USER_DATA_DIR:
            profile_kwargs["user_data_dir"] = BROWSER_USER_DATA_DIR
        if os.environ.get("JARVIS_BROWSER_CHROMIUM_SANDBOX", "0") == "1":
            profile_kwargs["chromium_sandbox"] = True
        else:
            profile_kwargs["chromium_sandbox"] = False  # containers/VMs me zaroori

        _SESSION = BrowserSession(browser_profile=BrowserProfile(**profile_kwargs))
        return _SESSION


async def close_browser_session() -> str:
    global _SESSION
    if _SESSION is None:
        return "ℹ️ Koi browser session khula nahi tha."
    try:
        await _SESSION.kill()
    except Exception:
        try:
            await _SESSION.close()
        except Exception:
            pass
    _SESSION = None
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
def _build_browser_tools(output_model_fields: list[str] | None = None):
    """browser-use `Tools` registry me Jarvis ke apne actions register karo."""
    from browser_use import Tools

    tools = Tools()

    @tools.registry.action("Save a note to disk for the owner (use when the task says to save/note the data).")
    async def jarvis_save_note(name: str, content: str) -> str:
        from .notes_tools import save_note

        try:
            return await save_note.ainvoke({"name": name, "content": content, "open_in_notepad": False})
        except Exception:
            safe_name = re.sub(r"[^\w-]", "_", name)[:60]
            path = NOTES_DIR / f"{safe_name}.md"
            path.write_text(content, encoding="utf-8")
            return f"saved to {path}"

    @tools.registry.action("Remember an important fact about the owner in long-term memory.")
    async def jarvis_remember(fact: str) -> str:
        from ..memory.store import get_memory

        get_memory().remember_fact(fact, tags="browser-agent")
        return f"remembered: {fact[:120]}"

    @tools.registry.action("Report progress to the Jarvis supervisor (use after completing a sub-goal).")
    async def jarvis_progress(note: str) -> str:
        _LAST_RESULT.setdefault("progress", []).append(note)
        return "ok"

    supervised_list = ", ".join(SUPERVISED_TOOL_ALLOWLIST)

    @tools.registry.action(
        "Call a supervised Jarvis tool (read-only/memory only): "
        + supervised_list
        + ". arguments_json must be a JSON object (max 4KB). Anything else is refused."
    )
    async def jarvis_tool(tool_name: str, arguments_json: str = "{}") -> str:
        return await _supervisor_tool_call(tool_name, arguments_json)

    return tools


_SUPERVISOR_CHANNEL_NOTE = (
    "\n\nSupervisor tool channel: besides jarvis_save_note/jarvis_remember/jarvis_progress, "
    "you can call jarvis_tool(tool_name, arguments_json) for these supervised Jarvis tools: "
    + ", ".join(SUPERVISED_TOOL_ALLOWLIST)
    + ". Use it for search/memory/notes instead of guessing. Other tools will be refused."
)


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

    chat = None if (OFFLINE_MODE or PROVIDER == "demo") else get_chat_model("browser")
    return langchain_to_browser_use_llm(chat)


async def build_browser_agent(
    task: str,
    output_fields: list[str] | None = None,
    max_steps: int | None = None,
    start_url: str = "",
    on_step=None,
):
    """Ek taiyaar browser-use Agent banao. None return karega agar LLM available nahi."""
    if not has_browser_use():
        return None
    llm = _browser_llm()
    if llm is None:
        return None

    from browser_use import Agent

    session = await get_browser_session()
    model = _structured_model(output_fields or [])

    full_task = task.strip()
    if start_url:
        full_task = f"Start at {start_url}.\n{full_task}"
    full_task += (
        f"\n\nWhen done, report: (1) exact data found, (2) source URLs, "
        f"(3) anything that failed. Owner: {os.environ.get('JARVIS_OWNER_NAME', 'Nikhil Mehra')}."
    )

    async def _step_hook(agent: Any) -> None:
        try:
            step_n = getattr(agent.state, "n_steps", 0) if hasattr(agent, "state") else 0
            url = ""
            try:
                page = await agent.browser_session.get_current_page()
                url = page.url
            except Exception:
                pass
            entry = {"step": step_n, "url": url, "ts": time.time()}
            record_browser_step(step_n, url, _progress_emit)
            if on_step:
                await on_step(entry)
        except Exception:
            pass

    agent = Agent(
        task=full_task,
        llm=llm,
        browser_session=session,
        tools=_build_browser_tools(output_fields),
        output_model_schema=model,
        max_actions_per_step=4,
        use_vision=True,
        use_thinking=True,
        enable_planning=True,
        extend_system_message=(BROWSER_TASK_PREAMBLE or "") + _SUPERVISOR_CHANNEL_NOTE,
        sensitive_data=_collect_sensitive_data(),
        max_failures=3,
        step_timeout=180,
        generate_gif=False,
        register_new_step_callback=None,
        on_step_end=_step_hook,
    )
    return agent


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
        return f"(http fast-path bhi fail hua: {exc})"


async def run_browser_task(
    task: str,
    start_url: str = "",
    output_fields: list[str] | None = None,
    max_steps: int | None = None,
    save_note_as: str = "",
    timeout: int = BROWSER_TIMEOUT_S,
) -> dict[str, Any]:
    """Ek browser-use agent chalao aur structured result do."""
    run_id = _new_run_id("task")
    _LAST_RESULT.clear()
    _LAST_RESULT.update(
        {"task": task, "started": time.time(), "steps": [], "progress": [], "tool_requests": [], "run_id": run_id}
    )

    agent = None
    try:
        agent = await asyncio.wait_for(build_browser_agent(task, output_fields, max_steps, start_url), timeout=60)
    except Exception as exc:
        agent = None
        _LAST_RESULT["build_error"] = str(exc)

    if agent is None:
        plan = _offline_plan(task, [start_url] if start_url else [])
        fetched = await _http_fallback(task, [start_url] if start_url else [])
        _LAST_RESULT.update({"ok": False, "offline": True})
        run_log = _write_run_log(
            run_id,
            {
                "kind": "task",
                "task": task[:500],
                "offline": True,
                "start_url": start_url,
                "fetched_chars": len(fetched),
            },
        )
        return {
            "ok": False,
            "offline": True,
            "result": plan + "\n\n---\n### 🌐 HTTP fast-path se jo mila (live data):\n" + fetched,
            "urls": [],
            "structured": None,
            "steps": 0,
            "run_id": run_id,
            "run_log": run_log,
        }

    try:
        history = await asyncio.wait_for(agent.run(max_steps=max_steps or BROWSER_MAX_STEPS), timeout=timeout)
    except asyncio.TimeoutError:
        _LAST_RESULT.update({"ok": False, "error": f"timeout {timeout}s"})
        run_log = _write_run_log(
            run_id,
            {
                "kind": "task",
                "task": task[:500],
                "ok": False,
                "error": f"timeout {timeout}s",
                "steps": _LAST_RESULT.get("steps", [])[-20:],
            },
        )
        return {
            "ok": False,
            "result": f"⏱️ Browser agent {timeout}s me complete nahi hua (max_steps={max_steps or BROWSER_MAX_STEPS}).",
            "urls": [],
            "structured": None,
            "steps": len(_LAST_RESULT.get("steps", [])),
            "run_id": run_id,
            "run_log": run_log,
        }
    except Exception as exc:
        _LAST_RESULT.update({"ok": False, "error": str(exc)})
        run_log = _write_run_log(
            run_id,
            {
                "kind": "task",
                "task": task[:500],
                "ok": False,
                "error": str(exc)[:500],
                "steps": _LAST_RESULT.get("steps", [])[-20:],
            },
        )
        return {
            "ok": False,
            "result": f"❌ Browser agent error: {exc}",
            "urls": [],
            "structured": None,
            "steps": len(_LAST_RESULT.get("steps", [])),
            "run_id": run_id,
            "run_log": run_log,
        }

    final = ""
    structured = None
    urls: list[str] = []
    try:
        final = history.final_result() or ""
    except Exception:
        pass
    try:
        urls = history.urls() or []
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
        "ok": bool(final),
        "result": final or "(agent ne koi final result nahi diya)",
        "urls": urls[:12],
        "structured": structured,
        "steps": len(_LAST_RESULT.get("steps", [])),
        "notes_saved": "",
        "run_id": run_id,
        "run_log": "",
    }

    if save_note_as:
        try:
            from .notes_tools import save_note

            body = payload["result"]
            if structured:
                body += (
                    "\n\n## Structured data (JSON)\n```json\n"
                    + json.dumps(structured, indent=2, ensure_ascii=False)
                    + "\n```"
                )
            if urls:
                body += "\n\n## Sources\n" + "\n".join(f"- {u}" for u in urls[:12])
            payload["notes_saved"] = await save_note.ainvoke(
                {"name": save_note_as, "content": body, "tags": "browser-use"}
            )
        except Exception as exc:
            payload["notes_saved"] = f"(note save fail: {exc})"

    payload["run_log"] = _write_run_log(
        run_id,
        {
            "kind": "task",
            "task": task[:500],
            "ok": payload["ok"],
            "urls": urls[:12],
            "tool_requests": _LAST_RESULT.get("tool_requests", []),
            "steps": _LAST_RESULT.get("steps", [])[-20:],
        },
    )
    _LAST_RESULT.update({"ok": payload["ok"], "result": payload["result"][:2000]})
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
        parts += [
            "",
            "## Structured output",
            "```json",
            json.dumps(outcome["structured"], indent=2, ensure_ascii=False)[:3000],
            "```",
        ]
    if outcome.get("urls"):
        parts += ["", "## Pages visited"] + [f"- {u}" for u in outcome["urls"][:10]]
    parts.append(f"\n⏱️ steps: {outcome.get('steps', 0)}")
    if outcome.get("run_id"):
        parts.append(f"🧾 run: {outcome['run_id']}")
    if outcome.get("notes_saved"):
        parts.append(f"📝 {outcome['notes_saved']}")
    return "\n".join(parts)


@tool
async def browser_research(
    task: str,
    sites: list[str] | None = None,
    fields: list[str] | None = None,
    save_note_as: str = "",
    max_parallel: int = 0,
) -> str:
    """MULTI-SITE parallel research: har website ke liye alag browser agent ek saath chalta hai.

    Ye owner ke "alag alag websites se xyz info laao" wale task ka asli tool hai.
    task: kya dhoondhna hai (natural language).
    sites: list of URLs/domains (khali chhodo to search se top sites khud chun lega).
    fields: har site se nikalne wale fields, e.g. ["price","rating","contact_email"].
    save_note_as: combined JSON+report note me save ho jayega.
    """
    max_parallel = max_parallel or BROWSER_MAX_PARALLEL
    target_sites = [s.strip() for s in (sites or []) if s and s.strip()]
    research_run_id = _new_run_id("research")

    if not target_sites:
        from .web_tools import web_search_results

        results = await web_search_results(task, num=5)
        target_sites = [r["url"] for r in results if r.get("url")][:max_parallel]
        if not target_sites:
            return "❌ Koi site nahi mili research ke liye — sites explicitly do."

    sem = asyncio.Semaphore(max(1, max_parallel))

    async def _one(site: str) -> dict[str, Any]:
        async with sem:
            sub_task = f"{task}\nFocus only on this site: {site}. Extract the requested fields if present."
            outcome = await run_browser_task(
                task=sub_task,
                start_url=site,
                output_fields=fields or [],
                max_steps=BROWSER_MAX_STEPS,
            )
            outcome["site"] = site
            return outcome

    started = time.time()
    outcomes = await asyncio.gather(*(_one(s) for s in target_sites), return_exceptions=True)
    elapsed = time.time() - started

    blocks: list[str] = []
    structured_rows: list[dict[str, Any]] = []
    for site, out in zip(target_sites, outcomes, strict=True):
        if isinstance(out, Exception):
            blocks.append(f"### ❌ {site}\n{out}")
            continue
        blocks.append(f"### ✅ {site}\n{str(out.get('result', ''))[:2500]}")
        if out.get("structured"):
            row = {"site": site, **out["structured"]}
            structured_rows.append(row)

    # DSPy/LLM se normalization (agar available) — warna raw rows
    if structured_rows and not (OFFLINE_MODE or PROVIDER == "demo"):
        try:
            from ..dspy_engine.modules import normalize_rows

            norm = await asyncio.to_thread(normalize_rows, structured_rows, fields or [])
            if norm:
                structured_rows = norm
        except Exception:
            pass

    _write_run_log(
        research_run_id,
        {
            "kind": "research",
            "task": task[:500],
            "sites": target_sites[:12],
            "fields": fields or [],
            "elapsed_s": round(elapsed, 1),
            "ok_sites": sum(1 for o in outcomes if not isinstance(o, Exception)),
        },
    )
    report = [
        f"🌐 **Multi-site research** — {len(target_sites)} sites, {len(outcomes)} agents, {elapsed:.1f}s (parallel)",
        "=" * 50,
        "\n\n".join(blocks)[:9000],
        f"\n🧾 run: {research_run_id}",
    ]
    if structured_rows:
        report += [
            "",
            "## Combined structured data (JSON)",
            "```json",
            json.dumps(structured_rows, indent=2, ensure_ascii=False)[:4000],
            "```",
        ]
    if save_note_as:
        try:
            from .notes_tools import save_note

            body = (
                "\n".join(report)
                + "\n\n## Raw JSON\n```json\n"
                + json.dumps(structured_rows, indent=2, ensure_ascii=False)
                + "\n```"
            )
            saved = await save_note.ainvoke(
                {"name": save_note_as, "content": body, "tags": "browser-research,multi-site"}
            )
            report.append(f"\n📝 {saved}")
        except Exception as exc:
            report.append(f"\n(note save fail: {exc})")
    return "\n".join(report)


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
    cli_run_id = _new_run_id("cli")
    _write_run_log(
        cli_run_id,
        {
            "kind": "cli",
            "code": code[:1000],
            "exit": proc.returncode,
            "stdout_chars": len(stdout),
            "stderr_chars": len(stderr),
        },
    )
    body = f"$ browser-use <<'PY'\n{code[:600]}\nPY\n[exit {proc.returncode}]\n{stdout[-4000:]}"
    if stderr.strip():
        body += f"\n--- stderr ---\n{stderr[-1500:]}"
    body += f"\n🧾 run: {cli_run_id}"
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
        safe_shot = re.sub(r"[^\w-]", "_", name)[:40]
        path = SCREENSHOTS_DIR / f"{safe_shot}_{int(time.time())}.png"
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
        f"• supervised tools: {len(SUPERVISED_TOOL_ALLOWLIST)} (jarvis_tool allowlist)",
        f"• run logs        : {_run_logs_dir()}",
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
