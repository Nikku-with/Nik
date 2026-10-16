"""Web research worker with parallel search-result fetching."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ..state import JarvisState
from .common import _event, _extract_urls, _finding, _permission_block


async def research_worker(state: JarvisState) -> dict[str, Any]:
    """Web search → parallel page fetch → DSPy structured extraction."""
    blocked = _permission_block(state, "research", "web_search")
    if blocked:
        return blocked
    request = state.get("request", "")
    refine_hint = state.get("refine_hint", "")
    query = f"{request}\nRefine hint: {refine_hint}" if refine_hint else request
    started = time.time()
    sites = _extract_urls(request)
    done_actions = dict(state.get("done_actions", {}) or {})

    from ...dspy_engine.modules import extract_many
    from ...tools.web_tools import fetch_url_text, web_search_results
    from ..idempotency import completed_action, remember_completed_action

    fields = _guess_fields(request)
    rows: list[dict[str, Any]] = []
    sources: list[str] = []
    search_rows: list[dict[str, Any]] = []

    if sites:
        sources = sites
        pairs: list[tuple[str, str]] = []
        async def _fetch(u: str):
            fetch_args = {"url": u if u.startswith("http") else f"https://{u}", "max_chars": 10000}
            cached = completed_action(done_actions, "fetch_url_text", fetch_args)
            if cached:
                return u, str(cached.get("result", cached.get("output", "")))
            try:
                text = await fetch_url_text(fetch_args["url"], max_chars=fetch_args["max_chars"])
                if "FETCH FAILED" not in str(text)[:80]:
                    remember_completed_action(
                        done_actions, "fetch_url_text", fetch_args, text, output=str(text), worker="research"
                    )
                return u, text
            except Exception as exc:
                return u, f"[FETCH FAILED: {exc}]"
        pairs = list(await asyncio.gather(*(_fetch(s) for s in sites[:6])))
    else:
        search_args = {"query": query, "num": 6}
        cached_search = completed_action(done_actions, "web_search_results", search_args)
        if cached_search:
            search_rows = cached_search.get("result", []) or []
        else:
            search_rows = await web_search_results(query, num=6)
            remember_completed_action(
                done_actions, "web_search_results", search_args, search_rows,
                output=f"{len(search_rows)} search result(s)", worker="research",
            )
        top = [r for r in search_rows if r.get("url")][:5]
        sources = [r["url"] for r in top]
        # parallel fetch (sasta fast path) — browser se pehle
        async def _fetch_r(u: str):
            fetch_args = {"url": u, "max_chars": 8000}
            cached = completed_action(done_actions, "fetch_url_text", fetch_args)
            if cached:
                return u, str(cached.get("result", cached.get("output", "")))
            try:
                text = await fetch_url_text(u, max_chars=8000)
                if "FETCH FAILED" not in str(text)[:80]:
                    remember_completed_action(
                        done_actions, "fetch_url_text", fetch_args, text, output=str(text), worker="research"
                    )
                return u, text
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

    summary = (
        f"query: {request[:200]}\n"
        f"sources fetched: {len(pairs)} (parallel)\n{search_table}\n\n"
        f"{digest[:6000]}"
        + ("\n\nstructured rows:\n" + json.dumps(rows, indent=2, ensure_ascii=False)[:2500] if rows else "")
    )

    fetched_ok = len(good_pairs)
    researched_ok = bool(fetched_ok or search_rows)
    evidence = {
        "worker": "research",
        "name": "web_search_results+fetch_url_text",
        "args": {"query": query[:200], "sites": sites[:6]},
        "output": (f"search_rows={len(search_rows)} pages_fetched_ok={fetched_ok}/{len(pairs)}"
                   + (f" first={sources[0]}" if sources else "")),
        "ok": researched_ok,
        "source": "worker",
        "seconds": round(time.time() - started, 1),
    }
    return {
        "findings": [_finding("research", summary, sites=sources, rows=rows, seconds=round(time.time() - started, 1))],
        # read-only research: claim "maine dhundh liya" tabhi verify hota hai jab fetch/search asli chala ho
        "tool_evidence": [evidence],
        "done_actions": done_actions,
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
