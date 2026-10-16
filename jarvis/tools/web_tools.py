"""
jarvis/tools/web_tools.py — Search / weather / time / page fetch / parallel deep research
=======================================================================================

Original `Jarvis_google_search.py` + `jarvis_get_whether.py` ka upgrade:
  * Google Custom Search (agar key hai) → warna DuckDuckGo HTML fallback (key-free)
  * weather: OpenWeather (key) → warna wttr.in (key-free)
  * fetch_page_text: httpx + HTML→text (browser-use se pehle sasta option — browser
    sirf tab kholo jab page JS-heavy / login wala ho)
  * deep_research(): ek query par sites PARALLEL fetch (asyncio.gather) — ye wahi
    "alag alag websites se data laao" ka fast path hai
"""
from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime

import httpx
from langchain_core.tools import tool

from ..config import BRAVE_API_KEY, CITY, SEARX_URL, TIMEZONE, env
from ..timezone import configured_timezone

IST = configured_timezone()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# ---------------------------------------------------------------------------
# HTML → text
# ---------------------------------------------------------------------------
def html_to_text(raw: str, max_chars: int = 12000) -> str:
    if not raw:
        return ""
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "form"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
    except Exception:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", text)).strip()
    return text[:max_chars]


MAX_PAGE_RESPONSE_BYTES = 2 * 1024 * 1024


async def fetch_url_text(url: str, max_chars: int = 12000, timeout: float = 25.0) -> str:
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers={"User-Agent": UA}) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                remaining = MAX_PAGE_RESPONSE_BYTES - len(body)
                if remaining <= 0:
                    break
                body.extend(chunk[:remaining])
                if len(chunk) > remaining or len(body) >= MAX_PAGE_RESPONSE_BYTES:
                    break
            encoding = response.encoding or "utf-8"
            raw_text = bytes(body).decode(encoding, errors="replace")
            return html_to_text(raw_text, max_chars=max_chars)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
def _google_cse_sync(query: str, num: int = 5) -> list[dict]:
    key = env("GOOGLE_SEARCH_API_KEY") or env("GOOGLE_API_KEY")
    cx = env("SEARCH_ENGINE_ID")
    if not key or not cx:
        return []
    import requests
    r = requests.get(
        "https://www.googleapis.com/customsearch/v1",
        params={"key": key, "cx": cx, "q": query, "num": min(num, 10)},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"CSE {r.status_code}: {r.text[:200]}")
    return [
        {"title": it.get("title", ""), "url": it.get("link", ""), "snippet": it.get("snippet", "")}
        for it in r.json().get("items", [])
    ]


async def _ddgs_search(query: str, num: int = 5) -> list[dict]:
    """
    `ddgs` package — multi-engine metasearch (duckduckgo/google/bing/brave/mojeek... behind the scenes).
    Ye sabse reliable key-free option hai (maintained package, HTML scraping se better).
    """
    from ddgs import DDGS

    def _run() -> list[dict]:
        with DDGS(timeout=15) as d:
            rows = d.text(query, max_results=max(num, 8))
        return [
            {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
            for r in (rows or [])
            if r.get("href")
        ]

    return await asyncio.to_thread(_run)


async def _searx_search(query: str, num: int = 5) -> list[dict]:
    """Self-hosted / public SearXNG instance (JARVIS_SEARX_URL) — privacy + no key."""
    if not SEARX_URL:
        return []
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": UA}) as client:
        r = await client.get(f"{SEARX_URL.rstrip('/')}/search",
                             params={"q": query, "format": "json", "safesearch": 1})
        r.raise_for_status()
        rows = r.json().get("results", [])
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
            for x in rows[:num] if x.get("url")]


async def _brave_search(query: str, num: int = 5) -> list[dict]:
    """Brave Search API (free tier) — sirf tab jab BRAVE_API_KEY ho."""
    if not BRAVE_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max(num, 5)},
            headers={"X-Subscription-Token": BRAVE_API_KEY, "Accept": "application/json"},
        )
        r.raise_for_status()
        rows = (r.json().get("web", {}) or {}).get("results", [])
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("description", "")}
            for x in rows[:num]]


async def _keyless_search(query: str, num: int = 5) -> list[dict]:
    """
    Key-free search — strategy chain (jo pehle kaam kare wahi jeete):
      1. ddgs (multi-engine metasearch — best)
      2. SearXNG (agar JARVIS_SEARX_URL set ho)
      3. Brave (agar BRAVE_API_KEY ho — free tier)
      4. Bing HTML scrape (b_algo blocks + redirect decode)
      5. DuckDuckGo HTML/lite endpoint
      6. Wikipedia search API (factual queries)
    """
    strategies = [_ddgs_search, _searx_search, _brave_search, _bing_search, _ddg_html_search, _wikipedia_search]
    for strategy in strategies:
        try:
            results = await strategy(query, num)
            if results:
                return results[:num]
        except Exception:
            continue
    return [{"title": "(no results)", "url": "",
             "snippet": "Key-free search se kuch nahi mila — GOOGLE_SEARCH_API_KEY daalo (ya browser_task use karo)."}]


