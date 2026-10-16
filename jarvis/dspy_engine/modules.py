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

import ast
import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from copy import deepcopy
from typing import Any

from ..config import PROVIDER, USE_DSPY, is_offline
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
_LOG = logging.getLogger(__name__)
_READY = False
_CACHE_TTL_S = 300.0
_CACHE_MAX_ENTRIES = 512
_RESULT_CACHE: OrderedDict[tuple[str, str, str], tuple[float, Any]] = OrderedDict()
_RESULT_CACHE_LOCK = threading.RLock()


def parse_bool(value: Any, default: bool = False) -> bool:
    """Parse DSPy booleans without Python's truthiness trap for strings like ``"False"``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "y", "1", "on"}:
        return True
    if normalized in {"false", "no", "n", "0", "off", ""}:
        return False
    return default


def parse_list(value: Any) -> list[str]:
    """Normalize list-shaped DSPy outputs whether they arrive typed or as text."""
    if value is None:
        return []
    parsed = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        for loader in (json.loads, ast.literal_eval):
            try:
                candidate = loader(text)
            except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
                continue
            if isinstance(candidate, (list, tuple, set)):
                parsed = candidate
                break
            if isinstance(candidate, str) and candidate != text:
                parsed = candidate
                break
        if isinstance(parsed, str):
            parsed = re.split(r"[,;\\n]+", parsed)
    if isinstance(parsed, dict):
        return []
    if not isinstance(parsed, Iterable):
        parsed = [parsed]
    return [str(item).strip().strip("\\\"' ") for item in parsed if str(item).strip().strip("\\\"' ")]


def _request_hash(request: str) -> str:
    return hashlib.sha256((request or "").strip().encode("utf-8")).hexdigest()


def _cache_key(kind: str, session_id: str, request: str) -> tuple[str, str, str]:
    return kind, str(session_id or "default"), _request_hash(request)


def _cache_get(kind: str, session_id: str, request: str) -> Any | None:
    key = _cache_key(kind, session_id, request)
    with _RESULT_CACHE_LOCK:
        entry = _RESULT_CACHE.get(key)
        if entry is None:
            return None
        created_at, value = entry
        if time.monotonic() - created_at > _CACHE_TTL_S:
            _RESULT_CACHE.pop(key, None)
            return None
        _RESULT_CACHE.move_to_end(key)
        return deepcopy(value)


def _cache_put(kind: str, session_id: str, request: str, value: Any) -> None:
    key = _cache_key(kind, session_id, request)
    with _RESULT_CACHE_LOCK:
        _RESULT_CACHE[key] = (time.monotonic(), deepcopy(value))
        _RESULT_CACHE.move_to_end(key)
        while len(_RESULT_CACHE) > _CACHE_MAX_ENTRIES:
            _RESULT_CACHE.popitem(last=False)


def clear_request_cache(session_id: str, request: str) -> None:
    """Discard one turn's cached route/plan results after its graph run ends."""
    request_digest = _request_hash(request)
    session = str(session_id or "default")
    with _RESULT_CACHE_LOCK:
        for key in list(_RESULT_CACHE):
            if key[1] == session and key[2] == request_digest:
                _RESULT_CACHE.pop(key, None)


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


def _safe(call: Callable[[], Any], default: Any, *, stage: str = "DSPy", role: str = "fast") -> Any:
    """Run one role-bound DSPy call; use deterministic fallback, but report live failures."""
    if is_offline() or PROVIDER == "demo":
        return default
    from ..runtime.progress import emit

    if not ensure_ready():
        emit("warn", f"DSPy {stage} unavailable; deterministic fallback used", status="skipped")
        return default
    started = __import__("time").monotonic()
    label = f"DSPy {stage} ({role})"
    try:
        from ..llm import dspy_role_context

        emit("model_start", f"{label} request started", tool=label, status="running")
        with dspy_role_context(role):
            result = call()
        elapsed = int((__import__("time").monotonic() - started) * 1000)
        emit("model_end", f"{label} response received", tool=label, status="ok", ms=elapsed)
        return result
    except Exception as exc:
        elapsed = int((__import__("time").monotonic() - started) * 1000)
        emit("model_end", f"{label} failed ({type(exc).__name__})", tool=label,
             status="fail", ms=elapsed)
        emit("warn", f"{label} failed; deterministic fallback used", status="fail")
        return default


