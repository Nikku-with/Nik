"""
jarvis/graph/nodes.py — Supervisor ke saare nodes (workers included)
==================================================================

Graph ka flow:

    START → recall → route ─┬─(clarify)──────────────→ respond → END
                            └─(plan) → dispatch ─┬→ browser_worker ─┐
                                                  ├→ research_worker ┤
                                                  ├→ code_worker     ├→ synthesize → critic ─┬→ persist → respond → END
                                                  ├→ desktop_worker  │                          └→ refine ─(wapis dispatch)
                                                  └→ memory_worker ──┘

Har worker apna kaam karke `findings` me likhta hai. Workers ek saath (parallel) chalte hain.
Critic answer ko check karta hai; gap mile to `refine` dobara dispatch karta hai (max iterations).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from ..config import FILE_SEARCH_ROOTS, MAX_GRAPH_ITERATIONS, OFFLINE_MODE, OWNER_NAME, PROVIDER
from ..dspy_engine import modules as dspy_mods
from ..llm import get_chat_model
from ..memory.session_state import get_state
from ..runtime.evidence import (
    claims_execution,  # noqa: F401
    claims_without_support,
    detect_blind_handoffs,
    evidence_records,
    session_evidence_summary,
    supporting_records,
    verify_execution,
)
from ..runtime.progress import emit
from ..memory.store import get_memory, owner_profile_block
from ..skills.loader import skills_prompt_block
from ..prompts import SUPERVISOR_SYSTEM, supervisor_prompt
from .state import JarvisState, evidence_digest


def _event(state: JarvisState, node: str, detail: str = "", **extra: Any) -> dict[str, Any]:
    return {"node": node, "detail": detail, "ts": time.time(), **extra}


# ===========================================================================
# 1. RECALL — memory se context
# ===========================================================================
async def recall_node(state: JarvisState) -> dict[str, Any]:
    request = state.get("request", "")
    mem = get_memory()
    try:
        facts = owner_profile_block()
        ctx = mem.context_block(limit=8, session=state.get("session_id"))
        hits = mem.search(request, limit=5)
        hit_text = "\n".join(f"- [{h.kind}] {h.text[:200]}" for h in hits) if hits else "(kuch match nahi)"
    except Exception as exc:
        facts, ctx, hit_text = f"(memory error: {exc})", "", ""

    mem.add_episode("user", request, session=state.get("session_id", "default"), importance=0.6)

    # short-term state (jarvis_memory.json): task start + last error context
    short_term = ""
    try:
        st = get_state()
        st.add_task(request, status="running")
        short_term = st.context_block(max_tasks=4)
    except Exception:
        short_term = ""

    return {
        "owner_facts": facts,
        "memory_context": f"{ctx}\n\nRelevant memory:\n{hit_text}\n\nShort-term state:\n{short_term}",
        "events": [_event(state, "recall", f"{len(hit_text)} chars memory context")],
    }


# ===========================================================================
# 2. ROUTE — kaun se workers chahiye (DSPy, ultra-fast)
# ===========================================================================
async def route_node(state: JarvisState) -> dict[str, Any]:
    request = state.get("request", "")
    history = state.get("memory_context", "")
    decision = await asyncio.to_thread(dspy_mods.route, request, history)
    workers = decision.get("workers") or ["answer"]
    return {
        "workers": workers,
        "route_reason": decision.get("reason", ""),
        "needs_clarification": bool(decision.get("needs_clarification")),
        "events": [_event(state, "route", f"workers={workers} • {decision.get('reason', '')[:120]}")],
    }


async def clarify_node(state: JarvisState) -> dict[str, Any]:
    """Ambiguous request → owner se ek short clarifying sawaal (guess karne se behtar)."""
    request = state.get("request", "")
    answer = (
        f"बॉस, ek chhoti si clarification chahiye — _{request.strip()}_\n\n"
        "Kis cheez ka exact output chahiye (file/note/json/answer), aur kaunsi sites/inputs use karun? "
        "Bata dijiye, main turant chala deta hoon. 🙌"
    )
    return {
        "answer": answer,
        "spoken": answer,
        "status": "needs_clarification",
        "events": [_event(state, "clarify", "owner se clarification maangi")],
    }


# ===========================================================================
# 3. PLAN — executable steps (DSPy)
# ===========================================================================
async def plan_node(state: JarvisState) -> dict[str, Any]:
    request = state.get("request", "")
    workers = state.get("workers", [])
    skills = ""
    try:
        skills = skills_prompt_block(request)  # owner ke bataye workflows (SKILL.md)
    except Exception:
        skills = ""
    context = f"{state.get('owner_facts', '')}\n{state.get('memory_context', '')}"
    if skills:
        context += f"\n\n{skills}"
    emit("thinking", f"Plan bana raha hoon — workers: {', '.join(workers) or 'default'}")
    result = await asyncio.to_thread(dspy_mods.plan, request, workers, context[:6000])
    steps = result.get("steps") or []
    plan_errors: list[str] = []
    if not steps and [w for w in workers if w in WORKER_NODES]:
        # anti-lazy: planner ne 0 steps diye par workers chalne wale hain — labeled fallback
        steps = ["request ko seedha execute karo (planner ne steps nahi diye — fallback)"]
        plan_errors.append("planner gave 0 steps — fallback single-step plan used")
        emit("error", "planner ne khaali plan diya — fallback step se chala rahe hain")
    if steps:
        emit("plan", f"{len(steps)}-step plan ready", detail=" → ".join(str(x)[:60] for x in steps[:5]))
    task_id = get_memory().start_task(request, route=",".join(workers))
    try:
        get_state().set_pending_steps(steps)
    except Exception:
        pass
    return {
        "plan": steps,
        "deliverable": result.get("deliverable", ""),
        "task_id": task_id,
        "errors": plan_errors,
        "events": [_event(state, "plan", f"{len(steps)} steps" + (" +skill" if skills else ""), steps=steps)],
    }


# ===========================================================================
# 4. WORKERS
# ===========================================================================
def _finding(worker: str, summary: str, **extra: Any) -> dict[str, Any]:
    # anti-lazy: khaali summary ko label karke mark karo (chup-chaap pass nahi hoga)
    text = (summary or "").strip()
    if not text:
        text = "(khali — worker ne kuch report nahi kiya)"
        extra.setdefault("lazy", True)
    return {"worker": worker, "summary": text[:12000], "ts": time.time(), **extra}


def _extract_urls(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s,)\"']+", text or "")
    domains = re.findall(r"\b(?:[a-z0-9-]+\.)+(?:com|in|org|net|io|ai|co|dev|gov|edu)\b", text or "", re.I)
    out: list[str] = []
    for u in urls + domains:
        u = u.rstrip(".,)")
        if u not in out:
            out.append(u)
    return out[:8]


async def browser_worker(state: JarvisState) -> dict[str, Any]:
    """browser-use agent(s) ko Jarvis ka prompt do → asli browser me kaam karwao."""
    request = state.get("request", "")
    plan_txt = " → ".join(state.get("plan", [])[:5])
    sites = _extract_urls(request)
    started = time.time()

    spec = await asyncio.to_thread(dspy_mods.website_task_spec, request, sites)
    browser_task_prompt = spec.get("browser_task") or request
    fields = spec.get("fields") or []
    save_as = spec.get("save_note_as") or ""

    if "note" in request.lower() or "note pad" in request.lower() or "notepad" in request.lower():
        if not save_as:
            m = re.search(r"(\w[\w\s]{2,40}?)\s*(?:naam|name)\s*(?:se|rakh)", request, re.I)
            save_as = m.group(1).strip().replace(" ", "_") if m else "jarvis_research"

    from ..tools.browser_tools import browser_research, run_browser_task

    if sites and len(sites) >= 2:
        # multi-site → parallel browser agents (ek saath, semaphore se bounded)
        result_text = await browser_research.ainvoke(
            {"task": browser_task_prompt, "sites": sites, "fields": fields, "save_note_as": save_as}
        )
        outcome = {"ok": True, "result": str(result_text), "sites": sites, "worker": "browser"}
    else:
        outcome = await run_browser_task(
            task=browser_task_prompt,
            start_url=sites[0] if sites else "",
            output_fields=fields,
            save_note_as=save_as,
        )
        outcome["sites"] = sites

    artifacts = []
    if outcome.get("structured"):
        try:
            from ..config import ARTIFACTS_DIR

            path = ARTIFACTS_DIR / f"browser_{int(time.time())}.json"
            path.write_text(json.dumps(outcome["structured"], indent=2, ensure_ascii=False), encoding="utf-8")
            artifacts.append(str(path))
        except Exception:
            pass

    summary = f"task: {browser_task_prompt[:300]}\nplan: {plan_txt}\n\nresult:\n{str(outcome.get('result'))[:6000]}"
    if outcome.get("notes_saved"):
        summary += f"\n\nnote: {outcome['notes_saved']}"

    # --- execution evidence (hard invariant): kaunsa asli tool chala, kya result aaya ---
    browser_tool = "browser_research" if (sites and len(sites) >= 2) else "run_browser_task"
    ok = bool(outcome.get("ok"))
    evidence = {
        "name": browser_tool,
        "args": {"task": browser_task_prompt[:200], "sites": sites, "fields": fields},
        "output": str(outcome.get("result") or outcome.get("error") or "")[:500],
        "ok": ok,
        "source": "worker",
        "seconds": round(time.time() - started, 1),
    }
    if not ok and outcome.get("error"):
        evidence["error"] = str(outcome.get("error"))[:200]

    update: dict[str, Any] = {
        "findings": [
            _finding(
                "browser",
                summary,
                sites=sites,
                artifacts=artifacts,
                offline=bool(outcome.get("offline")),
                seconds=round(time.time() - started, 1),
            )
        ],
        "artifacts": artifacts,
        # browser = tool-required worker: bina is record ke success claim allowed nahi
        "execution_required": True,
        "tool_evidence": [evidence],
        "events": [
            _event(state, "browser_worker", f"{time.time() - started:.1f}s • {len(sites)} sites • {browser_tool}={ok}")
        ],
    }
    if outcome.get("notes_saved"):
        update["notes_saved"] = [save_as or "browser_result"]
    if not outcome.get("ok") and outcome.get("error"):
        update["errors"] = [f"browser: {outcome['error']}"]
    return update


async def research_worker(state: JarvisState) -> dict[str, Any]:
    """Web search → parallel page fetch → DSPy structured extraction."""
    request = state.get("request", "")
    started = time.time()
    sites = _extract_urls(request)

    from ..dspy_engine.modules import extract_many
    from ..tools.web_tools import web_search_results, fetch_url_text

    fields = _guess_fields(request)
    rows: list[dict[str, Any]] = []
    sources: list[str] = []
    search_rows: list[dict[str, Any]] = []

    if sites:
        sources = sites
        pairs: list[tuple[str, str]] = []

        async def _fetch(u: str):
            try:
                return u, await fetch_url_text(u if u.startswith("http") else f"https://{u}", max_chars=10000)
            except Exception as exc:
                return u, f"[FETCH FAILED: {exc}]"

        pairs = list(await asyncio.gather(*(_fetch(s) for s in sites[:6])))
    else:
        search_rows = await web_search_results(request, num=6)
        top = [r for r in search_rows if r.get("url")][:5]
        sources = [r["url"] for r in top]

        # parallel fetch (sasta fast path) — browser se pehle
        async def _fetch_r(u: str):
            try:
                return u, await fetch_url_text(u, max_chars=8000)
            except Exception as exc:
                return u, f"[FETCH FAILED: {exc}]"

        pairs = list(await asyncio.gather(*(_fetch_r(u) for u in sources)))

    good_pairs = [(u, t) for u, t in pairs if t and "FETCH FAILED" not in t[:40]]
    if fields and good_pairs:
        try:
            rows = await extract_many(good_pairs[:6], fields)
        except Exception:
            rows = []

    digest_lines = []
    for i, (u, t) in enumerate(pairs, 1):
        digest_lines.append(f"[{i}] {u}\n{t[:2200]}")
    digest = "\n\n".join(digest_lines)

    search_table = ""
    if search_rows:
        search_table = "\n".join(f"- {r['title']} — {r['url']}" for r in search_rows[:6])

    summary = f"query: {request[:200]}\nsources fetched: {len(pairs)} (parallel)\n{search_table}\n\n{digest[:6000]}" + (
        "\n\nstructured rows:\n" + json.dumps(rows, indent=2, ensure_ascii=False)[:2500] if rows else ""
    )

    fetched_ok = len(good_pairs)
    researched_ok = bool(fetched_ok or search_rows)
    evidence = {
        "name": "web_search_results+fetch_url_text",
        "args": {"query": request[:200], "sites": sites[:6]},
        "output": (
            f"search_rows={len(search_rows)} pages_fetched_ok={fetched_ok}/{len(pairs)}"
            + (f" first={sources[0]}" if sources else "")
        ),
        "ok": researched_ok,
        "source": "worker",
        "seconds": round(time.time() - started, 1),
    }
    return {
        "findings": [_finding("research", summary, sites=sources, rows=rows, seconds=round(time.time() - started, 1))],
        # read-only research: claim "maine dhundh liya" tabhi verify hota hai jab fetch/search asli chala ho
        "tool_evidence": [evidence],
        "events": [_event(state, "research_worker", f"{len(pairs)} pages in {time.time() - started:.1f}s")],
    }


def _guess_fields(request: str) -> list[str]:
    """Request me kaun kaun se data point maange gaye — quick heuristic."""
    low = request.lower()
    fields: list[str] = []
    for key, aliases in {
        "price": ("price", "rate", "kitna", "cost", "₹"),
        "title": ("title", "naam", "name"),
        "rating": ("rating", "review", "stars"),
        "contact": ("contact", "email", "phone", "number"),
        "address": ("address", "pata", "location"),
        "date": ("date", "kab", "deadline"),
    }.items():
        if any(a in low for a in aliases):
            fields.append(key)
    if "price" in fields and "product" not in fields:
        fields.insert(0, "product")
    return fields[:6]


async def code_worker(state: JarvisState) -> dict[str, Any]:
    """Code likho → chalao → error par khud fix karo (self-healing)."""
    request = state.get("request", "")
    started = time.time()
    from ..tools.code_engine import code_engine_run

    outcome = await code_engine_run(task=request, language="python", run=True)
    summary = (
        f"task: {request[:200]}\n"
        f"script: {outcome.get('script')}\n"
        f"attempts: {outcome.get('attempts')}\n"
        f"ok: {outcome.get('ok')}\n\n"
        f"{outcome.get('report', '')[:5000]}"
    )
    code_evidence = [
        {
            "name": "code_engine",
            "args": {"task": request[:200], "language": "python"},
            "output": str(outcome.get("report", ""))[:800] or "(koi output nahi)",
            "ok": bool(outcome.get("ok")),
            "source": "deterministic",
        }
    ]
    return {
        "findings": [
            _finding(
                "code",
                summary,
                artifacts=outcome.get("artifacts", []),
                seconds=round(time.time() - started, 1),
                ok=outcome.get("ok"),
                tool_calls=code_evidence,
            )
        ],
        "artifacts": outcome.get("artifacts", []),
        "execution_required": True,  # code worker hamesha asli execution hai
        "tool_evidence": code_evidence,
        "events": [_event(state, "code_worker", f"{outcome.get('attempts')} attempts • ok={outcome.get('ok')}")],
    }


async def desktop_worker(state: JarvisState) -> dict[str, Any]:
    """Local machine par kaam: apps, files, notes/notepad — ek ReAct sub-agent ke saath.

    Verification: agar request me asli action maanga gaya hai par sub-agent ne koi tool call
    NAHI kiya (LLM hawa me "kar diya" bol deta hai), to hum khud deterministic executor se
    wo action chalate hain. Jo bhi hua, wahi `actions_done` / `action_failures` me jaata hai —
    aur synthesize me honesty guard lagta hai.
    """
    request = state.get("request", "")
    started = time.time()
    tools = _desktop_tools()

    # --- 1) action pehle se detect karo (LLM se pehle) ---
    action = detect_desktop_action(request)
    needs_action = bool(action.get("actions")) or _needs_action(request)

    run = await _run_react_subagent(
        role="desktop",
        task=(
            f"Owner ({OWNER_NAME}) ki request: {request}\n"
            "Local machine ke tools use karo: apps kholna/band karna, files dhoondna/padhna, notes save karna, "
            "system maintenance (refresh/status/process clear), aur scheduled automations banana. "
            "Confirm karo kya kya actually hua — guess mat karo. Agar tool call nahi kiya to ye mat likho ki kaam ho gaya."
        ),
        tools=tools,
        raw_request=request,
    )
    calls = list(run.calls)
    forced: list[dict[str, Any]] = []

    # --- 2) verified execution: action *attempt* hi nahi hua to khud chalao ---
    # (dhyaan: fail hui call bhi "attempt" hai — warna same kaam do baar chal jata,
    #  jaise Windows par notepad do baar khulna aur text do baar likhna)
    attempted_tools = {c.get("name") for c in calls}
    pending_actions = {
        "actions": [
            a
            for a in action.get("actions", [])
            if not (set(_ACTION_TOOL_NAMES.get(a.get("kind"), ())) & attempted_tools)
        ]
    }
    if pending_actions["actions"]:
        forced = await _execute_desktop_action(pending_actions, tools)
        calls.extend(forced)

    done = [c for c in calls if c.get("ok")]
    failed = [c for c in calls if not c.get("ok")]

    notes: list[str] = []
    low_r = request.lower()
    wants_note = any(
        k in low_r
        for k in (
            "save kar",
            "save kardo",
            "save krdo",
            "likh do",
            "likh de",
            "notepad me",
            "note me save",
            "note bana",
            "note banao",
        )
    )
    if wants_note and ("save_note" in run.text or "Note save ho gaya" in run.text):
        notes = [f"desktop_{int(time.time())}"]

    # --- 3) finding me sirf verified facts ---
    body = run.text
    if forced:
        body += (
            "\n\n[verified execution] sub-agent ne tool call nahi kiya tha, isliye maine khud ye chalaya:\n"
            + "\n".join(SubAgentRun(calls=forced).lines(limit=8))
        )
    if done:
        body += "\n\n[verified actions]\n" + "\n".join(SubAgentRun(calls=done).lines(limit=8))
    if failed:
        body += "\n\n[FAILED actions]\n" + "\n".join(SubAgentRun(calls=failed).lines(limit=8))

    # jab request action wali thi par kuch bhi nahi chala — sub-agent ka text unreliable hai
    if needs_action and not calls:
        body = (
            f"⚠️ VERIFICATION FAILED: is request ke liye koi tool call nahi hua (sub-agent ka text: "
            f"{run.text[:300]})\n"
            "Matlab: machine par kuch bhi nahi hua. Owner ko sach batao, kaam hua nahi."
        )

    finding = _finding(
        "desktop",
        body[:6000],
        seconds=round(time.time() - started, 1),
        actions=[{"name": c["name"], "ok": c["ok"]} for c in calls][:12],
        tool_calls=calls[:24],
    )
    return {
        "findings": [finding],
        "notes_saved": notes,
        "action_required": needs_action,
        "execution_required": needs_action,
        "tool_evidence": calls[:24],  # asli tool results — honesty_guard ka evidence
        "actions_done": done,
        "action_failures": failed,
        "events": [
            _event(
                state,
                "desktop_worker",
                f"{time.time() - started:.1f}s • {len(tools)} tools • "
                f"calls={len(calls)} ok={len(done)} fail={len(failed)}",
            )
        ],
    }


async def memory_worker(state: JarvisState) -> dict[str, Any]:
    """Facts yaad rakhna / recall karna."""
    request = state.get("request", "")
    mem = get_memory()
    stored = ""
    recalled = ""
    low = request.lower()
    try:
        if any(k in low for k in ("yaad rakh", "remember", "note kar le")):
            fact = re.sub(
                r"(yaad rakhna|yaad rakho|yaad rakh|remember karo|remember|note kar le)", "", request, flags=re.I
            ).strip(" ,।:-")
            mem.remember_fact(fact or request, tags="owner,explicit")
            stored = f"stored: {fact or request}"
        if any(k in low for k in ("yaad hai", "kya pata", "recall", "bata mujhe", "bhool")):
            hits = mem.search(request, limit=6)
            recalled = "\n".join(f"- [{h.kind}] {h.text[:200]}" for h in hits) or "(kuch nahi mila)"
    except Exception as exc:
        return {"errors": [f"memory: {exc}"], "findings": [_finding("memory", f"error: {exc}")]}

    summary = f"request: {request[:200]}\n{stored}\n{recalled}".strip()
    return {
        "findings": [_finding("memory", summary or "memory me koi change nahi")],
        "events": [_event(state, "memory_worker", "memory handled")],
    }


def _desktop_tools() -> list:
    """Desktop sub-agent ke tools: local machine + REPL + session state + system ops + automations."""
    from ..tools import tools_for

    return tools_for("desktop", "runtime", "ops")


# ===========================================================================
# DETERMINISTIC DESKTOP ACTION ENGINE
# ---------------------------------------------------------------------------
# Kyun: LLM kabhi "notepad khol diya, likh diya" bol deta hai jabki tool call
# hua hi nahi (ya tool ne ❌ return kiya). Isliye:
#   1. action wali request detect karte hain
#   2. sub-agent ne tool call NAHI kiya → hum khud seedha tool chalate hain
#   3. jo hua wahi state me jaata hai (actions_done / action_failures)
#   4. answer me jhooth nahi ja sakta — synthesize me honesty guard lagta hai
# ===========================================================================
_ACTION_KEYS = (
    "open",
    "kholo",
    "khol do",
    "khol",
    "launch",
    "chalu kar",
    "chala do",
    "start karo",
    "band kar",
    "close",
    "likho",
    "likh do",
    "type kar",
    "type karo",
    "likhde",
    "likh de",
    "screenshot",
    "screen shot",
    "hotkey",
    "ctrl+",
    "alt+",
    "volume",
    "aawaz",
    "sunna",
    "minimize",
    "maximize",
    "focus",
    "click",
    "dabao",
    "display",
    "screen par",
    "screen pe",
    "kya dikh",
    "dikha raha",
    "describe",
)
_OPEN_VERBS = r"(?:open|kholo|khol\s*do|khol|launch|chalu\s*kar\w*|chala\s*do|start\s*kar\w*)"
_TYPE_VERBS = r"(?:likh\w*|type\s*kar\w*|type|likh\s*de|likh\s*do)"
_CONNECTORS = (
    r"\b(?:krke|karke|ke\s*baad|then|aur|and|usme|usmein|us\s*me|me|mein|upar|par|pe|"
    r"phir|so|ab)\b"
)


def detect_desktop_action(request: str) -> dict[str, Any]:
    """Request se actions nikalo (order ke saath). Kuch na mile to {} — tab AI normal kaam karega."""
    t = (request or "").strip()
    low = t.lower()
    if not low:
        return {}
    if not any(k in low for k in _ACTION_KEYS):
        return {}

    actions: list[dict[str, Any]] = []

    # app open: "notepad open krke", "chrome kholo", "vs code chala do"
    m_open = re.search(rf"\b([a-z0-9][a-z0-9 ._+-]{{1,28}}?)\s+{_OPEN_VERBS}\b", low)
    if m_open:
        # sirf aage-peeche punctuation hatao — `strip(" ,.yehuthis")` bug tha:
        # wo "chrome" ko "chrom" aur "vs code" ko "vs cod" bana deta tha.
        app = m_open.group(1).strip(" ,.-")
        app = re.sub(r"^(?:jarvis|bhai|boss|please|plz|mera|meri|ye|yeh|wo|the|a|an)\s+", "", app).strip()
        if app and app not in {"kuch", "kaam", "isse", "isko"}:
            actions.append({"kind": "open_app", "app": app[:40]})

    # text typing: "... likho: TEXT" / "... TEXT likho" / "type karo TEXT"
    m_type_sep = re.search(rf"{_TYPE_VERBS}\s*[:\-]\s*(.+)$", t, re.I)
    m_type_tail = re.search(rf"(.+?)\s+{_TYPE_VERBS}\s*$", t, re.I)
    typed = ""
    if m_type_sep:
        typed = m_type_sep.group(1)
    elif m_type_tail:
        typed = m_type_tail.group(1)
        # pehle wale open-clause ko hatao: "notepad open krke i hate you" → "i hate you"
        typed = re.sub(rf"^.*?{_OPEN_VERBS}\b", "", typed, flags=re.I)
        typed = re.sub(rf"^[\s,]+(?:{_CONNECTORS.strip('()?')})[\s,]+", "", typed, flags=re.I)
        typed = re.sub(rf"^(?:{_CONNECTORS.strip('()?')})[\s,]+", "", typed, flags=re.I)
    typed = (typed or "").strip(" .,\"'“”‘’")
    if typed and len(typed) >= 1 and ("likh" in low or "type" in low):
        actions.append(
            {
                "kind": "type_text",
                "text": typed[:400],
                "press_enter": bool(re.search(r"(enter|⏎|nayi line|new line)", low)),
            }
        )

    # screenshot
    if "screenshot" in low or "screen shot" in low:
        actions.append({"kind": "screenshot"})

    # screen describe: "screen par kya hai" / "kya dikh raha hai" / "screen describe karo"
    if ("screen" in low or "display" in low) and any(
        k in low for k in ("kya", "dikha", "dikh", "describe", "bata", "padh", "likha")
    ):
        q = t.strip()
        actions.append({"kind": "describe_screen", "question": q[:200]})

    # mouse click: "500 300 par click karo" / "click at 500,300" / "(500,300) dabao"
    m_click = re.search(r"(\d{1,5})\s*[,\s]\s*(\d{1,5})", low)
    if m_click and any(k in low for k in ("click", "dabao", "daba")):
        actions.append(
            {
                "kind": "click",
                "x": int(m_click.group(1)),
                "y": int(m_click.group(2)),
                "button": ("right" if "right" in low else "left"),
                "clicks": 2 if "double" in low else 1,
            }
        )

    # hotkey: "ctrl+s dabao", "alt+f4"
    m_hk = re.search(r"\b((?:ctrl|alt|shift|win|cmd|command|super)(?:\s*\+\s*[a-z0-9]+)+)\b", low)
    if m_hk:
        actions.append({"kind": "hotkey", "keys": m_hk.group(1).replace(" ", "")})

    # volume
    if "volume" in low or "aawaz" in low:
        if any(k in low for k in ("mute", "band", "silent")):
            actions.append({"kind": "volume", "action": "mute"})
        elif any(k in low for k in ("badha", "up", "tez", "increase", "zyada")):
            actions.append({"kind": "volume", "action": "up"})
        elif any(k in low for k in ("kam", "down", "decrease", "ghata")):
            actions.append({"kind": "volume", "action": "down"})

    # close app: "notepad band karo"
    m_close = re.search(r"\b([a-z0-9][a-z0-9 ._+-]{1,24}?)\s*(?:band|close|kill)\w*", low)
    if m_close and any(k in low for k in ("band", "close", "kill")):
        app = m_close.group(1).strip(" ,.")
        if app and app not in {"ye", "yeh", "wo", "isko", "isse", "sab"}:
            actions.append({"kind": "close_app", "app": app[:40]})

    # dedupe (same kind + same key) order preserve
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for a in actions:
        key = f"{a['kind']}:{a.get('app') or a.get('text') or a.get('keys') or a.get('action')}"
        if key not in seen:
            seen.add(key)
            unique.append(a)
    return {"actions": unique} if unique else {}


async def _execute_desktop_action(action: dict[str, Any], tools: list) -> list[dict[str, Any]]:
    """Deterministic action ko seedha tool se chalao (LLM ke bharose nahi)."""
    by_name = {getattr(t, "name", ""): t for t in (tools or [])}
    records: list[dict[str, Any]] = []

    async def call(name: str, args: dict) -> None:
        t = by_name.get(name)
        if t is None:
            emit("error", f"Tool '{name}' is worker me nahi mila", tool=name, status="fail")
            records.append(
                {"name": name, "args": args, "ok": False, "output": f"(tool '{name}' is worker me available nahi hai)"}
            )
            return
        emit("tool_start", f"{name} chala raha hoon", tool=name, detail=str(args)[:160])
        _t0 = time.time()
        try:
            out = await t.ainvoke(args)
            ok = not str(out).lstrip().startswith(("❌", "⚠️", "🚫"))
            emit(
                "tool_end",
                f"{name}: {str(out)[:100]}",
                tool=name,
                ms=int((time.time() - _t0) * 1000),
                status="ok" if ok else "fail",
                detail=str(out)[:300],
            )
            rec = _tool_call_record(name, args, str(out))
            rec["source"] = "deterministic"  # LLM se nahi, verification loop se chala
            records.append(rec)
        except Exception as exc:
            emit(
                "error", f"{name} fail: {str(exc)[:140]}", tool=name, status="fail", ms=int((time.time() - _t0) * 1000)
            )
            records.append(
                {"name": name, "args": args, "ok": False, "output": f"error: {exc}", "source": "deterministic"}
            )

    for a in action.get("actions") or []:
        kind = a.get("kind")
        if kind == "open_app":
            target = a.get("app") or ""
            # app search based tool pehle (kisi bhi naam se), warna registry wala open_app
            if "open_any_app" in by_name:
                await call("open_any_app", {"query": target})
            else:
                await call("open_app", {"app_title": target})
        elif kind == "type_text":
            await call("type_text", {"text": a.get("text", ""), "press_enter": bool(a.get("press_enter"))})
        elif kind == "hotkey":
            await call("press_hotkey", {"keys": a.get("keys", "")})
        elif kind == "screenshot":
            await call("take_screenshot", {})
        elif kind == "describe_screen":
            await call("describe_screen", {"question": a.get("question", "")})
        elif kind == "click":
            await call(
                "click_at",
                {
                    "x": a.get("x", 0),
                    "y": a.get("y", 0),
                    "button": a.get("button", "left"),
                    "clicks": a.get("clicks", 1),
                },
            )
        elif kind == "volume":
            await call("control_volume", {"action": a.get("action", "up")})
        elif kind == "close_app":
            await call("close_app", {"window_title": a.get("app", "")})
        else:
            # PEHLE: unknown kind chup-chaap skip ho jata tha → calls khali → "kuch hua hi nahi"
            # ka confusion aur success claim ka rasta. Ab explicit failure record banta hai.
            records.append(
                {
                    "name": f"unsupported_action:{kind}",
                    "args": dict(a),
                    "ok": False,
                    "output": f"(action kind '{kind}' is version me supported nahi hai — kaam nahi hua)",
                    "source": "deterministic",
                }
            )

    # invariant: har action ka ek record hona chahiye — warna "kuch hua nahi" ka pata nahi chalega
    expected = len(action.get("actions") or [])
    if len(records) != expected:
        records.append(
            {
                "name": "action_record_invariant",
                "args": {},
                "ok": False,
                "output": f"(records={len(records)} expected={expected} — execution tracking "
                f"adhoora hai, isliye fail maana jaye)",
                "source": "deterministic",
            }
        )
    return records


_ACTION_TOOL_NAMES: dict[str, tuple[str, ...]] = {
    "open_app": ("open_any_app", "open_app"),
    "type_text": ("type_text",),
    "hotkey": ("press_hotkey",),
    "screenshot": ("take_screenshot",),
    "volume": ("control_volume",),
    "close_app": ("close_app",),
}


def _needs_action(request: str) -> bool:
    """Request me asli action maanga gaya hai? (sirf sawaal nahi)"""
    low = (request or "").lower()
    if not any(k in low for k in _ACTION_KEYS):
        return False
    # pure sawaal (kaise karein / kya hai / batao) → action nahi
    if re.search(r"\b(kaise|how to|kya hai|batao|samjhao|explain|tutorial|steps)\b", low) and not re.search(
        rf"\b({_OPEN_VERBS}|{_TYPE_VERBS})\b", low
    ):
        return False
    return True


class SubAgentRun:
    """Sub-agent ka result — text ke saath *actual* tool calls ka record.

    Ye isliye zaroori hai: model "kar diya" bol deta hai chahe tool chala hi na ho.
    Yahan hum sirf wahi maante hain jo tool ne khud return kiya.
    """

    def __init__(self, text: str = "", calls: list[dict[str, Any]] | None = None, error: str = "") -> None:
        self.text = text or ""
        self.calls = calls or []
        self.error = error

    @property
    def tool_count(self) -> int:
        return len(self.calls)

    @property
    def ok_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c.get("ok")]

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if not c.get("ok")]

    def lines(self, limit: int = 6) -> list[str]:
        out = []
        for c in self.calls[-limit:]:
            mark = "✅" if c.get("ok") else "❌"
            out.append(f"{mark} {c.get('name')}({_short_args(c.get('args'))}) → {str(c.get('output'))[:220]}")
        return out


def _short_args(args: Any) -> str:
    try:
        if isinstance(args, dict):
            return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:3])
        return str(args)[:80]
    except Exception:
        return "?"


def _looks_like_failure(output: str) -> bool:
    """Tool output actually fail hua ya nahi (emoji/word dono check)."""
    low = (output or "").strip().lower()
    if not low:
        return False
    bad = (
        "❌",
        "error:",
        "traceback",
        " not found",
        "not found",
        "nahi mila",
        "fail hua",
        "fail:",
        "install karo",
        "unavailable",
        "permission denied",
        "access is denied",
        "module not found",
        "no module named",
        "exception",
    )
    return any(b in low for b in bad) and "✅" not in low[:6]


def _tool_call_record(name: str, args: Any, output: str) -> dict[str, Any]:
    return {
        "name": name,
        "args": args if isinstance(args, dict) else {"_raw": str(args)[:120]},
        "output": str(output)[:800],
        "ok": not _looks_like_failure(str(output)),
    }


async def _run_react_subagent(role: str, task: str, tools: list, raw_request: str = "") -> SubAgentRun:
    """Ek specialist ReAct sub-agent chalao (LangChain 1.x create_agent) + tool calls track karo."""
    if not tools:
        return SubAgentRun(text="(koi tool available nahi)", error="no tools")
    if OFFLINE_MODE or PROVIDER == "demo":
        # offline: tools ko deterministic queue me chalao (demo brain).
        # NOTE: instructions wala wrapper text use nahi karte, warna uske words
        # (jaise "launch"/"band karna") galat tool trigger kar dete hain.
        text, calls = await _offline_tool_run(raw_request or task, tools)
        return SubAgentRun(text=text, calls=calls)

    from ..llm.core import ToolBindingError

    try:
        try:
            from langchain.agents import create_agent  # LangChain 1.x
        except Exception:  # import hi fail hua (1.x nahi hai) → legacy ReAct
            create_agent = None  # type: ignore[assignment]

        # tool-required path: model tool-capable hona hi chahiye. `require_tool_bound` unbound
        # / un-flagged chains par ToolBindingError deta hai (silent plain-text generation nahi).
        from ..runtime.evidence import require_tool_bound

        model = require_tool_bound(get_chat_model("brain"), role="brain")
        tools_bound_already = bool(getattr(model, "tools_bound", True))

        if create_agent is not None:
            agent = create_agent(model, tools=tools, system_prompt=SUPERVISOR_SYSTEM)
        else:
            from langgraph.prebuilt import create_react_agent  # fallback

            agent = create_react_agent(model, tools=tools, prompt=SUPERVISOR_SYSTEM)
        _ = tools_bound_already  # (create_agent khud bind karta hai; flag sirf precondition proof)

        result = await asyncio.wait_for(
            agent.ainvoke({"messages": [HumanMessage(content=task)]}, config={"recursion_limit": 25}),
            timeout=300,
        )
        msgs = result.get("messages", [])

        # --- tool calls ko *dono* taraf se pakdo: request (AIMessage.tool_calls) aur result (ToolMessage)
        pending: dict[str, dict[str, Any]] = {}
        calls: list[dict[str, Any]] = []
        texts: list[str] = []
        for m in msgs:
            if isinstance(m, AIMessage):
                if isinstance(m.content, str) and m.content.strip():
                    texts.append(m.content)
                for tc in getattr(m, "tool_calls", None) or []:
                    tcid = tc.get("id") or tc.get("name") or f"call{len(pending)}"
                    pending[tcid] = {"name": tc.get("name") or "tool", "args": tc.get("args") or {}}
            elif m.__class__.__name__ == "ToolMessage":
                tcid = getattr(m, "tool_call_id", "") or ""
                meta = pending.pop(tcid, {}) or {}
                calls.append(
                    _tool_call_record(
                        meta.get("name", getattr(m, "name", "tool") or "tool"), meta.get("args", {}), str(m.content)
                    )
                )
        # jinke result nahi aaya (aborted) unhe bhi record karo
        for meta in pending.values():
            calls.append(
                {
                    "name": meta.get("name", "tool"),
                    "args": meta.get("args", {}),
                    "output": "(tool result nahi aaya — call aborted)",
                    "ok": False,
                }
            )

        body = texts[-1] if texts else "(subagent ne kuch nahi kaha)"
        if calls:
            body += "\n\ntool calls:\n" + "\n".join(f"- {ln}" for ln in SubAgentRun(calls=calls).lines())
        return SubAgentRun(text=body, calls=calls)
    except ToolBindingError as exc:
        # tools bind hi nahi huye → bina tools ke "confident" jawab dena galat hoga.
        # Isliye loud fail: user ko saaf reason, aur graph ise error ke roop me record karta hai.
        emit("error", f"tools model ke saath bind nahi hue — {str(exc)[:160]}", status="fail")
        return SubAgentRun(
            text=(
                "⚠️ is kaam ke liye tools ko model ke saath bind nahi kiya ja saka, aur bina "
                "tools ke jawab dena galat hota — isliye ruk gaya. Reason: " + str(exc)
            ),
            error=f"tool_binding_failed: {str(exc)[:200]}",
        )
    except Exception as exc:
        return SubAgentRun(text=f"(subagent error: {exc})", error=str(exc))


async def _offline_tool_run(task: str, tools: list) -> tuple[str, list[dict[str, Any]]]:
    """Offline demo: intent ke hisaab se seedha tool chala do (bina LLM)."""
    by_name = {getattr(t, "name", ""): t for t in tools}
    low = task.lower()
    results: list[str] = []
    calls: list[dict[str, Any]] = []
    # Sawaal ko action na samjho: "notepad kaise kholte hain" par app open karna galat tha.
    question_only = bool(
        re.search(
            r"\b(kaise|kaisay|how to|kya hai|batao|samjhao|explain|tutorial|"
            r"steps|kyun|kyu)\b",
            low,
        )
    ) and not re.search(rf"\b({_OPEN_VERBS}|{_TYPE_VERBS})\b\s*[:\-]?\s*\S*\s*(likho|likh|type)?$", low)

    # loop guard: sirf *is run* ke andar duplicate call rokо — global/persistent memory se nahi.
    # (Pehle ye persistent store use karta tha, isliye same request doosri baar aate hi tool
    #  "loop guard" ke naam par chalta hi nahi tha — matlab kaam bina execute huye skip.)
    import json as _json

    seen_this_run: dict[str, int] = {}

    def _record(name: str, args: dict, output: str, ok: bool = True, **extra) -> None:
        """Real kaam ka record (offline path me bhi) — bina iske success claim proof-less hai.

        `ok` = tool chala aur result mila. Outcome ka content `output` me hota hai; search jaise
        cases me `found` field alag se rakhi jaati hai taaki "kuch nahi mila" ko success na samjha jaye.
        """
        rec = {"name": name, "args": args, "output": str(output)[:800], "ok": bool(ok), "source": "deterministic"}
        rec.update(extra)
        calls.append(rec)

    async def _call(name: str, args: dict) -> None:
        t = by_name.get(name)
        if not t:
            return
        try:
            sig = f"offline::{name}::{_json.dumps(args, sort_keys=True, default=str)[:200]}"
            count = seen_this_run.get(sig, 0)
            seen_this_run[sig] = count + 1
            if count >= 2:
                emit("warn", f"{name} skip (loop guard — yahi call pehle hui thi)", tool=name, status="skipped")
                results.append(f"[{name}] skipped (loop guard: yahi call pehle bhi hui thi)")
                return
        except Exception:
            pass
        emit("tool_start", f"{name} chala raha hoon", tool=name, detail=json.dumps(args, default=str)[:160])
        _t0 = time.time()
        try:
            out = await t.ainvoke(args)
            _ok = not str(out).lstrip().startswith(("❌", "⚠️", "🚫", "🚫"))
            emit(
                "tool_end",
                f"{name}: {str(out)[:100]}",
                tool=name,
                status="ok" if _ok else "fail",
                ms=int((time.time() - _t0) * 1000),
                detail=str(out)[:300],
            )
            results.append(f"[{name}] {str(out)[:600]}")
            calls.append(_tool_call_record(name, args, str(out)))
            try:
                get_state().note_file_touched(str(args.get("path") or args.get("name") or ""))
            except Exception:
                pass
        except Exception as exc:
            emit(
                "error", f"{name} fail: {str(exc)[:140]}", tool=name, status="fail", ms=int((time.time() - _t0) * 1000)
            )
            results.append(f"[{name}] error: {exc}")
            calls.append({"name": name, "args": args, "output": f"error: {exc}", "ok": False})
            try:
                get_state().remember_error(f"{name}: {exc}")
            except Exception:
                pass

    # ---- automations: "har subah 8 baje X" / "har ghante Y" / "remind me" ----
    if any(
        k in low
        for k in (
            "har subah",
            "har sham",
            "har shaam",
            "har raat",
            "har din",
            "daily",
            "every day",
            "every hour",
            "har ghante",
            "har ghanta",
            "every ",
            "remind",
            "yaad dila",
            "schedule",
            "automation banao",
            "kal ",
        )
    ):
        m = re.search(
            r"(har\s+[\w\s]{0,20}?\s*\d{1,2}(?::\d{2})?\s*(?:am|pm|baje)?|"
            r"daily\s*\d{1,2}(?::\d{2})?|every\s*\d*\s*\w+|kal\s*\d{1,2}(?::\d{2})?\s*(?:am|pm|baje)?|"
            r"\d{1,2}(?::\d{2})?\s*(?:am|pm|baje))",
            task,
            re.I,
        )
        sched = m.group(1).strip() if m else "daily 08:00"
        await _call("create_automation", {"task": task, "schedule": sched})

    # ---- system ops: refresh / status / cache / process clear ----
    if any(
        k in low
        for k in (
            "refresh",
            "reload",
            "dobara load",
            "cache",
            "system status",
            "machine ki halat",
            "kya haal hai system",
            "system info",
        )
    ):
        if any(k in low for k in ("refresh", "reload", "dobara load", "cache")):
            await _call("refresh_system_tool", {"clear_caches": True})
        else:
            await _call("full_system_status", {})
    if ("process" in low or "background" in low) and any(k in low for k in ("clear", "band", "kill", "hata", "rok")):
        await _call("clear_process_tool", {})

    # ---- seedha typing: "notepad me hello likho" (offline) ----
    if not question_only:
        typed = ""
        m_typed = re.search(r"(.+?)\s*(?:likho|likh do|type karo|type)\s*$", task, re.I)
        if m_typed:
            typed = m_typed.group(1).strip(" .,:\"'“”‘’")
            typed = re.sub(rf"^.*?\b(?:{_OPEN_VERBS}|me|mein|usme|usmein|par|pe|upar)\b[\s,]*", "", typed, flags=re.I)
        if typed and len(typed) >= 2 and any(k in low for k in ("likho", "likh", "type")):
            await _call("type_text", {"text": typed[:300], "press_enter": False})

    # ---- note save? (sirf jab save karne ko kaha ho, sirf 'note' word se nahi) ----
    if any(
        k in low
        for k in (
            "save kar",
            "save kardo",
            "save krdo",
            "likh do",
            "likh de",
            "notepad me",
            "note me save",
            "note bana",
            "note banao",
        )
    ):
        await _call(
            "save_note",
            {"name": _extract_note_name(task), "content": _extract_note_body(task), "open_in_notepad": False},
        )

    # ---- app kholna (ab kisi bhi naam se — search based) ----
    # question_only: "notepad kaise kholte hain" par app kholna galat tha (kaise/tutorial).
    if not question_only and any(k in low for k in ("khol", "open", "launch", "chalu kar", "chala do")):
        hint = _extract_app_hint(task)
        if hint and hint.lower() not in {"kaise", "kaisay", "how", "kya", "kyun", "steps", "tutorial"}:
            await _call("open_any_app", {"query": hint})

    # ---- file dhoondhna + padhna (asli offline power) ----
    want_read = any(
        k in low for k in ("padho", "padh", "read", "kya likha", "summary", "summar", "dikhao", "nikaal", "dekh")
    )
    want_find = any(k in low for k in ("dhoondh", "find", "kahan hai", "dhundo", "locate", "search"))
    if want_find or (
        want_read
        and any(n in low for n in ("file", "notes", "note", "pdf", "excel", "csv", "docx", "folder", "report"))
    ):
        hint = _extract_file_hint(task)
        exts = _ext_filter(task)
        if hint:
            hits = await _local_file_find(hint, limit=5, exts=exts)
            if not hits and exts:
                loose = await _local_file_find(hint, limit=3)
                if loose:
                    results.append(
                        f"[find_files] '{hint}' ki file mili par wo {', '.join(exts)} type ki nahi hai: "
                        + ", ".join(f"{l['name']} ({l['score']}%)" for l in loose)
                    )
            if not hits:
                results.append(
                    f"[find_files] '{hint}' naam ki koi file nahi mili "
                    f"({'/'.join(exts) if exts else 'koi bhi type'}, roots: {', '.join(FILE_SEARCH_ROOTS)})"
                )
                # search chali (ye asli tool call hai) par kuch nahi mila → found=False
                _record(
                    "find_files",
                    {"name": hint, "exts": list(exts)},
                    f"0 matches ('{hint}', {('/'.join(exts)) if exts else 'any type'}, "
                    f"roots: {', '.join(FILE_SEARCH_ROOTS)})",
                    ok=True,
                    found=False,
                )
            else:
                top = hits[0]
                alt = ", ".join(f"{h['name']} ({h['score']}%)" for h in hits[1:4])
                results.append(
                    f"[find_files] {len(hits)} match{' (' + '/'.join(exts) + ')' if exts else ''}"
                    f" • best: {top['name']} ({top['score']}%)"
                    f"\n   {top['path']}" + (f"\n   aur bhi: {alt}" if alt else "")
                )
                _record(
                    "find_files",
                    {"name": hint, "exts": list(exts)},
                    f"{len(hits)} match • best: {top['name']} ({top['score']}%) → {top['path']}"
                    + (f" • aur: {alt}" if alt else ""),
                    ok=True,
                    found=True,
                    path=top["path"],
                )
                if want_read:
                    body = await _local_file_read(top["path"], max_chars=1500)
                    results.append(f"[read_file] 📄 {top['name']}\n{body}")
                    _record(
                        "read_file",
                        {"path": top["path"], "max_chars": 1500},
                        f"📄 {top['name']} ({len(body or '')} chars)",
                        ok=bool(body),
                        found=bool(body),
                        path=top["path"],
                    )
    if not results:
        results.append(
            "(offline desktop worker: koi concrete tool action match nahi hua — plan ke steps: " + task[:200] + ")"
        )
    return "\n".join(results), calls


WORKER_NODES = {
    "browser": browser_worker,
    "research": research_worker,
    "code": code_worker,
    "desktop": desktop_worker,
    "memory": memory_worker,
}


# ---------------------------------------------------------------------------
# offline file/app helpers (bina LLM ke bhi kaam karein)
# ---------------------------------------------------------------------------
def _extract_file_hint(task: str) -> str:
    """Request se file ka naam guess karo — extension, quotes, ya 'X wali file' pattern se."""
    t = (task or "").strip()
    m = re.search(r"[\"'“”‘’]([^\"'“”‘’]{3,60})[\"'“”‘’]", t)
    if m and "." in m.group(1):
        return m.group(1).strip()
    m = re.search(r"([\w\-]{2,50}\.\w{2,5})\b", t)
    if m:
        return m.group(1).strip()
    m = re.search(
        r"([\w\-]{3,50})\s+(?:wala|wali|wale|ka|ki|ke|naam\s+ki|naam\s+ka)?\s*"
        r"(?:file|files|notes?|pdf|excel|csv|docx|folder|document|report|sheet)",
        t,
        re.I,
    )
    if m:
        return _clean_hint(m.group(1))
    m = re.search(
        r"(?:file|notes?|pdf|excel|docx|folder|document|report)\s+"
        r"(?:named?|naam\s+ki|jisda?\s+naam)?\s*([\w\-]{3,50})",
        t,
        re.I,
    )
    if m:
        return _clean_hint(m.group(1))
    return ""


_STOP_HINTS = {
    "workspace",
    "jo",
    "wo",
    "woh",
    "mein",
    "me",
    "meri",
    "mera",
    "the",
    "file",
    "koi",
    "ko",
    "se",
    "par",
    "pe",
    "ek",
    "kya",
    "hai",
    "kaun",
    "kahan",
    "jarvis",
    "merko",
    "mujhe",
}


def _clean_hint(hint: str) -> str:
    words = [w for w in re.split(r"[\s,]+", hint.strip()) if w and w.lower() not in _STOP_HINTS]
    return " ".join(words[-3:]) if words else ""


def _extract_app_hint(task: str) -> str:
    """'safari khol do' / 'notepad open karo' → app ka naam."""
    m = re.search(r"(?:open|launch|chalu|start)\s+(?:kar\s*do|karo|kardo|krdo)?\s*([\w\-\.]{2,40})", task, re.I)
    if m:
        return _clean_hint(m.group(1))
    m = re.search(r"([\w\-\.]{2,40})\s+(?:ko\s+)?(?:khol|open|launch|chalu|start)", task, re.I)
    if m:
        return _clean_hint(m.group(1))
    return ""


_EXT_HINTS: dict[str, tuple[str, ...]] = {
    "pdf": (".pdf",),
    "word": (".docx", ".doc"),
    "docx": (".docx",),
    "excel": (".xlsx", ".xls", ".csv"),
    "xlsx": (".xlsx",),
    "sheet": (".xlsx", ".csv"),
    "csv": (".csv",),
    "ppt": (".pptx",),
    "powerpoint": (".pptx",),
    "image": (".png", ".jpg", ".jpeg", ".webp", ".gif"),
    "photo": (".png", ".jpg", ".jpeg"),
    "screenshot": (".png", ".jpg"),
    "markdown": (".md",),
    "note": (".md", ".txt"),
    "notes": (".md", ".txt"),
    "text": (".txt",),
    "txt": (".txt",),
    "json": (".json",),
    "zip": (".zip",),
}


def _ext_filter(task: str) -> tuple[str, ...]:
    """Request me type mention ho to wahi type ki file dhoondho ('resume pdf' → sirf .pdf)."""
    low = (task or "").lower()
    out: list[str] = []
    for key, vals in _EXT_HINTS.items():
        if re.search(rf"\b{re.escape(key)}", low):
            for v in vals:
                if v not in out:
                    out.append(v)
    return tuple(out)


_NOTE_NAME_STOP = {
    "karo",
    "kar",
    "krdo",
    "kardo",
    "likho",
    "likh",
    "de",
    "do",
    "save",
    "notepad",
    "me",
    "file",
    "banao",
    "bana",
    "naam",
    "name",
    "se",
    "aur",
    "content",
    "ek",
    "ye",
    "is",
    "ko",
    "note",
    "wala",
    "wali",
    "bhai",
}


def _extract_note_body(task: str) -> str:
    """Instruction hata ke sirf asli content nikalo.

    "notes me deals naam se ye save kardo: laptop 55k"  →  "laptop 55k"
    "yaad dilao ki ... notepad me todo naam se save kardo: doodh lana"  → "doodh lana"
    Pehle poora request hi content ban jata tha — wahi bug fix.
    """
    t = (task or "").strip()
    if not t:
        return t

    # 1) explicit separator (: — – newline) ke baad ka content sabse bharosemand hai
    for sep in (":", "\n", "—", "–"):
        if sep in t:
            after = t.split(sep, 1)[1].strip(" \t")
            if len(after) >= 2:
                return after

    # 2) instruction prefix ("notes me <name> naam se ye save kardo") hatao
    body = re.sub(
        r"^\s*(?:jarvis[,\s]+)?(?:notes?|note\s*pad|notepad|file)\s*(?:me(?:in)?)?\s*"
        r"(?:ye|yeh)?\s*[\w\-\. ]{0,40}?\s*(?:naam|name)\s*(?:se|rakh\w*|ke\s*naam\s*se)?\s*(?:ke|kar|karke)?\s*"
        r"(?:ye|yeh)?\s*(?:save\s*kar\w*|likh\s*(?:do|de|ke\s*rakh\w*)|rakh\w*|store\s*kar\w*)\s*[:,-]?\s*",
        "",
        t,
        flags=re.I,
    ).strip()
    if body and body != t:
        return body

    # 3) generic "ye save kardo ..." / "save kardo ..."
    body = re.sub(
        r"^\s*(?:jarvis[,\s]+)?(?:ye\s*|yeh\s*)?(?:save\s*kar\w*|likh\s*(?:do|de)|store\s*kar\w*)\s*[:,-]?\s*",
        "",
        t,
        flags=re.I,
    ).strip()
    return body or t


def _extract_note_name(task: str) -> str:
    """'... naam laptop_deals ...' → laptop_deals  (pehle 'karo' jaisa word pakda ja raha tha)."""
    t = task or ""
    pats = [
        # "todo naam se" / "todo naam rakh ke" — sabse common Hinglish structure, isliye pehle
        r"([\w\-\.]{3,40})\s*(?:naam|name)\s*(?:se|rakh\w*|ke\s*naam\s*se)",
        # "naam: todo" / "name = todo"
        r"(?:naam|name)\s*[:=]\s*[\"\']?([\w\-\.]{3,40})",
        r"(?:naam|name)\s*(?:se|rakh\w*)?\s*[:=]?\s*[\"\']?([\w\-\.]{3,40})",
    ]
    for pat in pats:
        m = re.search(pat, t, re.I)
        if m:
            cand = m.group(1).strip().strip(".,;:!?")
            if cand.lower() in _NOTE_NAME_STOP or any(w.lower() in _NOTE_NAME_STOP for w in cand.split()):
                continue
            return cand
    m = re.search(r"[\"\'“”‘’]([^\"\'“”‘’]{3,40})[\"\'“”’‘’]", t)
    if m and m.group(1).strip().lower() not in _NOTE_NAME_STOP:
        return m.group(1).strip()
    return "jarvis_note"


_LAST_RESCAN = [0.0]


def _RESCAN_OK(min_gap_s: float = 1.0) -> bool:
    """Forced file-index rescan ko throttle karo (har search par poori disk walk na ho)."""
    if time.time() - _LAST_RESCAN[0] < min_gap_s:
        return False
    _LAST_RESCAN[0] = time.time()
    return True


async def _local_file_find(name: str, limit: int = 5, exts: tuple[str, ...] = ()) -> list:
    """Index se fuzzy file dhoondho (stale index ho to forced rescan). Optionally type-filter."""
    from ..tools.file_tools import _fresh_index, _index_items, fuzzy_search

    ext_arg = ",".join(exts)
    hits = fuzzy_search(name, items=await _fresh_index(), limit=limit, ext=ext_arg)
    # Stale index me koi *adhoora* match (65-92%) aa gaya to forced rescan bhi karo —
    # warna user ki abhi banayi file purane fuzzy hit ke peeche chhup jaati hai.
    if not hits or hits[0].get("score", 0) < 92:
        if _RESCAN_OK():
            fresh = fuzzy_search(name, items=await _index_items(force=True), limit=limit, ext=ext_arg)
            if fresh and (not hits or fresh[0].get("score", 0) >= hits[0].get("score", 0)):
                hits = fresh
    return [h for h in hits if h.get("score", 0) >= 65]


async def _local_file_read(path: str, max_chars: int = 1500) -> str:
    """Kisi bhi type ki file ka text nikaalo (PDF/DOCX/XLSX/text…)."""
    from ..tools.file_readers import read_any_file

    try:
        res = await asyncio.to_thread(read_any_file, path, max_chars)
        text = (res or {}).get("text") or ""
        return text.strip()[:max_chars] or "(file khali hai)"
    except Exception as exc:
        return f"(read fail: {exc})"


# ===========================================================================
# 5. SYNTHESIZE — answer banao
# ===========================================================================

# ---------------------------------------------------------------------------
# HONESTY GUARD — "kar diya" sirf tab jab tool ne kiya ho
# ---------------------------------------------------------------------------
_SUCCESS_CLAIMS = (
    "kar diya",
    "kar di",
    "ho gaya",
    "ho gya",
    "done",
    "likh diya",
    "likh di",
    "khol diya",
    "khol di",
    "band kar diya",
    "save kar diya",
    "bhej diya",
    "chala diya",
    "complete",
    "successfully",
    "ban gaya",
    "ban diya",
)
_RAKH_DOWN = ("khaali", "khali")


def _claims_success(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in _SUCCESS_CLAIMS) and not any(k in low for k in ("nahi", "not ", "fail"))


def honesty_guard(state: JarvisState, answer: str) -> tuple[str, str]:
    """Answer ko sach ke saath align karo. Returns (final_answer, guard_reason).

    Rule (hard invariant): LLM ka text evidence NAHI hai. Success sirf tab maana jata hai jab
    asli tool call hui ho aur uska result mila ho (state["tool_evidence"] / findings[].tool_calls).
    """
    if state.get("status") in {"memory", "settings", "time", "clarify"}:
        return answer, ""

    verdict = verify_execution(state, answer)
    required = verdict.tool_required
    ev_done = verdict.ok_records
    ev_failed = verdict.failed_records

    if not required:
        # Action nahi maanga gaya — par agar answer *execution success* claim karta hai aur
        # koi tool evidence nahi hai, to wo claim bhi evidence nahi ban sakta (req: LLM ke
        # "done/opened/executed/maine kar diya" text ko evidence mat maano).
        # Generic success-phrases ("ho gaya / done / complete") bahut broad hain — inhe sirf
        # tab pakdo jab request action-shaped ho (plain chit-chat par false positive na ho).
        actionish = _needs_action(state.get("request", "")) or bool(state.get("action_required"))
        if claims_without_support(state, answer) or (actionish and _claims_success(answer)):
            state["status"] = "failed"
            return (
                f"⚠️ **Ye claim main verify nahi kar sakta, {OWNER_NAME}.**\n\n"
                f"Jawab me '{verdict.claim_words[0]}' jaisa kuch tha, lekin is run me koi tool call "
                f"nahi chali — matlab machine par kuch execute nahi hua.\n\n"
                f"Jo main actually kar sakta hoon wo batao (jaise `notepad kholo`, `notes me x naam se "
                f"save kardo: ...`), phir seedha tool chala kar proof ke saath bataunga.",
                "execution claim without tool evidence",
            )
        return answer, ""

    # "done" sirf un records se jo kaam ko support karte hain (khali search se nahi).
    # `actions_done` legacy channel bhi isi filter se guzarta hai — warna found=False record
    # wahan se wapas "success" ban jata tha (guard bypass).
    done = supporting_records(ev_done) or supporting_records(state.get("actions_done") or [])
    failed = ev_failed or (state.get("action_failures") or [])

    def _fmt(items: list[dict[str, Any]]) -> str:
        out = []
        for c in items[-6:]:
            mark = "✅" if c.get("ok") else "❌"
            out.append(f"{mark} {c.get('name')} → {str(c.get('output'))[:180]}")
        return "\n".join(out)

    if not done and not failed:
        if ev_done:
            # tool(s) chale par kuch mila/nikal nahi (jaise file search: 0 matches).
            # Yahan model ka "ho gaya / kaam complete" claim *hata* diya jata hai aur asli
            # natija likha jata hai. Read-only search ke liye run "failed" nahi — search to
            # chal hi gayi thi; bas result khali tha.
            search_only = all(str(r.get("name") or "") in {"find_files", "read_file"} for r in ev_done)
            state["status"] = "done" if search_only else "failed"
            reason = (
                "search ran but nothing was found" if search_only else "execution ran but nothing was found / no effect"
            )
            return (
                f"⚠️ **Ye claim verify nahi hua, {OWNER_NAME}.**\n\n"
                f"Tool(s) chale, par kuch mila nahi / koi asar nahi hua:\n{_fmt(ev_done)}\n\n"
                "Jo actually hua wahi bata raha hoon — 'ho gaya' bolna galat hota. "
                "Naam/path thoda exact do (jaise `notepad kholo` ya `notes me save kardo: …`) "
                "to dobara try karta hoon.",
                "" if search_only else reason,
            )
        state["status"] = "failed"
        return (
            f"⚠️ **Kaam nahi hua, {OWNER_NAME}.**\n\n"
            "Is request ke liye koi tool call nahi chala — matlab machine par kuch bhi execute nahi hua.\n"
            "Aise hi 'ho gaya' bolna galat hota hai, isliye main saaf bata raha hoon.\n\n"
            "**Kya try kar sakte ho:** request thoda specific karo jaise "
            "`notepad kholo` ya `notepad open karke 'hello' likho` — phir main seedha tool chalaunga.",
            "action required but zero tool calls",
        )

    if not done and failed:
        state["status"] = "failed"
        return (
            f"❌ **Kaam fail hua, {OWNER_NAME}.**\n\nYe tool call(s) chale par error aaya:\n{_fmt(failed)}\n\n"
            "Wajah usually: app registry me nahi mila, ya typing/hotkey ke liye OS-level input block hai. "
            "Windows par typing ab bina kisi install ke chalti hai; Linux par `xdotool` chahiye.",
            "all actions failed",
        )

    if failed:
        state["status"] = "partial_failure"
        # Warning HAMESHA sabse upar — warna pehli line ("Ho gaya boss ✅") padh kar
        # user ko lagta hai kaam ho gaya, jabki ek step fail hua tha.
        head = (
            f"⚠️ **Aadha kaam hua, {OWNER_NAME}.**\n\n"
            f"✅ Jo hua:\n{_fmt(done) if done else '—'}\n\n"
            f"❌ Jo fail hua:\n{_fmt(failed)}\n\n---\n"
        )
        body_lines = (answer or "").split("\n")
        # pehli line me jhooti success claim ho to hata do
        if body_lines and _claims_success(body_lines[0]):
            body_lines[0] = ""
        return head + "\n".join(body_lines).strip(), "partial failure reported"

    # sab ok → agar model ne claim nahi kiya to verified facts add kar do
    if not _claims_success(answer):
        answer = answer + f"\n\n**Verified:**\n{_fmt(done)}"
    return answer, ""


async def synthesize_node(state: JarvisState) -> dict[str, Any]:
    emit("thinking", "Findings ko final jawab me likh raha hoon…")
    request = state.get("request", "")
    # ---- BLIND-HANDOFF DETECTION: bina tool proof wali findings pakdo ----
    blind = detect_blind_handoffs(state)
    blind_workers = [str(b.get("worker")) for b in blind]
    blind_events: list[dict[str, Any]] = []
    if blind:
        for b in blind:
            msg = f"blind_handoff: {b.get('worker')} ne bina tool evidence summary di ({b.get('reason')})"
            if isinstance(state.get("errors"), list):
                state.setdefault("errors", []).append(msg)
            blind_events.append(_event(state, "blind_handoff", msg))
            emit("error", msg)
    evidence = evidence_digest(state, blind=blind_workers)
    critique = state.get("critique") or {}
    critique_text = json.dumps(critique, ensure_ascii=False)[:1500] if critique else ""
    iterations = state.get("iterations", 0)

    answer = ""
    if not (OFFLINE_MODE or PROVIDER == "demo"):
        answer = await asyncio.to_thread(dspy_mods.final_answer, request, evidence, critique_text)

        if not answer:
            try:
                from langchain_core.messages import SystemMessage

                model = get_chat_model("brain")
                resp = await model.ainvoke(
                    [
                        SystemMessage(content=supervisor_prompt()),
                        HumanMessage(
                            content=(
                                f"Owner ki request: {request}\n\n"
                                f"Workers ka evidence:\n{evidence}\n\n"
                                f"Critic feedback: {critique_text or 'none'}\n\n"
                                f"Ab final Hinglish answer likho (short, factual, {OWNER_NAME} ko address karke)."
                            )
                        ),
                    ]
                )
                answer = str(resp.content).strip()
            except Exception as exc:
                answer = ""
                state.setdefault("errors", []).append(
                    f"synthesize: {exc}" if isinstance(state.get("errors"), list) else "synthesize error"
                )

    if not answer:
        answer = _offline_synthesize(state, evidence, iterations)

    # ---- HONESTY GUARD: jhooth se pehle sach ----
    answer, guard_reason = honesty_guard(state, answer)
    extra_events = []
    if guard_reason:
        extra_events.append(_event(state, "honesty_guard", guard_reason))
        state.setdefault("errors", []).append(f"honesty_guard: {guard_reason}")

    # ---- HARD INVARIANT (belt & braces): tool-required + zero evidence → kabhi success nahi ----
    # Ye guard ke baad bhi chalta hai, taaki koi naya code path silently plain-text success na bana de.
    verdict = verify_execution(state, answer)
    if verdict.tool_required and not verdict.has_evidence:
        state["status"] = "failed"
        if not guard_reason:
            guard_reason = "execution_required but zero tool evidence (hard invariant)"
            extra_events.append(_event(state, "invariant", guard_reason))
            state.setdefault("errors", []).append(f"invariant: {guard_reason}")
            answer = (
                f"⚠️ **Kaam nahi hua, {OWNER_NAME}.** Is request ke liye asli tool execution zaroori "
                f"thi, lekin koi tool call nahi chali — isliye main success claim nahi kar sakta.\n\n"
                f"Reason: {guard_reason}"
            )

    out: dict[str, Any] = {
        "answer": answer,
        "evidence": evidence,
        "execution_required": bool(verdict.tool_required),
        "events": (
            [_event(state, "synthesize", f"{len(answer)} chars answer (iter {iterations + 1})")]
            + blind_events
            + extra_events
        ),
    }
    if state.get("status") in ("failed", "partial_failure"):
        out["status"] = state["status"]  # honest status upstream tak (AgentResult.status)
    return out


def _offline_synthesize(state: JarvisState, evidence: str, iterations: int) -> str:
    """Deterministic Hinglish summary — demo/offline mode."""
    request = state.get("request", "")
    findings = state.get("findings", []) or []

    # Direct-answer route (identity/baarish/small talk) → demo brain ka conversational reply
    if not findings and set(state.get("workers") or []) <= {"answer"}:
        try:
            from ..llm import get_chat_model

            model = get_chat_model("brain")
            chat_fn = getattr(model, "_chat", None)
            if callable(chat_fn):
                reply = str(chat_fn(request)).strip()
                if reply:
                    return reply
        except Exception:
            pass
    notes = state.get("notes_saved", []) or []
    artifacts = state.get("artifacts", []) or []
    workers = sorted({f.get("worker", "?") for f in findings})

    # Offline synthesis: chhota, kaam-ka, Hinglish — plan/evidence ko screenshot ki tarah nahi dikhata
    lines: list[str] = []
    real_evidence = [ln for ln in evidence.splitlines() if ln.strip() and "koi tool evidence nahi" not in ln]
    head = real_evidence[:6] if real_evidence else []

    failed_actions = state.get("action_failures") or []
    verified_actions = state.get("actions_done") or []
    verification_failed = any("VERIFICATION FAILED" in str(f.get("summary", "")) for f in findings)
    # "koi concrete tool action match nahi hua" = humne kuch kiya hi nahi.
    # Us case me "Ho gaya boss ✅" kehna jhooth lagta hai (khaas kar sawaal par).
    no_action_taken = bool(findings) and all(
        (
            "koi concrete tool action match nahi hua" in str(f.get("summary", ""))
            or "koi tool evidence nahi" in str(f.get("summary", ""))
        )
        for f in findings
    )
    if failed_actions and not verified_actions:
        lines.append(f"❌ {OWNER_NAME}, ye kaam poora nahi hua — tool ne error diya:")
        lines.append("")
        lines += [f"• {c.get('name')} → {str(c.get('output'))[:160]}" for c in failed_actions[:4]]
        lines.append("")
    elif verification_failed:
        lines.append(
            f"⚠️ {OWNER_NAME}, maine is request par koi tool call nahi kiya — "
            "matlab machine par kuch nahi hua. Sach bata raha hoon, guess nahi."
        )
        lines.append("")
    if no_action_taken:
        lines.append(f"Samajh gaya, {OWNER_NAME} — is request par koi tool chalane ki zaroorat nahi padi.")
        lines.append("")
        lines.append(
            "Agar **kaam karwana** hai to seedha bolo, jaise: "
            "`notepad kholo`, `notepad open karke hello likho`, `screenshot lo`, "
            "`notes me x naam se save kardo: ...`. "
            "Aur agar **samajhna** hai to poocho, jaise: `notepad kaise kholte hain`."
        )
        head = []
    elif head:
        if not (failed_actions and not verified_actions) and not verification_failed:
            lines.append(f"Ho gaya boss ✅ — {OWNER_NAME} ke liye kaam complete.")
        else:
            lines.append("Jo hua:")
        lines.append("")
        lines += head
    else:
        lines.append(f"Samajh gaya: _{request[:160]}_")
        lines.append("")
        lines.append(
            "Offline demo mode me main ise rule-based handle kar raha hoon — koi tool "
            "isi request ke liye match nahi hua."
        )
    if notes:
        lines.append(f"\n📝 Note save hua: {', '.join(str(n)[:40] for n in notes[:3])}")
    if artifacts:
        lines.append(f"📦 {len(artifacts)} artifact workspace me saved")

    if PROVIDER == "demo":
        lines.append("")
        lines.append(
            f"_OFFLINE / DEMO MODE_ — key daalte hi yahi request {OWNER_NAME} ke configured brain "
            "se chalegi: research → browser → code → verify → answer. "
            "`python -m jarvis doctor` se check karo."
        )
    return "\n".join(lines)


# ===========================================================================
# 6. CRITIC — quality gate
# ===========================================================================
async def critic_node(state: JarvisState) -> dict[str, Any]:
    request = state.get("request", "")
    answer = state.get("answer", "")
    evidence = state.get("evidence", "")

    verdict = await asyncio.to_thread(dspy_mods.critic, request, answer, evidence)

    # offline heuristic critic
    if OFFLINE_MODE or PROVIDER == "demo" or not verdict.get("score"):
        score = 0.75
        if state.get("findings"):
            score += 0.15
        if state.get("errors"):
            score -= 0.2
        if len(answer) < 80:
            score -= 0.25
        blind_here = detect_blind_handoffs(state)
        missing: list[str] = []
        fix_hint = ""
        if blind_here:
            # blind handoff = sabse bada gap: proof ke bina claim — refine pakka
            score = min(score, 0.3)
            names = sorted({str(b.get("worker")) for b in blind_here})
            missing.append("blind handoff: bina tool proof wali findings (" + ", ".join(names) + ")")
            fix_hint = "blind worker(s) dobara chalao, is baar ASLI tool call karke: " + ", ".join(names)
        elif score < 0.6:
            missing.append("answer chhota/incomplete lag raha hai")
            fix_hint = "errors dekho aur failed worker dobara chalao"
        verdict = {"ok": score >= 0.6, "score": max(0.0, min(1.0, score)), "missing": missing, "fix_hint": fix_hint}

    return {
        "critique": verdict,
        "fix_hint": verdict.get("fix_hint", ""),
        "events": [_event(state, "critic", f"ok={verdict.get('ok')} score={verdict.get('score')}")],
    }


async def refine_node(state: JarvisState) -> dict[str, Any]:
    """
    Critic ke gap ko address karke workers dobara chalao (iteration++).
    LOOP GUARD: agar isi type ka evidence dobara ban raha hai (same signature) to
    refine rok do — warna agent same kaam repeat karta rehta hai (OpenClaw ka
    loop-detection guardrail, owner ka "loop me na phase" wala point).
    """
    import hashlib

    iteration = int(state.get("iterations", 0)) + 1
    hint = state.get("fix_hint") or "pichhla attempt adhoora tha — missing data complete karo"
    request = state.get("request", "")

    try:
        st = get_state()
        # session bhi sig me — warna kal same request aane par refine bhi block ho jata
        # (persistent store cross-session false positive deta tha).
        sig = hashlib.md5(
            f"{state.get('session_id', '')}|{evidence_digest(state, limit_per_item=400, total_limit=1500)}".encode()
        ).hexdigest()[:10]
        seen_count = st.seen_signature(sig)
        if seen_count >= 1:  # is session me yahi evidence pehle bhi mila tha
            st.remember_error("refine loop detected — same evidence dobara, refining band kar di")
            return {
                "iterations": MAX_GRAPH_ITERATIONS,  # critic ab persist par jayega
                "fix_hint": "",
                "events": [
                    _event(state, "refine", f"LOOP DETECTED (sig {sig}) — refining band, best answer bhej rahe hain")
                ],
            }
    except Exception:
        pass

    # TARGETED re-verify: sirf failed/blind workers dobara.
    # NOTE: findings/tool_evidence `operator.add` reducers hain — purane records hataye nahi
    # ja sakte, isliye blind findings digest me ⚠️UNVERIFIED mark ke saath rehte hain aur
    # re-run ke NAYE findings append hote hain. Same targets dobara fail hon to STALL rok deta hai.
    blind_names = {str(b.get("worker")) for b in detect_blind_handoffs(state)}
    failed_names = {str(r.get("worker") or "") for r in evidence_records(state) if not r.get("ok")}
    failed_names |= {
        str(f.get("worker") or "")
        for f in (state.get("findings") or [])
        if isinstance(f, dict) and (f.get("lazy") or not (f.get("tool_calls") or []))
    }
    failed_names.discard("")
    targets = sorted((blind_names | failed_names) & set(WORKER_NODES))
    if not targets:
        targets = [w for w in (state.get("workers") or []) if w in WORKER_NODES] or list(WORKER_NODES)
    prev_targets = state.get("refine_targets") or []
    if prev_targets and set(targets) == set(prev_targets):
        # STALL: wahi workers dobara fail/blind — aur refine bekaar, best answer bhejo
        try:
            get_state().remember_error(f"refine stalled — same targets failed twice: {', '.join(targets)}")
        except Exception:
            pass
        return {
            "iterations": MAX_GRAPH_ITERATIONS,
            "fix_hint": "",
            "dispatch_targets": [],
            "events": [
                _event(
                    state,
                    "refine",
                    f"STALLED (targets [{', '.join(targets)}] dobara fail) — refining band, best answer bhej rahe hain",
                )
            ],
        }
    return {
        "iterations": iteration,
        "request": f"{request}\n\n[REFINE PASS {iteration} — {hint} | re-verify: {', '.join(targets)}]",
        "critique": {},
        "refine_targets": targets,
        "dispatch_targets": targets,
        "events": [_event(state, "refine", f"pass {iteration}: re-verify [{', '.join(targets)}]")],
    }


# ===========================================================================
# 7. PERSIST + RESPOND
# ===========================================================================
async def persist_node(state: JarvisState) -> dict[str, Any]:
    request = state.get("request", "")
    answer = state.get("answer", "")
    mem = get_memory()
    try:
        mem.add_turn(request, answer, session=state.get("session_id", "default"))
        try:
            st = get_state()
            st.finish_task(
                status="done" if not state.get("errors") else "done_with_errors",
                result=answer[:300],
                artifacts=state.get("artifacts", []),
            )
            st.set_pending_steps([])
        except Exception:
            pass
        if state.get("task_id"):
            mem.finish_task(
                state["task_id"],
                status="done" if not state.get("errors") else "done_with_errors",
                result=answer[:4000],
                plan=state.get("plan", []),
                artifacts=state.get("artifacts", []),
            )
    except Exception as exc:
        return {"errors": [f"persist: {exc}"]}
    return {"events": [_event(state, "persist", "memory updated")]}


def build_reasoning_summary(state: JarvisState) -> list[str]:
    """Structured reasoning (PLAN/DECISION/ACTION/RESULT/VERIFICATION) — hidden CoT expose nahi karte."""
    lines: list[str] = []
    plan = state.get("plan") or []
    if plan:
        lines.append("PLAN: " + " → ".join(str(p)[:70] for p in plan[:4]))
    workers = state.get("workers") or []
    if workers:
        lines.append("DECISION: " + ", ".join(workers) + " worker(s) chune gaye")
    tools_used = state.get("selected_tools") or []
    if tools_used:
        lines.append("TOOLS: " + ", ".join(tools_used[:8]))
    findings = state.get("findings") or []
    if findings:
        lines.append("ACTION: " + " | ".join(f"{f.get('worker', '?')} {f.get('seconds', '?')}s" for f in findings[:5]))
    evidence = state.get("evidence") or ""
    if evidence:
        lines.append(f"RESULT: {len(evidence)} chars evidence, {len(state.get('artifacts') or [])} artifact(s)")
    try:
        bal = session_evidence_summary(state)
        lines.append(
            f"EVIDENCE: {bal['verified']}/{bal['tool_calls']} tools verified"
            + (f", {bal['failed']} failed" if bal["failed"] else "")
            + (f", blind: {', '.join(bal['blind_findings'])}" if bal["blind_findings"] else "")
            + f" → {bal['status']}"
        )
    except Exception:
        pass
    critique = state.get("critique") or {}
    if critique:
        lines.append("VERIFICATION: " + str(critique.get("verdict") or critique.get("score") or "ok")[:100])
    if state.get("iterations"):
        lines.append(f"ITERATIONS: {state['iterations']} refine loop(s)")
    if state.get("errors"):
        lines.append("ERRORS: " + "; ".join(str(e)[:70] for e in (state.get("errors") or [])[:2]))
    return lines


async def respond_node(state: JarvisState) -> dict[str, Any]:
    answer = state.get("answer", "")
    spoken = ""
    if answer and not (OFFLINE_MODE or PROVIDER == "demo"):
        spoken = await asyncio.to_thread(dspy_mods.voice_polish, answer)
    # voice ke liye hamesha emoji/markdown-free text (TTS emoji ka naam nahi bolega)
    try:
        from ..voice.speech import for_speech, summary_for_speech

        spoken = summary_for_speech(spoken or answer, max_sentences=4) if answer else ""
        spoken = for_speech(spoken) or _shorten(answer)
    except Exception:
        spoken = spoken or _shorten(answer)
    # Status ko *kabhi* blindly "done" mat karo — honesty guard/invariant ne failure set kiya ho
    # to wahi upstream (AgentResult.status, Telegram footer, CLI) tak jaana chahiye.
    status = state.get("status") or "done"
    if status not in ("failed", "partial_failure", "error"):
        status = "done"
    return {
        "spoken": spoken,
        "reasoning_summary": build_reasoning_summary(state),
        "status": status,
        "messages": [AIMessage(content=answer)],
        "events": [_event(state, "respond", f"spoken={len(spoken)} chars • status={status}")],
    }


def _shorten(text: str, limit: int = 420) -> str:
    text = re.sub(r"```.*?```", " (code)", text or "", flags=re.S)
    text = re.sub(r"[#*_>]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = cut.rfind(". ")
    return (cut[: dot + 1] if dot > 120 else cut).strip()


# ===========================================================================
# routing helpers (graph edges)
# ===========================================================================
def after_route(state: JarvisState) -> str:
    if state.get("needs_clarification"):
        return "clarify"
    return "plan"


def dispatch(state: JarvisState) -> Any:
    """Workers ko parallel bhejo (LangGraph Send fan-out).

    Refine pass me `dispatch_targets` subset hota hai (sirf failed/blind workers dobara) —
    warna poori `workers` list.
    """
    from langgraph.types import Send

    workers = state.get("dispatch_targets") or state.get("workers", []) or ["answer"]
    real_workers = [w for w in workers if w in WORKER_NODES]
    if not real_workers:
        return "synthesize"
    payload = {
        "request": state.get("request", ""),
        "plan": state.get("plan", []),
        "workers": workers,
        "session_id": state.get("session_id", "default"),
        "memory_context": state.get("memory_context", ""),
        "owner_facts": state.get("owner_facts", ""),
        "iterations": state.get("iterations", 0),
    }
    return [Send(WORKER_NODES[w].__name__, payload) for w in real_workers]


def after_critic(state: JarvisState) -> str:
    verdict = state.get("critique") or {}
    ok = bool(verdict.get("ok", True))
    iterations = int(state.get("iterations", 0))
    if ok or iterations >= MAX_GRAPH_ITERATIONS:
        return "persist"
    return "refine"
