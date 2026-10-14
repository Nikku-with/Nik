"""
NikhilAI — centralised settings
===============================

Ek hi jagah se poora configuration. Baaki code me `os.getenv()` bikhra hua nahi milega —
sab kuch yahan se import hota hai, aur runtime me `jarvis.config.runtime` se badla ja sakta hai.

Naming convention
-----------------
Naye (preferred) naam:
    NIKHILAI_NAME, OWNER_NAME, OWNER_ID, ASSISTANT_PERSONALITY, LANGUAGE,
    BRAIN_PROVIDER, BRAIN_MODEL, GEMINI_API_KEY, OPENROUTER_API_KEY, GROQ_API_KEY,
    TELEGRAM_BOT_TOKEN, LIVEKIT_URL, WORKSPACE_DIR, LOG_LEVEL ...
Purane `JARVIS_*` naam bhi chalta hai (backward compatible) — naye naam ko priority milti hai.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# .env loading (no hard dependency — python-dotenv ho to use karo)
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent.parent


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """`KEY=value  # comment` → ("KEY", "value"). Quoted values me comment nahi kaatta."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, _, val = line.partition("=")
    key, val = key.strip(), val.strip()
    if not key:
        return None
    if val[:1] in ("'", '"'):                     # quoted → poora value, comments nahi
        quote = val[0]
        end = val.find(quote, 1)
        val = val[1:end] if end != -1 else val[1:]
    else:
        # unquoted → `#` jo whitespace ke baad (ya line ke shuru me) aaye, wahan se comment shuru
        stripped = val.lstrip()
        if stripped.startswith("#"):
            val = ""
        else:
            m = re.search(r"\s#", val)
            if m:
                val = val[: m.start()]
        val = val.strip()
    return key, val


def _load_env_file() -> None:
    """`.env` padho — apna parser (inline comments safe) + dotenv fallback."""
    for candidate in (ROOT_DIR / ".env", Path.cwd() / ".env"):
        if not candidate.exists():
            continue
        try:
            for raw in candidate.read_text(encoding="utf-8", errors="ignore").splitlines():
                parsed = _parse_env_line(raw)
                if not parsed:
                    continue
                key, val = parsed
                if key not in os.environ or os.environ[key].strip() == "":
                    os.environ[key] = val
        except Exception:
            pass
        try:  # dotenv ho to uske extra features (multiline etc.) bhi le lo
            from dotenv import load_dotenv  # type: ignore

            load_dotenv(candidate, override=False)
        except Exception:
            pass
        return


_load_env_file()


# ---------------------------------------------------------------------------
# env helpers  (dono naam support karte hain: naya pehle, phir purana)
# ---------------------------------------------------------------------------
def _first(*names: str) -> str | None:
    for name in names:
        if not name:
            continue
        val = os.environ.get(name)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return None


def env(key: str, default: str | None = None) -> str | None:
    """Raw env read — `KEY` / `JARVIS_KEY` dono try karta hai."""
    alt = f"JARVIS_{key}" if not key.startswith("JARVIS_") else key
    return _first(key, alt, default)