def _clean_html(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).strip()


async def _bing_search(query: str, num: int = 5) -> list[dict]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"}) as client:
        r = await client.get(
            "https://www.bing.com/search",
            params={"q": query, "count": max(num, 10), "cc": env("JARVIS_SEARCH_CC", "IN") or "IN", "setlang": "en"},
        )
        r.raise_for_status()
        page = r.text
    blocks = re.split(r'<li class="b_algo"', page)[1:]
    results: list[dict] = []
    for b in blocks:
        link = ""
        title = ""
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if m:
            link, title = m.group(1), _clean_html(m.group(2))
        else:
            m2 = re.search(r'href="(https?://(?!r\.bing|www\.bing|go\.microsoft)[^"]+)"', b)
            m3 = re.search(r"<h2[^>]*>(.*?)</h2>", b, re.S)
            if m2:
                link = m2.group(1)
            if m3:
                title = _clean_html(m3.group(1))
        snippet = ""
        ms = re.search(r'<p[^>]*>(.*?)</p>', b, re.S)
        if ms:
            snippet = _clean_html(ms.group(1))[:300]
        if link and link.startswith("http"):
            results.append({"title": title or link[:60], "url": html.unescape(link), "snippet": snippet})
        if len(results) >= num:
            break
    return await _resolve_redirects(results)


def _decode_bing_redirect(url: str) -> str:
    """Bing ke `ck/a?...&u=a1<base64>` redirect ko asli URL me decode karo (no network)."""
    import base64
    from urllib.parse import parse_qs, urlparse

    try:
        if "bing.com/ck/a" not in url:
            return url
        q = parse_qs(urlparse(url).query)
        target = (q.get("u") or [""])[0]
        if target.startswith("a1"):
            target = target[2:]
        if not target:
            return url
        padded = target + "=" * (-len(target) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8", "ignore")
        return decoded if decoded.startswith("http") else url
    except Exception:
        return url


async def _resolve_redirects(results: list[dict]) -> list[dict]:
    return [dict(r, url=_decode_bing_redirect(r.get("url", ""))) for r in results]


async def _ddg_html_search(query: str, num: int = 5) -> list[dict]:
    """DuckDuckGo HTML/lite endpoint (anti-bot hone par khali list dega — chain aage badh jaayegi)."""
    from urllib.parse import parse_qs, unquote, urlparse

    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": UA}) as client:
        r = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
        if r.status_code >= 400:
            r = await client.post("https://lite.duckduckgo.com/lite/", data={"q": query})
        page = r.text
    results: list[dict] = []
    for m in re.finditer(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="(?P<u>[^"]+)"[^>]*>(?P<t>.*?)</a>', page, re.S):
        link = html.unescape(m.group("u"))
        if "uddg=" in link:
            link = unquote(parse_qs(urlparse(link).query).get("uddg", [link])[0])
        results.append({"title": _clean_html(m.group("t")), "url": link, "snippet": ""})
        if len(results) >= num:
            break
    if not results:
        for m in re.finditer(r'<a[^>]+href="(?P<u>https?://[^"]+)"[^>]*class="result-link"[^>]*>(?P<t>.*?)</a>', page, re.S):
            results.append({"title": _clean_html(m.group("t")), "url": html.unescape(m.group("u")), "snippet": ""})
    return results


async def _wikipedia_search(query: str, num: int = 5) -> list[dict]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": UA}) as client:
        r = await client.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": num},
        )
        r.raise_for_status()
        hits = r.json().get("query", {}).get("search", [])
    return [
        {"title": h.get("title", ""), "url": "https://en.wikipedia.org/wiki/" + h.get("title", "").replace(" ", "_"),
         "snippet": _clean_html(h.get("snippet", ""))[:300]}
        for h in hits
    ]


async def web_search_results(query: str, num: int = 5) -> list[dict]:
    try:
        res = await asyncio.to_thread(_google_cse_sync, query, num)
        if res:
            return res
    except Exception:
        pass
    try:
        return await _keyless_search(query, num)
    except Exception as exc:
        return [{"title": "search failed", "url": "", "snippet": str(exc)[:200]}]


