"""
Runtime settings — bina code edit kiye behaviour badalna
========================================================

`workspace/settings.json` me persist hoti hain. CLI (`/settings owner Nikhil`) aur
Telegram (`/settings owner Nikhil`) dono se badli ja sakti hain, aur agli baar bhi yaad rehti hain.

Sirf whitelisted keys badli ja sakti hain — warna typo se poora config ud jayega.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from .settings import RUNTIME_SETTINGS_FILE

_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {}
_LOADED = False

# key → (type, description, allowed values)
SCHEMA: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "owner_name": ("str", "Owner ka naam (Jarvis isse address karega)", ()),
    "owner_id": ("str", "Owner ID (Telegram authorization ke liye)", ()),
    "assistant_name": ("str", "Assistant ka naam", ()),
    "personality": ("str", "Personality tags: smart,friendly,concise,agentic,savage", ()),
    "language": ("str", "Bhasha preference", ("hinglish", "hindi", "english", "auto")),
    "response_style": ("str", "Jawab ka style", ("concise", "detailed", "bullet")),
    "provider": ("str", "Brain provider", ("gemini", "openrouter", "groq", "openai", "anthropic", "xai", "ollama", "auto")),
    "model": ("str", "Default (brain) model", ()),
    "temperature": ("float", "Creativity (0.0 - 1.0)", ()),
    "max_iterations": ("int", "Graph ke max refine loops", ()),
    "mode": ("str", "Default interface mode", ("text", "voice", "telegram")),
    "voice_enabled": ("bool", "Voice mode on/off", ()),
    "voice_name": ("str", "LiveKit voice ka naam", ()),
    "voice_model": ("str", "Realtime (voice) model", ()),
    "browser_enabled": ("bool", "browser-use tools on/off", ()),
    "shell_enabled": ("bool", "Shell execution on/off", ()),
    "desktop_enabled": ("bool", "Desktop automation on/off", ()),
    "file_tools_enabled": ("bool", "File tools on/off", ()),
    "research_enabled": ("bool", "Web research on/off", ()),
    "memory_enabled": ("bool", "Memory on/off", ()),
    "automation_enabled": ("bool", "Scheduled tasks on/off", ()),
    "skills_enabled": ("bool", "SKILL.md instruction packs on/off", ()),
    "show_reasoning": ("bool", "Plan/Decision/Action summaries dikhao", ()),
    "exec_approval": ("str", "Shell approval mode", ("off", "risky", "all")),
    "log_level": ("str", "Logging level", ("DEBUG", "INFO", "WARNING", "ERROR")),
    "telegram_progress": ("bool", "Telegram par live thinking/tool progress dikhao", ()),
    "cli_progress": ("bool", "CLI par live thinking/tool progress dikhao", ()),
    "cli_theme": ("str", "CLI ka look", ("pro", "plain", "dark")),
}

DEFAULTS: dict[str, Any] = {
    "owner_name": None, "owner_id": None, "assistant_name": None, "personality": None,
    "language": None, "response_style": None, "provider": None, "model": None,
    "temperature": None, "max_iterations": None, "mode": "text", "voice_enabled": None,
    "voice_name": None, "voice_model": None, "browser_enabled": None, "shell_enabled": None,
    "desktop_enabled": None, "file_tools_enabled": None, "research_enabled": None,
    "memory_enabled": None, "automation_enabled": None, "skills_enabled": None,
    "show_reasoning": None, "exec_approval": None, "log_level": None,
    "telegram_progress": "on", "cli_progress": "on", "cli_theme": "pro",
}


def _coerce(key: str, value: Any) -> Any:
    kind = SCHEMA.get(key, ("str", "", ()))[0]
    if value is None:
        return None
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "haan", "ha"}
    if kind == "int":
        return int(float(value))
    if kind == "float":
        return float(value)
    return str(value).strip()


def load(force: bool = False) -> dict[str, Any]:
    global _LOADED
    with _LOCK:
        if _LOADED and not force:
            return dict(_CACHE)
        data = dict(DEFAULTS)
        try:
            if RUNTIME_SETTINGS_FILE.exists():
                raw = json.loads(RUNTIME_SETTINGS_FILE.read_text(encoding="utf-8")) or {}
                for k, v in raw.items():
                    if k in SCHEMA:
                        try:
                            data[k] = _coerce(k, v)
                        except Exception:
                            continue
        except Exception:
            pass
        _CACHE.clear()
        _CACHE.update(data)
        _LOADED = True
        return dict(_CACHE)


def _save() -> None:
    try:
        RUNTIME_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in _CACHE.items() if v is not None}
        payload["_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        RUNTIME_SETTINGS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def get(key: str, default: Any = None) -> Any:
    data = load()
    val = data.get(key, default)
    return default if val is None else val


def set_many(**pairs: Any) -> tuple[dict[str, Any], list[str]]:
    """Settings set karo → (applied, errors). Sirf SCHEMA ke keys allowed."""
    load()
    applied: dict[str, Any] = {}
    errors: list[str] = []
    with _LOCK:
        for key, raw in pairs.items():
            key = key.strip().lower()
            if key not in SCHEMA:
                errors.append(f"'{key}' setting maujood nahi (allowed: {', '.join(sorted(SCHEMA))})")
                continue
            allowed = SCHEMA[key][2]
            try:
                val = _coerce(key, raw)
            except Exception as exc:
                errors.append(f"'{key}' ki value samajh nahi aayi: {exc}")
                continue
            if allowed and str(val).lower() not in allowed:
                errors.append(f"'{key}' ki allowed values: {', '.join(allowed)}")
                continue
            _CACHE[key] = val
            applied[key] = val
        if applied:
            _save()
    return applied, errors


def reset(key: str | None = None) -> None:
    load()
    with _LOCK:
        if key:
            _CACHE[key] = DEFAULTS.get(key)
        else:
            _CACHE.update(DEFAULTS)
        _save()


def snapshot() -> dict[str, Any]:
    """Effective settings (runtime + env defaults) — /status aur doctor ke liye."""
    from . import settings as S

    data = load()
    effective = {
        "assistant_name": data.get("assistant_name") or S.ASSISTANT_NAME,
        "owner_name": data.get("owner_name") or S.OWNER_NAME,
        "owner_id": data.get("owner_id") or S.OWNER_ID,
        "personality": data.get("personality") or S.ASSISTANT_PERSONALITY,
        "language": data.get("language") or S.LANGUAGE,
        "response_style": data.get("response_style") or S.RESPONSE_STYLE,
        "provider": data.get("provider") or S.PROVIDER,
        "model": data.get("model") or S.MODEL_ROUTES["brain"].model,
        "temperature": data.get("temperature") if data.get("temperature") is not None else S.MODEL_ROUTES["brain"].temperature,
        "max_iterations": data.get("max_iterations") or S.MAX_GRAPH_ITERATIONS,
        "mode": data.get("mode") or S.DEFAULT_MODE,
        "voice_enabled": data.get("voice_enabled") if data.get("voice_enabled") is not None else S.VOICE_ENABLED,
        "voice_name": data.get("voice_name") or S.VOICE_NAME,
        "voice_model": data.get("voice_model") or S.REALTIME_MODEL,
        "browser_enabled": data.get("browser_enabled") if data.get("browser_enabled") is not None else S.BROWSER_ENABLED,
        "shell_enabled": data.get("shell_enabled") if data.get("shell_enabled") is not None else S.SHELL_ENABLED,
        "desktop_enabled": data.get("desktop_enabled") if data.get("desktop_enabled") is not None else S.DESKTOP_ENABLED,
        "file_tools_enabled": data.get("file_tools_enabled") if data.get("file_tools_enabled") is not None else S.FILE_TOOLS_ENABLED,
        "research_enabled": data.get("research_enabled") if data.get("research_enabled") is not None else S.RESEARCH_ENABLED,
        "memory_enabled": data.get("memory_enabled") if data.get("memory_enabled") is not None else S.MEMORY_ENABLED,
        "automation_enabled": data.get("automation_enabled") if data.get("automation_enabled") is not None else S.AUTOMATION_ENABLED,
        "skills_enabled": data.get("skills_enabled") if data.get("skills_enabled") is not None else True,
        "show_reasoning": data.get("show_reasoning") if data.get("show_reasoning") is not None else S.SHOW_REASONING,
        "exec_approval": data.get("exec_approval") or S.EXEC_APPROVAL,
        "log_level": data.get("log_level") or S.LOG_LEVEL,
        "workspace": str(S.WORKSPACE_DIR),
        "telegram_progress": data.get("telegram_progress", "on"),
        "cli_progress": data.get("cli_progress", "on"),
        "cli_theme": data.get("cli_theme", "pro"),
    }
    return effective


def describe() -> list[tuple[str, str, str]]:
    """(key, current value, description) — /settings ke liye."""
    snap = snapshot()
    rows: list[tuple[str, str, str]] = []
    for key in sorted(SCHEMA):
        rows.append((key, str(snap.get(key, "")), SCHEMA[key][1]))
    return rows