def env_bool(key: str, default: bool = False) -> bool:
    val = env(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on", "haan", "ha"}


def env_int(key: str, default: int) -> int:
    val = env(key)
    try:
        return int(str(val).strip()) if val is not None else default
    except Exception:
        return default


def env_float(key: str, default: float) -> float:
    val = env(key)
    try:
        return float(str(val).strip()) if val is not None else default
    except Exception:
        return default


def env_list(key: str, default: str = "") -> list[str]:
    val = env(key, default) or ""
    return [x.strip() for x in val.replace(";", ",").split(",") if x.strip()]


# ---------------------------------------------------------------------------
# IDENTITY
# ---------------------------------------------------------------------------
ASSISTANT_NAME = _first("NIKHILAI_NAME", "JARVIS_ASSISTANT_NAME") or "NikhilAI"
OWNER_NAME = _first("OWNER_NAME", "JARVIS_OWNER_NAME") or "Nikhil"
OWNER_ID = _first("OWNER_ID", "JARVIS_OWNER_ID") or ""
OWNER_NAME_HINDI = _first("OWNER_NAME_HINDI", "JARVIS_OWNER_NAME_HINDI") or OWNER_NAME
OWNER_FIRST_NAME = OWNER_NAME.split()[0] if OWNER_NAME else "Owner"
ORG_NAME = _first("ORG_NAME", "JARVIS_ORG") or ASSISTANT_NAME
PROJECT_NAME = "NikhilAI"
ASSISTANT_PERSONALITY = _first("ASSISTANT_PERSONALITY", "JARVIS_PERSONALITY") or "smart,friendly,concise,agentic"
LANGUAGE = _first("LANGUAGE", "JARVIS_LANGUAGE") or "hinglish"
RESPONSE_STYLE = _first("RESPONSE_STYLE", "JARVIS_RESPONSE_STYLE") or "concise"
DEFAULT_MODE = (_first("DEFAULT_MODE", "JARVIS_DEFAULT_MODE") or "text").lower()


# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------
WORKSPACE_DIR = Path(_first("WORKSPACE_DIR", "JARVIS_WORKSPACE") or str(ROOT_DIR / "workspace")).expanduser()
SCRIPTS_DIR = WORKSPACE_DIR / "scripts"
NOTES_DIR = Path(_first("NOTES_DIR", "JARVIS_NOTES_DIR") or str(WORKSPACE_DIR / "notes")).expanduser()
ARTIFACTS_DIR = WORKSPACE_DIR / "artifacts"
SCREENSHOTS_DIR = WORKSPACE_DIR / "screenshots"
LOGS_DIR = WORKSPACE_DIR / "logs"
MEMORY_DB = Path(_first("MEMORY_DB", "JARVIS_MEMORY_DB") or str(WORKSPACE_DIR / "nikhilai_memory.sqlite3")).expanduser()
OPTIMIZED_DIR = ROOT_DIR / "optimized"
SESSION_STATE_FILE = Path(_first("SESSION_STATE_FILE", "JARVIS_SESSION_STATE") or str(WORKSPACE_DIR / "nikhilai_memory.json")).expanduser()
RUNTIME_SETTINGS_FILE = Path(_first("RUNTIME_SETTINGS", "JARVIS_RUNTIME_SETTINGS") or str(WORKSPACE_DIR / "settings.json")).expanduser()
AUTOMATION_FILE = Path(_first("AUTOMATION_FILE", "JARVIS_AUTOMATIONS") or str(WORKSPACE_DIR / "automations.json")).expanduser()
MODEL_CACHE_FILE = WORKSPACE_DIR / "model_cache.json"

def _default_search_roots() -> list[str]:
    """File dhoondhne ki default jagah: ghar + apna workspace + current dir.

    Sirf home rakhne se ek bug tha — agar workspace home ke bahar ho (dusra drive,
    /tmp, shared folder) to Jarvis apni khud ki saved file dhoondh nahi paata tha.
    """
    roots = [str(Path.home()), str(WORKSPACE_DIR)]
    try:
        cwd = str(Path.cwd())
    except Exception:
        cwd = ""
    if cwd and not any(cwd == r or cwd.startswith(r.rstrip("/\\") + "/") for r in roots):
        roots.append(cwd)
    seen: list[str] = []
    for r in roots:
        if r and r not in seen:
            seen.append(r)
    return seen


FILE_SEARCH_ROOTS = env_list("FILE_SEARCH_ROOTS", "JARVIS_FILE_SEARCH_ROOTS") or _default_search_roots()

for _d in (WORKSPACE_DIR, SCRIPTS_DIR, NOTES_DIR, ARTIFACTS_DIR, SCREENSHOTS_DIR, LOGS_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# PROVIDERS  (multi-brain: Gemini / OpenRouter / Groq / extras)
# ---------------------------------------------------------------------------
GEMINI_API_KEY = _first("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY") or ""
OPENROUTER_API_KEY = _first("OPENROUTER_API_KEY") or ""
OPENROUTER_BASE_URL = _first("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
GROQ_API_KEY = _first("GROQ_API_KEY") or ""
GROQ_BASE_URL = _first("GROQ_BASE_URL") or "https://api.groq.com/openai/v1"
OPENAI_API_KEY = _first("OPENAI_API_KEY") or ""
OPENAI_BASE_URL = _first("OPENAI_BASE_URL") or "https://api.openai.com/v1"
ANTHROPIC_API_KEY = _first("ANTHROPIC_API_KEY") or ""
XAI_API_KEY = _first("XAI_API_KEY") or ""
OLLAMA_HOST = _first("OLLAMA_HOST") or ""

# Provider → kis env var me key rehti hai (live check ke liye — snapshot nahi)
PROVIDER_KEY_ENV: dict[str, tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "ollama": ("OLLAMA_HOST",),
}
# import-time snapshot (sirf display/back-compat ke liye — availability check live hoti hai)
PROVIDER_KEYS: dict[str, str] = {name: (_first(*envs) or "") for name, envs in PROVIDER_KEY_ENV.items()}
PROVIDER_ORDER = ("gemini", "openrouter", "groq", "openai", "anthropic", "xai", "ollama")


def _provider_available(provider: str) -> bool:
    """LIVE check — user ne abhi key export ki ho to turant pakde (snapshot nahi)."""
    envs = PROVIDER_KEY_ENV.get(provider, ())
    return bool(_first(*envs))


def autodetect_provider() -> str:
    forced = (_first("BRAIN_PROVIDER", "JARVIS_PROVIDER") or "").lower()
    if forced:
        return forced if forced != "auto" else _auto_provider()
    return _auto_provider()


def _auto_provider() -> str:
    for provider in PROVIDER_ORDER:
        if _provider_available(provider):
            return provider
    return "demo"


PROVIDER = autodetect_provider()


# ---------------------------------------------------------------------------
# MODEL ROUTES  (role based; provider-specific defaults + fallback chains)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelRoute:
    provider: str
    model: str
    fallbacks: tuple[str, ...] = ()
    temperature: float = 0.2
    max_tokens: int | None = None
    reasoning_effort: str | None = None

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "fallbacks": list(self.fallbacks),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
        }


MODEL_ROLES = ("brain", "fast", "planner", "code", "research", "extract", "vision", "browser", "critic")

# Role → model defaults per provider (free/low-cost tier first).
MODEL_DEFAULTS: dict[str, dict[str, list[str]]] = {
    "gemini": {
        "brain": ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-2.5-flash"],
        "fast": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"],
        "planner": ["gemini-3.7-flash", "gemini-3.8-flash"],
        "code": ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.1-pro-preview"],
        "research": ["gemini-3.7-flash", "gemini-3.6-flash"],
        "extract": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
        "vision": ["gemini-3.7-flash", "gemini-3.8-flash"],
        "browser": ["gemini-3.7-flash", "gemini-3.6-flash"],
        "critic": ["gemini-3.7-flash", "gemini-3.8-flash"],
    },
    "openrouter": {  # `:free` models — discovery inhe live verify karta hai
        "brain": ["deepseek/deepseek-chat-v3.1:free", "qwen/qwen3-235b-a22b:free",
                  "meta-llama/llama-3.3-70b-instruct:free"],
        "fast": ["meta-llama/llama-3.1-8b-instruct:free", "qwen/qwen-2.5-7b-instruct:free"],
        "planner": ["deepseek/deepseek-chat-v3.1:free", "qwen/qwen3-235b-a22b:free"],
        "code": ["qwen/qwen3-coder:free", "deepseek/deepseek-chat-v3.1:free"],
        "research": ["meta-llama/llama-3.3-70b-instruct:free", "qwen/qwen3-235b-a22b:free"],
        "extract": ["meta-llama/llama-3.1-8b-instruct:free", "qwen/qwen-2.5-7b-instruct:free"],
        "vision": ["qwen/qwen2.5-vl-32b-instruct:free", "meta-llama/llama-3.2-11b-vision-instruct:free"],
        "browser": ["deepseek/deepseek-chat-v3.1:free", "qwen/qwen3-235b-a22b:free"],
        "critic": ["deepseek/deepseek-chat-v3.1:free", "meta-llama/llama-3.3-70b-instruct:free"],
    },
    "groq": {  # fast + free tier
        "brain": ["llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct", "llama-3.1-8b-instant"],
        "fast": ["llama-3.1-8b-instant", "gemma2-9b-it"],
        "planner": ["llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct"],
        "code": ["qwen/qwen3-32b", "llama-3.3-70b-versatile"],
        "research": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
        "extract": ["llama-3.1-8b-instant", "gemma2-9b-it"],
        "vision": ["meta-llama/llama-4-scout-17b-16e-instruct"],
        "browser": ["llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct"],
        "critic": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    },
    "openai": {
        "brain": ["gpt-4.1-mini", "gpt-4o-mini"],
        "fast": ["gpt-4o-mini"],
        "planner": ["gpt-4.1-mini"],
        "code": ["gpt-4.1-mini"],
        "research": ["gpt-4.1-mini"],
        "extract": ["gpt-4o-mini"],
        "vision": ["gpt-4o-mini"],
        "browser": ["gpt-4.1-mini"],
        "critic": ["gpt-4.1-mini"],
    },
    "anthropic": {
        "brain": ["claude-sonnet-4-5", "claude-haiku-4-5"],
        "fast": ["claude-haiku-4-5"],
        "planner": ["claude-sonnet-4-5"],
        "code": ["claude-sonnet-4-5"],
        "research": ["claude-sonnet-4-5"],
        "extract": ["claude-haiku-4-5"],
        "vision": ["claude-sonnet-4-5"],
        "browser": ["claude-sonnet-4-5"],
        "critic": ["claude-sonnet-4-5"],
    },
    "xai": {
        "brain": ["grok-4", "grok-3-mini"],
        "fast": ["grok-3-mini"],
        "planner": ["grok-4"],
        "code": ["grok-4"],
        "research": ["grok-4"],
        "extract": ["grok-3-mini"],
        "vision": ["grok-4"],
        "browser": ["grok-4"],
        "critic": ["grok-4"],
    },
    "ollama": {
        "brain": ["qwen3:8b", "llama3.2:3b"],
        "fast": ["llama3.2:3b"],
        "planner": ["qwen3:8b"],
        "code": ["qwen2.5-coder:7b", "qwen3:8b"],
        "research": ["qwen3:8b"],
        "extract": ["llama3.2:3b"],
        "vision": ["llava:7b"],
        "browser": ["qwen3:8b"],
        "critic": ["qwen3:8b"],
    },
}


def _default_chain(provider: str, role: str) -> list[str]:
    table = MODEL_DEFAULTS.get(provider) or MODEL_DEFAULTS.get("gemini") or {}
    chain = table.get(role) or table.get("brain") or ["gemini-3.8-flash"]
    return list(chain)


def model_chain_for(role: str, provider: str | None = None) -> list[str]:
    """Role ke liye model chain: NIKHILAI_<ROLE>_MODEL / BRAIN_MODEL se override, warna defaults."""
    provider = (provider or PROVIDER).lower()
    if provider == "demo":
        return []
    upper = role.upper()
    primary = _first(f"NIKHILAI_{upper}_MODEL", f"BRAIN_MODEL" if role == "brain" else f"JARVIS_{upper}_MODEL")
    fallbacks = env_list(f"NIKHILAI_{upper}_FALLBACKS", f"JARVIS_{upper}_FALLBACKS")
    chain = ([primary] if primary else []) + fallbacks
    if not chain:
        chain = _default_chain(provider, role)
    elif fallbacks or primary:
        chain += [m for m in _default_chain(provider, role) if m not in chain]
    return chain


def _route(role: str, temperature: float, max_tokens: int | None = None,
           reasoning_effort: str | None = None) -> ModelRoute:
    chain = model_chain_for(role)
    model = chain[0] if chain else ""
    return ModelRoute(PROVIDER, model, tuple(chain[1:]), temperature, max_tokens, reasoning_effort)


MODEL_ROUTES: dict[str, ModelRoute] = {
    "brain": _route("brain", env_float("TEMPERATURE", 0.3), reasoning_effort=env("REASONING_EFFORT", "medium")),
    "fast": _route("fast", 0.0),
    "planner": _route("planner", 0.2),
    "code": _route("code", 0.1),
    "research": _route("research", 0.2),
    "extract": _route("extract", 0.0),
    "vision": _route("vision", 0.1),
    "browser": _route("browser", 0.1),
    "critic": _route("critic", 0.0),
}

MODEL_FALLBACKS: dict[str, list[str]] = {role: list(r.fallbacks) for role, r in MODEL_ROUTES.items()}

# Realtime voice (LiveKit + Gemini Live / realtime)
REALTIME_MODEL = _first("REALTIME_MODEL", "JARVIS_REALTIME_MODEL") or "gemini-3.8-live"
VOICE_ENABLED = env_bool("VOICE_ENABLED", True)
VOICE_NAME = _first("VOICE_NAME", "JARVIS_VOICE_NAME") or "Charon"

# Provider discovery (live /models endpoint se free/available models)
MODEL_DISCOVERY = env_bool("MODEL_DISCOVERY", True)
MODEL_DISCOVERY_TTL_H = env_int("MODEL_DISCOVERY_TTL_H", 12)


# ---------------------------------------------------------------------------
# FEATURE FLAGS / RUNTIME
# ---------------------------------------------------------------------------
OFFLINE_MODE = env_bool("OFFLINE_MODE", PROVIDER == "demo")
USE_DSPY = env_bool("USE_DSPY", True)
DSPY_OPTIMIZED = env_bool("DSPY_OPTIMIZED", True)
USE_LANGGRAPH_CHECKPOINTER = env_bool("LANGGRAPH_CHECKPOINTER", True)
MEMORY_ENABLED = env_bool("MEMORY_ENABLED", True)
AUTOMATION_ENABLED = env_bool("AUTOMATION_ENABLED", True)
DESKTOP_ENABLED = env_bool("DESKTOP_ENABLED", True)
SHELL_ENABLED = env_bool("SHELL_ENABLED", True)
FILE_TOOLS_ENABLED = env_bool("FILE_TOOLS_ENABLED", True)
RESEARCH_ENABLED = env_bool("RESEARCH_ENABLED", True)
BROWSER_ENABLED = env_bool("BROWSER_ENABLED", True)
SHOW_REASONING = env_bool("SHOW_REASONING", True)
LOG_LEVEL = (_first("LOG_LEVEL", "JARVIS_LOG_LEVEL") or "INFO").upper()

MAX_GRAPH_ITERATIONS = env_int("MAX_ITERATIONS", env_int("JARVIS_MAX_ITERATIONS", 3))

BROWSER_MODE = (env("BROWSER_MODE", "auto") or "auto").lower()  # auto | python | cli | cloud
BROWSER_HEADLESS = env_bool("BROWSER_HEADLESS", True)
BROWSER_KEEP_ALIVE = env_bool("BROWSER_KEEP_ALIVE", True)
BROWSER_MAX_STEPS = env_int("BROWSER_MAX_STEPS", 60)
BROWSER_TIMEOUT_S = env_int("BROWSER_TIMEOUT_S", 420)
BROWSER_MAX_PARALLEL = env_int("BROWSER_MAX_PARALLEL", 3)
BROWSER_USER_DATA_DIR = _first("BROWSER_PROFILE_DIR", "JARVIS_BROWSER_PROFILE_DIR")

CODE_MAX_HEAL_ATTEMPTS = env_int("CODE_HEAL_ATTEMPTS", 3)
CODE_TIMEOUT_S = env_int("CODE_TIMEOUT_S", 90)
SHELL_TIMEOUT_S = env_int("SHELL_TIMEOUT_S", 120)

SAFETY_ALLOW_SHELL = env_bool("ALLOW_SHELL", True)
SAFETY_ALLOW_FILE_DELETE = env_bool("ALLOW_FILE_DELETE", True)
SAFETY_SANDBOX_ROOT = Path(_first("SANDBOX_ROOT", "JARVIS_SANDBOX_ROOT") or str(WORKSPACE_DIR)).expanduser()
EXEC_APPROVAL = (_first("EXEC_APPROVAL", "JARVIS_EXEC_APPROVAL") or "risky").lower()  # off | risky | all
# NOTE: purana EXEC_APPROVAL_TOKEN ("haan-karo") hata diya — model-visible approval ka
# koi rasta nahi hai. Approvals sirf human deta hai via jarvis/security/gate.py broker.
PROCESS_MAX = env_int("MAX_PROCESSES", env_int("JARVIS_MAX_PROCESSES", 12))

MAX_FILE_READ_CHARS = env_int("MAX_FILE_READ_CHARS", 20000)
FILE_SEARCH_MAX_RESULTS = env_int("FILE_SEARCH_MAX_RESULTS", 15)
FILE_CONTENT_SEARCH_MAX = env_int("CONTENT_SEARCH_MAX_FILES", 2000)

def _default_skills_dir() -> Path:
    """SKILL.md kahan se padhein — repo aur pip-install dono me same kaam kare.

    Order: packaged catalog (wheel me bhi aata hai) → repo-root `skills/` (user apni
    skill yahan daal sakta hai) → workspace/skills → sys.prefix/share/nikhilai/skills.
    """
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "skills" / "catalog",      # jarvis/skills/catalog (packaged)
        here.parent.parent.parent / "skills",           # repo root skills/ (purana + custom)
        WORKSPACE_DIR / "skills",
        Path(sys.prefix) / "share" / "nikhilai" / "skills",
    ]
    for c in candidates:
        try:
            if c.is_dir() and any(c.rglob("SKILL.md")):
                return c
        except Exception:
            continue
    return candidates[0]


SKILLS_DIR = Path(_first("SKILLS_DIR", "JARVIS_SKILLS_DIR") or str(_default_skills_dir())).expanduser()
SKILLS_MAX_INJECT = env_int("SKILLS_MAX_INJECT", 3)

# ---------------------------------------------------------------------------
# SEARCH / WEATHER
# ---------------------------------------------------------------------------
BRAVE_API_KEY = _first("BRAVE_API_KEY") or ""
SEARX_URL = _first("SEARX_URL", "JARVIS_SEARX_URL") or ""
BING_SEARCH_API_KEY = _first("BING_SEARCH_API_KEY") or ""
BING_SEARCH_ENDPOINT = _first("BING_SEARCH_ENDPOINT") or "https://api.bing.microsoft.com/v7.0/search"
GOOGLE_SEARCH_API_KEY = _first("GOOGLE_SEARCH_API_KEY") or ""
GOOGLE_SEARCH_ENGINE_ID = _first("SEARCH_ENGINE_ID", "GOOGLE_SEARCH_ENGINE_ID") or ""
DUCKDUCKGO_ENABLED = env_bool("DUCKDUCKGO_ENABLED", True)
OPENWEATHER_API_KEY = _first("OPENWEATHER_API_KEY") or ""

# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = _first("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_OWNER_ID = _first("TELEGRAM_OWNER_ID") or OWNER_ID
TELEGRAM_ALLOWED_CHAT_IDS = env_list("TELEGRAM_ALLOWED_CHAT_IDS")
TELEGRAM_ALLOWED_USERS = env_list("TELEGRAM_ALLOWED_USERS")
TELEGRAM_POLL_INTERVAL = env_float("TELEGRAM_POLL_INTERVAL", 1.0)

# ---------------------------------------------------------------------------
# LIVEKIT (voice)
# ---------------------------------------------------------------------------
LIVEKIT_URL = _first("LIVEKIT_URL") or ""
LIVEKIT_API_KEY = _first("LIVEKIT_API_KEY") or ""
LIVEKIT_API_SECRET = _first("LIVEKIT_API_SECRET") or ""

# ---------------------------------------------------------------------------
# API SERVER
# ---------------------------------------------------------------------------
API_HOST = _first("API_HOST", "JARVIS_API_HOST") or "0.0.0.0"
API_PORT = env_int("API_PORT", env_int("JARVIS_API_PORT", 8000))


# ---------------------------------------------------------------------------
# optional-capability probes
# ---------------------------------------------------------------------------
def python_executable() -> str:
    return sys.executable or "python3"


def has_browser_use() -> bool:
    try:
        import browser_use  # noqa: F401

        return True
    except Exception:
        return False


def has_browser_use_cli() -> bool:
    return bool(shutil.which("browser-use"))


def has_livekit() -> bool:
    try:
        import livekit.agents  # noqa: F401

        return True
    except Exception:
        return False


def has_telegram() -> bool:
    try:
        import telegram  # noqa: F401

        return True
    except Exception:
        return False


def has_rich() -> bool:
    try:
        import rich  # noqa: F401

        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CONFIG REPORT  (kabhi bhi secret value print nahi hoti)
# ---------------------------------------------------------------------------
@dataclass
class ConfigReport:
    ready: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_required

    def lines(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        out += [("READY", item) for item in self.ready]
        out += [("MISSING OPTIONAL", item) for item in self.missing_optional]
        out += [("MISSING REQUIRED", item) for item in self.missing_required]
        return out

    def as_text(self) -> str:
        parts = []
        for status, item in self.lines():
            icon = {"READY": "✅", "MISSING OPTIONAL": "🟡", "MISSING REQUIRED": "🔴"}[status]
            parts.append(f"{icon} {status:<17} {item}")
        return "\n".join(parts)


def config_report() -> ConfigReport:
    """Startup par dikhne wala report — READY / MISSING OPTIONAL / MISSING REQUIRED."""
    rep = ConfigReport()
    rep.ready.append(f"Identity: {ASSISTANT_NAME} (owner: {OWNER_NAME})")
    rep.ready.append(f"Workspace: {WORKSPACE_DIR}")

    if PROVIDER == "demo":
        rep.missing_required.append("Brain provider — koi API key nahi mili (Gemini/OpenRouter/Groq me se ek daalo)")
    else:
        rep.ready.append(f"Brain: {PROVIDER} / {MODEL_ROUTES['brain'].model or 'auto'}")
    rep.ready.append(f"Fallback chains: {', '.join(MODEL_FALLBACKS['brain']) or '(none)'}")

    for name, present, hint in (
        ("Gemini (GEMINI_API_KEY)", bool(GEMINI_API_KEY), "https://aistudio.google.com/apikey"),
        ("OpenRouter (OPENROUTER_API_KEY)", bool(OPENROUTER_API_KEY), "https://openrouter.ai/keys"),
        ("Groq (GROQ_API_KEY)", bool(GROQ_API_KEY), "https://console.groq.com/keys"),
    ):
        (rep.ready if present else rep.missing_optional).append(f"{name}" + ("" if present else f" — {hint}"))

    (rep.ready if TELEGRAM_BOT_TOKEN else rep.missing_optional).append(
        "Telegram bot" if TELEGRAM_BOT_TOKEN else "Telegram bot (TELEGRAM_BOT_TOKEN) — @BotFather se lo")
    voice_ok = bool(LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET)
    (rep.ready if voice_ok else rep.missing_optional).append(
        "Voice (LiveKit)" if voice_ok else "Voice (LIVEKIT_URL/API_KEY/API_SECRET) — cloud.livekit.io")

    rep.ready.append(f"Keyless search: ddgs={'on' if DUCKDUCKGO_ENABLED else 'off'}"
                     + (f", searx={SEARX_URL}" if SEARX_URL else "")
                     + (", brave=on" if BRAVE_API_KEY else "")
                     + (", bing=on" if BING_SEARCH_API_KEY else ""))
    (rep.ready if has_browser_use() or has_browser_use_cli() else rep.missing_optional).append(
        "browser-use" if (has_browser_use() or has_browser_use_cli()) else "browser-use (pip install browser-use)")
    if OFFLINE_MODE:
        rep.notes.append("OFFLINE / DEMO MODE active — bina API key bhi sab demo chalta hai")
    return rep


def doctor() -> int:
    """Purana entry point — asli doctor ab jarvis.cli.doctor me hai."""
    print(config_report().as_text())
    return 0


def reload_settings() -> dict[str, str]:
    """`.env` dobara padho aur process env update karo (reload config ke liye)."""
    _load_env_file()
    return {"env": "reloaded"}
