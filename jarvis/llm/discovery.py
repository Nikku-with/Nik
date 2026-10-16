"""
Model discovery — free/available models ki LIVE list
===================================================

Kyun: model IDs har 2-3 mahine me badalte hain (kuch shutdown, kuch paywall ke peeche). Code me
purana model hardcode karke chhodna = "404 model not found" hamesha ke liye.

Ye module provider ke `/models` endpoint se live list leti hai, 12 ghante cache karti hai
(`workspace/model_cache.json`), aur free models ko prefer karti hai. Agar network na ho to
configured defaults hi chalti hain — kuch crash nahi hota.
"""
from __future__ import annotations

import json
import time
from typing import Any

from ..config import settings as S
from . import providers as P

_MEM: dict[str, dict[str, Any]] = {}


def _load_cache() -> dict[str, Any]:
    try:
        if S.MODEL_CACHE_FILE.exists():
            return json.loads(S.MODEL_CACHE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_cache(data: dict[str, Any]) -> None:
    try:
        S.MODEL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        S.MODEL_CACHE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _fresh(entry: dict[str, Any] | None) -> bool:
    if not entry:
        return False
    age_h = (time.time() - float(entry.get("ts") or 0)) / 3600.0
    return age_h < S.MODEL_DISCOVERY_TTL_H


def _http_get(url: str, headers: dict[str, str]) -> dict[str, Any] | None:
    try:
        import httpx

        with httpx.Client(timeout=12.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:
        return None


def _parse(payload: dict[str, Any], provider: str) -> list[dict[str, Any]]:
    """Har provider ka models JSON thoda alag hota hai — normalize karo."""
    out: list[dict[str, Any]] = []
    if provider == "gemini":
        for m in payload.get("models", []) or []:
            name = str(m.get("name") or "").replace("models/", "")
            if not name:
                continue
            methods = m.get("supportedGenerationMethods") or m.get("supported_actions") or []
            out.append({
                "id": name,
                "free": ":free" in name or "flash" in name.lower(),
                "tools": any("generateContent" in str(x) for x in methods) or not methods,
                "vision": "vision" in json.dumps(m).lower() or "flash" in name.lower(),
            })
        return out

    for m in payload.get("data", []) or []:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        pricing = m.get("pricing") or {}
        try:
            prompt_cost = float(pricing.get("prompt") or 0)
            completion_cost = float(pricing.get("completion") or 0)
            is_free = mid.endswith(":free") or (prompt_cost == 0 and completion_cost == 0)
        except Exception:
            is_free = mid.endswith(":free")
        supported_parameters = m.get("supported_parameters")
        tool_support = (
            any(str(param).lower() in {"tools", "tool_choice"} for param in supported_parameters)
            if isinstance(supported_parameters, list) else None
        )
        out.append({
            "id": mid,
            "free": is_free,
            "tools": tool_support,
            "vision": "vl" in mid.lower() or "vision" in mid.lower(),
            "context": m.get("context_length") or m.get("context_window") or 0,
        })
    return out


def list_models(provider: str, force: bool = False) -> list[dict[str, Any]]:
    """Provider ke available models (cached). Network fail ho to [] deta hai."""
    name = P.canonical(provider)
    spec = P.PROVIDERS.get(name)
    if not spec or not spec.models_endpoint:
        return []
    if not S.MODEL_DISCOVERY:
        return []
    if not force and name in _MEM and _fresh(_MEM[name]):
        return list(_MEM[name]["models"])
    cache = _load_cache()
    if not force and _fresh(cache.get(name)):
        _MEM[name] = cache[name]
        return list(cache[name]["models"])

    key = P.api_key(name)
    headers: dict[str, str] = {}
    if key:
        if spec.kind == "google":
            headers["x-goog-api-key"] = key
        elif spec.kind == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {key}"
    url = spec.models_endpoint
    if spec.kind == "google":
        url = f"{url}?key={key}" if key else url
        headers.pop("x-goog-api-key", None)

    payload = _http_get(url, headers)
    if not payload:
        return []
    models = _parse(payload, name)
    entry = {"ts": time.time(), "models": models}
    _MEM[name] = entry
    cache[name] = entry
    _save_cache(cache)
    return models


def free_models(provider: str, prefer_big: bool = True) -> list[str]:
    """Free models ki list — bade/behtar pehle (brain ke liye), chhote baad me (fast ke liye)."""
    models = [m for m in list_models(provider) if m.get("free")]
    if not models:
        return []

    def rank(m: dict[str, Any]) -> tuple:
        mid = m["id"].lower()
        size = 0
        for token, val in (("405b", 405), ("235b", 235), ("120b", 120), ("70b", 70),
                           ("32b", 32), ("27b", 27), ("12b", 12), ("8b", 8), ("7b", 7), ("3b", 3)):
            if token in mid:
                size = val
                break
        big_pref = size if prefer_big else -size
        return (-big_pref, 0 if ":free" in mid else 1, mid)

    models.sort(key=rank)
    return [m["id"] for m in models]


def best_free_model(provider: str, role: str = "brain", exclude: tuple[str, ...] = ()) -> str | None:
    """Role ke hisaab se sabse fit free model — aur agar mila to cache me note kar do."""
    want_vision = role == "vision"
    want_code = role == "code"
    models = list_models(provider)
    if not models:
        return None
    pool = [m for m in models if m.get("free")]
    if want_vision:
        vis = [m for m in pool if m.get("vision")]
        pool = vis or pool
    if not pool:
        return None

    def score(m: dict[str, Any]) -> tuple:
        mid = m["id"].lower()
        s = 0
        if want_code and ("coder" in mid or "codestral" in mid or "qwen" in mid):
            s += 3
        if want_vision and m.get("vision"):
            s += 3
        if "instruct" in mid or "chat" in mid:
            s += 1
        if any(bad in mid for bad in ("guard", "embed", "whisper", "tts", "rerank", "moderation")):
            s -= 5
        if "deepseek" in mid or "qwen3" in mid or "llama-3.3" in mid or "kimi" in mid:
            s += 2
        size = 0
        for token, val in (("405b", 405), ("235b", 235), ("120b", 120), ("70b", 70),
                           ("32b", 32), ("27b", 27), ("12b", 12), ("8b", 8), ("7b", 7), ("3b", 3)):
            if token in mid:
                size = val
                break
        return (-s, -size, mid)

    pool.sort(key=score)
    for m in pool:
        if m["id"] not in exclude:
            return m["id"]
    return None


def summary() -> str:
    """Doctor/status ke liye chhota summary."""
    lines: list[str] = []
    for name in P.PROVIDERS:
        if not P.is_available(name):
            continue
        models = list_models(name)
        free = [m["id"] for m in models if m.get("free")]
        if not models:
            lines.append(f"{name}: {len(P.PROVIDERS)} provider key set, live model list nahi mili (network/keys)")
        else:
            lines.append(f"{name}: {len(models)} models ({len(free)} free) — best free: "
                         f"{best_free_model(name) or 'n/a'}")
    return "\n".join(lines) or "koi provider key set nahi (offline/demo mode)"
