"""
jarvis/dspy_engine/modules.py — DSPy modules jo Jarvis ke dimaag ko sharp karte hain
=================================================================================

Har module ka ek clear kaam hai, aur har ek ka **deterministic fallback** bhi hai —
matlab LLM/DSPy fail ho ya offline mode ho, poora Jarvis system phir bhi chalta rahega.

  router()            → request ko workers me route karo (ultra-fast, chhota model)
  planner()           → executable plan
  code_smith()        → code likho (code engine ka stage-1)
  extract_structured()→ page text → JSON fields
  normalize_rows()    → multi-site rows ko comparable banao
  critic()            → answer ki quality check
  final_answer()      → Hinglish answer
  voice_polish()      → bolne layak short version
  website_task_spec() → owner request → browser-agent prompt + fields

Speed tricks jo lagi hain:
  * routing/planning fast model par (gemini-2.5-flash-lite / gpt-4.1-mini class)
  * dspy.LM(..., cache=True) → same query dobara par 0 cost
  * parallel multi-site extraction: dspy.Parallel + asyncio.gather
  * Refine/BestOfN sirf wahan jahan quality matter karti hai (plan, answer)
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable

from ..config import OFFLINE_MODE, PROVIDER, USE_DSPY
from .signatures import (
    HAS_DSPY,
    CritiqueAnswer,
    ExtractFields,
    FinalAnswer,
    MakePlan,
    NormalizeRows,
    RouteIntent,
    VoicePolish,
    WebsiteTaskSpec,
    WriteCode,
)

_MODULES: dict[str, Any] = {}
_READY = False


# ---------------------------------------------------------------------------
# init / registry
# ---------------------------------------------------------------------------
def ensure_ready() -> bool:
    """DSPy configure karo + optimized prompts load karo (agar compiled mile)."""
    global _READY
    if _READY:
        return True
    if not USE_DSPY:
        return False
    try:
        from ..llm import init_dspy

        init_dspy("fast")
        _load_optimized()
        _READY = True
        return True
    except Exception:
        return False


def _load_optimized() -> None:
    """optimized/*.json me compiled program mile to load kar lo (GEPA/MIPROv2 ka output)."""
    try:
        from .optimize import load_compiled

        for name in ("router", "planner", "code_smith", "extract", "final_answer"):
            prog = load_compiled(name)
            if prog is not None:
                _MODULES[name] = prog
    except Exception:
        pass


def get_module(name: str):
    """Compiled module mile to wo, warna base module (lazy). dspy na ho to None (fallback chalega)."""
    if not HAS_DSPY:
        return None
    ensure_ready()
    if name in _MODULES:
        return _MODULES[name]
    try:
        import dspy
    except Exception:
        return None

    builders: dict[str, Callable[[], Any]] = {
        "router": lambda: dspy.Predict(RouteIntent),
        "planner": lambda: dspy.ChainOfThought(MakePlan),
        "code_smith": lambda: dspy.ChainOfThought(WriteCode),
        "extract": lambda: dspy.ChainOfThought(ExtractFields),
        "normalize": lambda: dspy.ChainOfThought(NormalizeRows),
        "critic": lambda: dspy.ChainOfThought(CritiqueAnswer),
        "final_answer": lambda: dspy.ChainOfThought(FinalAnswer),
        "voice": lambda: dspy.Predict(VoicePolish),
        "website_spec": lambda: dspy.ChainOfThought(WebsiteTaskSpec),
    }
    builder = builders.get(name)
    if builder is None:
        return None
    try:
        module = builder()
        _MODULES[name] = module
        return module
    except Exception:
        return None


def _safe(call: Callable[[], Any], default: Any) -> Any:
    """DSPy call ko try karo; fail ho to default (system kabhi crash na ho)."""
    if OFFLINE_MODE or PROVIDER == "demo":
        return default
    if not ensure_ready():
        return default
    try:
        return call()
    except Exception:
        return default


# ---------------------------------------------------------------------------
# router
# ---------------------------------------------------------------------------
_KEYWORD_ROUTES: list[tuple[tuple[str, ...], list[str]]] = [
    (("browser", "website", "site", "scrape", "login", "form bhar", "http", "web se", "online"), ["browser"]),
    (("search", "khoj", "dhoondh", "research", "compare", "news", "kya hai", "kitna",
      "best", "recommend", "suggest", "konsa", "kaun sa",
      "mausam", "weather", "temperature", "latest", "aaj ka", "khabar", "score", "rate kya"), ["research"]),
    (("code", "script", "program", "automate", "plot", "chart", "calculate", "run kar"), ["code"]),
    (("notepad", "note", "save kar", "file khol", "app khol", "band kar", "volume", "screenshot", "folder"), ["desktop"]),
    (("khol do", "kholo", "khol de", "open karo", "open kar do", "chalu kar", "launch karo",
      "band kar do", "process", "background", "terminal", "command chala", "shell"), ["desktop"]),
    (("refresh", "reload", "cache clear", "cache saaf", "system status", "process clear",
      "process band", "restart", "automation", "schedule karo", "remind", "yaad dila",
      "har subah", "har ghante", "every day", "daily"), ["desktop"]),
    (("yaad rakh", "remember", "bhool gaya", "recall", "mujhe kya pata", "memory", "memories"), ["memory"]),
]

# --- local (machine) vs web intent — file/app tasks ko galti se research me na bhejein ---
_LOCAL_NOUNS = ("file", "files", "folder", "folders", "note", "notes", "notepad", "pdf", "docx", "excel",
                "csv", "document", "workspace", "app", "application", "process", "terminal", "shell",
                "repl", "screenshot", "resume", "report", "sheet", "path", "drive")
_LOCAL_VERBS = ("dhoondh", "find", "padho", "padh", "read", "khol", "open", "launch", "chala", "locate",
                "dekh", "search", "nikaal", "dikhao", "summary", "summar")
_WEB_INTENT = ("website", "site", "web", "online", "internet", "browser", "scrape", "news", "price",
               "kitna", "mausam", "weather", "google", "amazon", "flipkart", "youtube", "http")


def _local_intent(low: str) -> bool:
    """Machine ke andar ka kaam? (file/app/process) — noun + verb dono chahiye."""
    return (any(f" {n} " in f" {low} " or low.startswith(n) for n in _LOCAL_NOUNS)
            and any(v in low for v in _LOCAL_VERBS))


def fallback_route(request: str) -> list[str]:
    low = (request or "").lower()
    picked: list[str] = []
    for keys, workers in _KEYWORD_ROUTES:
        if any(k in low for k in keys):
            picked.extend(workers)

    # ---- automation creation: "har subah 8 baje X" → sirf desktop (abhi research nahi karni) ----
    automation_rx = (
        r"\b(har\s+(subah|shaam|sham|raat|din|ghante?|ghanta)\b.*\d"
        r"|daily\s*\d"
        r"|every\s*(day|hour|\d+\s*(min|h))"
        r"|(kal|tomorrow|aaj)\s*\d{0,2}\s*(baje|am|pm|:)"
        r"|remind me|yaad dila|schedule karo|automation banao)"
    )
    if re.search(automation_rx, low):
        return ["desktop"]          # automation banao, abhi research mat karo

    # ---- pure time/date sawaal: local clock hi kaafi hai, web research zabardasti mat karo ----
    if len(low.split()) <= 12 and any(k in low for k in ("time", "samay", "date", "tareekh", "din",
                                                         "timestamp", "utc", "timezone", "baje")) \
            and not any(b in low for b in ("mausam", "weather", "news", "schedule", "remind",
                                           "automation", "save", "browser", "laao", "search")):
        return ["answer"] if "answer" not in picked else picked[:1]

    # ---- local vs web correction ----
    local = _local_intent(low)
    web = any(k in low for k in _WEB_INTENT)
    if local and not web:
        # "notes file dhoondh ke padho" → ye web research NAHI hai
        picked = [w for w in picked if w not in ("research", "browser")]
        if not picked or "desktop" not in picked:
            picked.insert(0, "desktop")
    elif local and web:
        # "web se data laake note me save karo" → dono chahiye
        if "desktop" not in picked:
            picked.append("desktop")

    if not picked:
        picked = ["answer"]
    # dedupe keep order, max 3
    out: list[str] = []
    for w in picked:
        if w not in out:
            out.append(w)
    return out[:3]


def route(request: str, history: str = "") -> dict[str, Any]:
    """Request → workers. Fast model, cached. Fallback: keyword routing."""
    def _call():
        pred = get_module("router")
        if pred is None:
            raise RuntimeError("no router")
        out = pred(request=request[:2000], history=(history or "")[:1500])
        workers = out.workers
        if isinstance(workers, str):
            workers = [w.strip() for w in re.split(r"[,;]", workers) if w.strip()]
        workers = [str(w).lower().strip() for w in (workers or [])]
        workers = [w for w in workers if w in {"research", "browser", "code", "desktop", "memory", "answer"}]
        return {
            "workers": workers or fallback_route(request),
            "reason": getattr(out, "reason", ""),
            "needs_clarification": bool(getattr(out, "needs_clarification", False)),
        }

    result = _safe(_call, None)
    if not result:
        result = {"workers": fallback_route(request), "reason": "keyword fallback", "needs_clarification": False}
    return result


# ---------------------------------------------------------------------------
# planner
# ---------------------------------------------------------------------------
def _fallback_plan(request: str, workers: list[str]) -> dict[str, Any]:
    steps = [f"Owner ki request samjho: {request[:120]}"]
    if "browser" in workers:
        steps.append("browser-use agent ko precise task prompt do aur browser khol ke data/action karwao")
    if "research" in workers:
        steps.append("web search + parallel page fetch se sources collect karo")
    if "code" in workers:
        steps.append("code likho → chalao → error aaye to khud fix karo (self-heal)")
    if "desktop" in workers:
        steps.append("local machine par files/apps/notes handle karo")
    if "memory" in workers:
        steps.append("memory se recall / naya fact save karo")
    steps.append("results ko verify karo aur owner ko Hinglish me short summary do")
    return {"steps": steps, "deliverable": "verified answer + saved artifacts"}


def plan(request: str, workers: list[str], context: str = "") -> dict[str, Any]:
    def _call():
        pred = get_module("planner")
        if pred is None:
            raise RuntimeError("no planner")
        out = pred(request=request[:2000], workers=workers, context=context[:3000])
        steps = out.steps
        if isinstance(steps, str):
            steps = [s.strip(" -•") for s in steps.split("\n") if s.strip()]
        return {"steps": [str(s) for s in (steps or [])][:8], "deliverable": str(getattr(out, "deliverable", ""))}

    result = _safe(_call, None)
    if not result or not result.get("steps"):
        result = _fallback_plan(request, workers)
    return result


# ---------------------------------------------------------------------------
# code smith (code engine ka stage-1)
# ---------------------------------------------------------------------------
def code_smith(task: str, language: str = "python", feedback: str = "none", previous: str = "none") -> Any:
    def _call():
        mod = get_module("code_smith")
        if mod is None:
            raise RuntimeError("no code module")
        return mod(task=task[:3000], language=language, feedback=feedback[:2500], previous=previous[:6000])

    return _safe(_call, None)


# ---------------------------------------------------------------------------
# extraction (page text → JSON)
# ---------------------------------------------------------------------------
def _fallback_extract(source_text: str, source_url: str, fields: list[str]) -> dict[str, Any]:
    """Regex/heuristic extraction — LLM ke bina bhi kuch na kuch nikalta hai."""
    data: dict[str, Any] = {}
    text = source_text or ""
    for field in fields:
        value: Any = None
        rx = re.compile(rf"{re.escape(field)}\s*[:\-–]\s*([^\n|]{{2,120}})", re.I)
        m = rx.search(text)
        if m:
            value = m.group(1).strip()
        elif field.lower() in ("url", "source", "source_url", "link"):
            value = source_url
        elif field.lower() in ("title", "name"):
            m2 = re.search(r"(?im)^\s*#?\s*(.{5,120})$", text)
            value = m2.group(1).strip() if m2 else None
        data[field] = value
    return data


def extract_structured(source_text: str, source_url: str, fields: list[str]) -> dict[str, Any]:
    """Page text → structured dict (DSPy; fallback: regex heuristics)."""
    if not fields:
        return {}

    def _call():
        mod = get_module("extract")
        if mod is None:
            raise RuntimeError("no extract module")
        out = mod(source_text=(source_text or "")[:12000], source_url=source_url, field_names=fields)
        data = out.data
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, dict):
            data["source_url"] = source_url
            return data
        raise RuntimeError("bad extraction")

    result = _safe(_call, None)
    if not result:
        result = _fallback_extract(source_text, source_url, fields)
        result["source_url"] = source_url
        result["_extractor"] = "heuristic-fallback"
    return result


async def extract_many(sources: list[tuple[str, str]], fields: list[str]) -> list[dict[str, Any]]:
    """[(url, text)] → rows. Ye parallel chalta hai (asyncio + dspy threads)."""
    async def _one(url: str, text: str) -> dict[str, Any]:
        return await asyncio.to_thread(extract_structured, text, url, fields)

    return list(await asyncio.gather(*(_one(u, t) for u, t in sources)))


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------
def normalize_rows(rows: list[dict[str, Any]], fields: list[str]) -> list[dict[str, Any]]:
    def _call():
        mod = get_module("normalize")
        if mod is None:
            raise RuntimeError("no normalize module")
        out = mod(rows_json=json.dumps(rows, ensure_ascii=False)[:8000], field_names=fields)
        data = out.normalized_json
        if isinstance(data, str):
            data = json.loads(data)
        return data if isinstance(data, list) else None

    return _safe(_call, None) or rows


# ---------------------------------------------------------------------------
# critic + final answer + voice
# ---------------------------------------------------------------------------
def critic(request: str, draft: str, evidence: str = "") -> dict[str, Any]:
    def _call():
        mod = get_module("critic")
        if mod is None:
            raise RuntimeError("no critic")
        out = mod(request=request[:1500], draft=draft[:4000], evidence=(evidence or "")[:4000])
        score = out.score
        try:
            score = float(score)
        except Exception:
            score = 0.6
        missing = out.missing
        if isinstance(missing, str):
            missing = [m.strip() for m in missing.split("\n") if m.strip()]
        return {"ok": bool(out.ok), "score": score, "missing": missing or [], "fix_hint": str(getattr(out, "fix_hint", ""))}

    return _safe(_call, {"ok": True, "score": 0.7, "missing": [], "fix_hint": ""})


def final_answer(request: str, results: str, critique_text: str = "") -> str:
    def _call():
        mod = get_module("final_answer")
        if mod is None:
            raise RuntimeError("no final module")
        out = mod(request=request[:1500], results=results[:8000], critique=critique_text[:2000])
        return str(out.answer).strip()

    return _safe(_call, "") or ""


def voice_polish(written: str) -> str:
    def _call():
        mod = get_module("voice")
        if mod is None:
            raise RuntimeError("no voice module")
        return str(mod(written=written[:4000]).spoken).strip()

    return _safe(_call, "") or written[:400]


# ---------------------------------------------------------------------------
# browser task spec
# ---------------------------------------------------------------------------
def website_task_spec(request: str, known_sites: list[str] | None = None) -> dict[str, Any]:
    def _call():
        mod = get_module("website_spec")
        if mod is None:
            raise RuntimeError("no website spec module")
        out = mod(request=request[:1500], known_sites=known_sites or [])
        fields = out.fields_to_extract
        if isinstance(fields, str):
            fields = [f.strip() for f in re.split(r"[,;\n]", fields) if f.strip()]
        return {
            "browser_task": str(out.browser_task).strip(),
            "fields": [str(f) for f in (fields or [])][:8],
            "save_note_as": str(getattr(out, "save_note_as", "") or "").strip(),
        }

    result = _safe(_call, None)
    if result and result.get("browser_task"):
        return result
    # fallback: request ko hi browser task banao
    return {
        "browser_task": (
            f"Open a browser and complete this task for the owner: {request}. "
            "Work step by step, handle popups, and report exact data with source URLs."
        ),
        "fields": [],
        "save_note_as": "",
    }


# ---------------------------------------------------------------------------
# parallel multi-site probing without a browser (sasta first pass)
# ---------------------------------------------------------------------------
async def probe_sites(sites: list[str], fields: list[str]) -> list[dict[str, Any]]:
    """HTTP fast-path: sites ko parallel fetch karo + DSPy se fields nikaalo.
    Browser-use tab lagao jab ye fail ho (bot-protection/JS)."""
    from ..tools.web_tools import fetch_url_text

    async def _fetch(u: str) -> tuple[str, str]:
        try:
            return u, await fetch_url_text(u if u.startswith("http") else f"https://{u}", max_chars=10000)
        except Exception as exc:
            return u, f"[FETCH FAILED: {exc}]"

    pairs = await asyncio.gather(*(_fetch(s) for s in sites))
    return await extract_many(list(pairs), fields)