# ---------------------------------------------------------------------------
# router
# ---------------------------------------------------------------------------
def _contains_keyword(text: str, keyword: str) -> bool:
    """Match a keyword/phrase as a whole token, not an accidental substring (e.g. site/opposite)."""
    if keyword == "http":
        return bool(re.search(r"\bhttps?://", text, re.I))
    escaped = re.escape(keyword).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<!\w){escaped}(?!\w)", text, re.I))


_KEYWORD_ROUTES: list[tuple[tuple[str, ...], list[str]]] = [
    (("browser", "website", "site", "scrape", "login", "form bhar", "http", "web se", "online"), ["browser"]),
    (("search", "khoj", "dhoondh", "research", "compare", "news", "kya hai", "kitna",
      "best", "recommend", "suggest", "konsa", "kaun sa",
      "mausam", "weather", "temperature", "latest", "aaj ka", "khabar", "score", "rate kya"), ["research"]),
    (("code", "script", "program", "automate", "plot", "chart", "calculate", "run kar"), ["code"]),
    (("notepad", "note", "notes", "save kar", "file khol", "app khol", "band kar", "volume", "screenshot", "folder"), ["desktop"]),
    (("khol do", "kholo", "khol de", "open karo", "open kar do", "chalu kar", "launch karo",
      "band kar do", "process", "background", "terminal", "command chala", "shell"), ["desktop"]),
    (("refresh", "reload", "cache clear", "cache saaf", "system status", "process clear",
      "process band", "restart", "automation", "schedule karo", "remind", "yaad dila",
      "undo", "revert file", "restore file", "har subah", "har ghante", "every day", "daily"), ["desktop"]),
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
    has_noun = any(_contains_keyword(low, noun) for noun in _LOCAL_NOUNS)
    has_verb = any(re.search(rf"(?<!\w){re.escape(verb)}\w*(?!\w)", low) for verb in _LOCAL_VERBS)
    return has_noun and has_verb


def fallback_route(request: str) -> list[str]:
    low = (request or "").lower()
    picked: list[str] = []
    for keys, workers in _KEYWORD_ROUTES:
        if any(_contains_keyword(low, keyword) for keyword in keys):
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
    if len(low.split()) <= 12 and any(_contains_keyword(low, k) for k in (
        "time", "samay", "date", "tareekh", "din", "timestamp", "utc", "timezone", "time zone", "baje"
    )) and not any(_contains_keyword(low, b) for b in (
        "mausam", "weather", "news", "schedule", "remind", "automation", "save", "browser", "laao", "search"
    )):
        return ["answer"] if "answer" not in picked else picked[:1]

    # ---- local vs web correction ----
    local = _local_intent(low)
    web = any(_contains_keyword(low, keyword) for keyword in _WEB_INTENT)
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


def route(request: str, history: str = "", session_id: str = "default") -> dict[str, Any]:
    """Request → workers. Fast model, per-session/request cached; fallback is keyword routing."""
    cached = _cache_get("route", session_id, request)
    if cached is not None:
        return cached

    allowed = {"research", "browser", "code", "desktop", "memory", "answer"}

    def _call():
        pred = get_module("router")
        if pred is None:
            raise RuntimeError("no router")
        out = pred(request=request[:2000], history=(history or "")[:1500])
        workers = [worker.lower() for worker in parse_list(getattr(out, "workers", None))]
        invalid_workers = not workers or any(worker not in allowed for worker in workers)
        return {
            "workers": list(dict.fromkeys(workers)),
            "reason": str(getattr(out, "reason", "") or ""),
            "needs_clarification": parse_bool(getattr(out, "needs_clarification", False)),
            "_invalid_workers": invalid_workers,
        }

    result = _safe(_call, None, stage="route", role="fast")
    if not result:
        _LOG.warning("DSPy router unavailable; using keyword fallback")
        result = {"workers": fallback_route(request), "reason": "keyword fallback", "needs_clarification": False}
    elif result.pop("_invalid_workers", False):
        _LOG.warning("DSPy router returned empty/unsupported workers; using keyword fallback")
        result = {"workers": fallback_route(request), "reason": "keyword fallback", "needs_clarification": False}
    _cache_put("route", session_id, request, result)
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


def plan(request: str, workers: list[str], context: str = "", session_id: str = "default") -> dict[str, Any]:
    cached = _cache_get("plan", session_id, request)
    if cached is not None:
        return cached

    def _call():
        pred = get_module("planner")
        if pred is None:
            raise RuntimeError("no planner")
        out = pred(request=request[:2000], workers=workers, context=context[:3000])
        steps = [step.strip(" -•") for step in parse_list(getattr(out, "steps", None)) if step.strip(" -•")]
        return {"steps": steps[:8], "deliverable": str(getattr(out, "deliverable", "") or "")}

    result = _safe(_call, None, stage="planner", role="planner")
    if not result or not result.get("steps"):
        result = _fallback_plan(request, workers)
    _cache_put("plan", session_id, request, result)
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

    return _safe(_call, None, stage="code writer", role="code")


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

    result = _safe(_call, None, stage="field extraction", role="extract")
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

    return _safe(_call, None, stage="row normalization", role="extract") or rows


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
        missing = parse_list(getattr(out, "missing", None))
        return {
            "ok": parse_bool(getattr(out, "ok", False)),
            "score": score,
            "missing": missing,
            "fix_hint": str(getattr(out, "fix_hint", "") or ""),
        }

    if is_offline() or PROVIDER == "demo":
        return {"ok": True, "score": 0.7, "missing": [], "fix_hint": "", "skipped": "offline"}
    result = _safe(_call, None, stage="critic", role="critic")
    if result is None:
        return {
            "ok": True,
            "score": 0.0,
            "missing": [],
            "fix_hint": "",
            "skipped": "critic-unavailable",
            "error": "critic unavailable; answer kept without refinement",
        }
    return result


def final_answer(request: str, results: str, critique_text: str = "") -> str:
    def _call():
        mod = get_module("final_answer")
        if mod is None:
            raise RuntimeError("no final module")
        out = mod(request=request[:1500], results=results[:8000], critique=critique_text[:2000])
        return str(out.answer).strip()

    return _safe(_call, "", stage="final answer", role="brain") or ""


def voice_polish(written: str) -> str:
    def _call():
        mod = get_module("voice")
        if mod is None:
            raise RuntimeError("no voice module")
        return str(mod(written=written[:4000]).spoken).strip()

    return _safe(_call, "", stage="voice polish", role="fast") or written[:400]


# ---------------------------------------------------------------------------
# browser task spec
# ---------------------------------------------------------------------------
def website_task_spec(request: str, known_sites: list[str] | None = None) -> dict[str, Any]:
    def _call():
        mod = get_module("website_spec")
        if mod is None:
            raise RuntimeError("no website spec module")
        out = mod(request=request[:1500], known_sites=known_sites or [])
        fields = parse_list(getattr(out, "fields_to_extract", None))
        return {
            "browser_task": str(getattr(out, "browser_task", "") or "").strip(),
            "fields": fields[:8],
            "save_note_as": str(getattr(out, "save_note_as", "") or "").strip(),
        }

    result = _safe(_call, None, stage="browser task spec", role="planner")
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