# ---------------------------------------------------------------------------
# weather
# ---------------------------------------------------------------------------
def _weather_sync(city: str = "") -> str:
    """Blocking weather (agent.py lifetime me use hota hai). OpenWeather → wttr.in fallback."""
    city = (city or CITY).strip() or CITY
    key = env("OPENWEATHER_API_KEY")
    if key:
        try:
            import requests
            r = requests.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={"q": city, "appid": key, "units": "metric"},
                timeout=12,
            )
            if r.status_code == 200:
                d = r.json()
                return (
                    f"{d['weather'][0]['description'].title()}, {d['main']['temp']}°C "
                    f"(feels {d['main']['feels_like']}°C), humidity {d['main']['humidity']}%, "
                    f"wind {d['wind']['speed']} m/s — {city}"
                )
        except Exception:
            pass
    # key-free
    try:
        import requests
        r = requests.get(f"https://wttr.in/{city}", params={"format": "j1"}, timeout=12)
        if r.status_code == 200:
            cur = r.json()["current_condition"][0]
            return (
                f"{cur['weatherDesc'][0]['value']}, {cur['temp_C']}°C (feels {cur['FeelsLikeC']}°C), "
                f"humidity {cur['humidity']}%, wind {cur['windspeedKmph']} km/h — {city}"
            )
    except Exception as exc:
        return f"weather unavailable ({exc})"
    return "weather unavailable"


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------
@tool
async def google_search(query: str, num: int = 5) -> str:
    """Search the web and return top results (title + url + snippet).

    Use: latest news, facts, prices, "kya hai", "kitna hai", research starting point.
    """
    results = await web_search_results(query, num=num)
    if not results:
        return "❌ Koi result nahi mila."
    lines = []
    for i, r in enumerate(results, 1):
        snippet = f"\n   {r['snippet'][:220]}" if r.get("snippet") else ""
        lines.append(f"{i}. {r['title']}\n   {r['url']}{snippet}")
    return "🔎 Top results:\n" + "\n".join(lines)


@tool
async def fetch_page(url: str) -> str:
    """Fetch a web page and return its readable text (fast, no browser).

    Public/static pages ke liye ye use karo — browser-use sirf tab jab JS/login chahiye.
    """
    try:
        text = await fetch_url_text(url, max_chars=10000)
        return f"📄 {url}\n---\n{text}" if text else f"⚠️ {url} se koi text nahi mila (JS-heavy ho sakta hai → browser_use use karo)."
    except Exception as exc:
        return f"❌ fetch_page fail ({url}): {exc} — agar page JS-heavy hai to `browser_task` use karo."


@tool
async def get_current_datetime() -> str:
    """Current date and time in the configured timezone (IST by default)."""
    now = datetime.now(IST)
    zone_label = "IST" if TIMEZONE == "Asia/Kolkata" else (now.tzname() or TIMEZONE)
    return now.strftime(f"%A, %d %B %Y — %I:%M %p ({zone_label})")


@tool
async def get_weather(city: str = "") -> str:
    """Current weather for a city (khali chhodo to auto-detect from IP)."""
    if not city:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                data = (await client.get("https://ipinfo.io/json")).json()
                city = data.get("city") or CITY
        except Exception:
            city = CITY
    return await asyncio.to_thread(_weather_sync, city)


@tool
async def deep_research(query: str, sites: list[str] | None = None, max_pages: int = 6) -> str:
    """Parallel web research: search (ya diye gaye sites) → saare pages ek saath fetch → combined text.

    Use jab owner bole "alag alag websites se ye info laao" — ye fast path hai
    (browser-use se pehle ise try karo, kyunki ye seconds me ho jata hai).
    sites: optional list of URLs/domains.
    """
    targets: list[str] = []
    if sites:
        targets = [s if s.startswith("http") else f"https://{s}" for s in sites][:max_pages]
    else:
        results = await web_search_results(query, num=max_pages)
        targets = [r["url"] for r in results if r.get("url")][:max_pages]

    if not targets:
        return "❌ Koi target URL nahi mila."

    async def _one(u: str) -> tuple[str, str]:
        try:
            return u, await fetch_url_text(u, max_chars=6000)
        except Exception as exc:
            return u, f"[FAILED: {exc}]"

    pairs = await asyncio.gather(*(_one(u) for u in targets))
    blocks = []
    for url, text in pairs:
        blocks.append(f"### SOURCE: {url}\n{text[:4000]}\n")
    return f"🌐 {len(pairs)} sources fetched (parallel) for: {query}\n\n" + "\n".join(blocks)


WEB_TOOLS = [google_search, fetch_page, get_current_datetime, get_weather, deep_research]
