"""
jarvis/memory/session_state.py — Short-term context (human-readable `jarvis_memory.json`)
=======================================================================================

Owner ka ask (bilkul sahi): *"ek lightweight JSON file jisme har session ka context save ho —
current workspace, task history, last error — taaki wo loop me na phase."*

Do-layer memory design:
  * `jarvis_memory.sqlite3`  → LONG-TERM (facts, episodes, notes — durable knowledge)
  * `workspace/jarvis_memory.json` → SHORT-TERM working memory (agar owner khol kar padh le
    to samajh aaye; aur agent ko agle step me turant pata ho kya ho raha tha)

JSON me kya rehta hai:
  current_workspace, cwd, task_history (last N runs: goal/workers/status/result-hash),
  last_error, open_apps, recent_files, pending_steps, repl variables summary,
  preferences (owner ki style), counters (loop-prevention ke liye)
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config import OWNER_NAME, SESSION_STATE_FILE, WORKSPACE_DIR

MAX_HISTORY = 30
_LOG = logging.getLogger(__name__)
_STATE_IO_LOCK = threading.RLock()


class SessionState:
    """Thread-safe JSON scratchpad (chhota, readable, fast)."""

    def __init__(self, path: Path = SESSION_STATE_FILE):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {}
        self.load_error = ""
        self.load()

    # ---------------------------------------------------------------- io
    def load(self) -> dict[str, Any]:
        with self._lock, _STATE_IO_LOCK:
            default = {
                "owner": OWNER_NAME,
                "current_workspace": str(WORKSPACE_DIR),
                "cwd": str(WORKSPACE_DIR),
                "session_started": time.strftime("%Y-%m-%d %H:%M:%S"),
                "task_history": [],
                "last_error": None,
                "open_apps": [],
                "recent_files": [],
                "pending_steps": [],
                "preferences": {"language": "Hinglish", "style": "short, action-first, no fluff"},
                "counters": {},
                "notes": "",
            }
            try:
                if self.path.exists():
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    if not isinstance(loaded, dict):
                        raise ValueError("top-level state must be a JSON object")
                    default.update(loaded)
                self.load_error = ""
            except Exception as exc:
                self.load_error = f"session state unreadable: {type(exc).__name__}: {str(exc)[:160]}"
                _LOG.warning("Session state %s could not be loaded (%s)", self.path.name, type(exc).__name__)
            self.data = default
            return self.data

    def save(self) -> bool:
        """Serialize under locks and atomically replace the state file; report failures."""
        temp_path: str | None = None
        with self._lock, _STATE_IO_LOCK:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                payload = json.dumps(self.data, indent=2, ensure_ascii=False, default=str)
                fd, temp_path = tempfile.mkstemp(
                    prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent),
                )
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.path)
                temp_path = None
                self.load_error = ""
                return True
            except Exception as exc:
                self.load_error = f"session state write failed: {type(exc).__name__}: {str(exc)[:160]}"
                _LOG.warning("Session state %s could not be saved (%s)", self.path.name, type(exc).__name__)
                return False
            finally:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass

    def update_section(self, key: str, value: Any) -> bool:
        """Merge one top-level section under the shared lock, then atomically persist it."""
        with self._lock, _STATE_IO_LOCK:
            try:
                merged = dict(self.data)
                if self.path.exists():
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    if not isinstance(loaded, dict):
                        raise ValueError("top-level state must be a JSON object")
                    merged.update(loaded)
                merged[key] = value
                self.data = merged
                return self.save()
            except Exception as exc:
                self.load_error = f"session state unreadable: {type(exc).__name__}: {str(exc)[:160]}"
                _LOG.warning("Session state %s could not be merged (%s)", self.path.name, type(exc).__name__)
                return False

    # ------------------------------------------------------------ mutators
    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.data[key] = value
            self.save()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.data.get(key, default)

    def set_workspace(self, path: str | Path) -> None:
        with self._lock:
            self.data["current_workspace"] = str(path)
            self.data["cwd"] = str(path)
            self.save()

    def add_task(self, goal: str, workers: list[str] | None = None, status: str = "running",
                 result: str = "", artifacts: list[str] | None = None) -> None:
        with self._lock:
            self.data.setdefault("task_history", []).append({
                "goal": (goal or "")[:300],
                "workers": workers or [],
                "status": status,
                "result": (result or "")[:400],
                "artifacts": (artifacts or [])[:8],
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            self.data["task_history"] = self.data["task_history"][-MAX_HISTORY:]
            self.save()

    def finish_task(self, status: str = "done", result: str = "", artifacts: list[str] | None = None) -> None:
        with self._lock:
            hist = self.data.get("task_history") or []
            if hist:
                hist[-1].update({"status": status, "result": (result or "")[:400],
                                 "artifacts": (artifacts or [])[:8],
                                 "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})
                self.save()

    def remember_error(self, error: str) -> None:
        with self._lock:
            self.data["last_error"] = {"text": (error or "")[:600], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            self.save()

    def note_app_opened(self, app: str) -> None:
        with self._lock:
            apps = [a for a in (self.data.get("open_apps") or []) if a.get("app") != app]
            apps.append({"app": app, "at": time.strftime("%H:%M:%S")})
            self.data["open_apps"] = apps[-10:]
            self.save()

    def note_file_touched(self, path: str) -> None:
        with self._lock:
            files = [f for f in (self.data.get("recent_files") or []) if f.get("path") != path]
            files.append({"path": str(path), "at": time.strftime("%H:%M:%S")})
            self.data["recent_files"] = files[-15:]
            self.save()

    def set_pending_steps(self, steps: list[str]) -> None:
        with self._lock:
            self.data["pending_steps"] = [str(s)[:200] for s in (steps or [])][:10]
            self.save()

    # -------------------------------------------------- loop prevention
    def bump_counter(self, key: str) -> int:
        with self._lock:
            counters = self.data.setdefault("counters", {})
            counters[key] = int(counters.get(key, 0)) + 1
            self.save()
            return counters[key]

    def reset_counter(self, key: str) -> None:
        with self._lock:
            counters = self.data.setdefault("counters", {})
            if key in counters:
                counters[key] = 0
                self.save()

    def seen_signature(self, sig: str, limit: int = 6) -> int:
        """Kitni baar yahi (tool, args) signature dekha gaya — loop detection ke liye."""
        with self._lock:
            sigs = self.data.setdefault("tool_signatures", [])
            count = sigs.count(sig)
            sigs.append(sig)
            self.data["tool_signatures"] = sigs[-200:]
            self.save()
            return count

    # ------------------------------------------------------------- prompts
    def context_block(self, max_tasks: int = 5) -> str:
        """Supervisor/planner prompt me daalne ke liye compact context."""
        with self._lock:
            d = self.data
            lines = [
                f"Workspace: {d.get('current_workspace')}",
                f"Session started: {d.get('session_started')}",
            ]
            if d.get("pending_steps"):
                lines.append("Pending steps: " + "; ".join(d["pending_steps"]))
            if d.get("last_error"):
                le = d["last_error"]
                lines.append(f"Last error ({le.get('at')}): {le.get('text', '')[:200]}")
            if d.get("open_apps"):
                lines.append("Recently opened apps: " + ", ".join(a["app"] for a in d["open_apps"][-5:]))
            if d.get("recent_files"):
                lines.append("Recently touched files: " + ", ".join(f["path"] for f in d["recent_files"][-5:]))
            tasks = (d.get("task_history") or [])[-max_tasks:]
            if tasks:
                lines.append("Recent tasks:")
                for t in tasks:
                    lines.append(f"  - [{t.get('status')}] {t.get('goal', '')[:120]} (workers: {', '.join(t.get('workers') or [])})")
            if d.get("preferences"):
                lines.append("Owner preferences: " + json.dumps(d["preferences"], ensure_ascii=False))
            return "\n".join(lines)


_STATE: SessionState | None = None
_STATES: dict[str, SessionState] = {}
_STATE_LOCK = threading.RLock()
_CURRENT_STATE_USER: contextvars.ContextVar[str] = contextvars.ContextVar(
    "jarvis_state_user", default="nikhil_mehra")


@contextmanager
def use_state_user(user_id: str):
    token = _CURRENT_STATE_USER.set(str(user_id or "nikhil_mehra"))
    try:
        yield
    finally:
        _CURRENT_STATE_USER.reset(token)


def get_state(user_id: str | None = None) -> SessionState:
    global _STATE
    uid = str(user_id or _CURRENT_STATE_USER.get() or "nikhil_mehra")
    with _STATE_LOCK:
        if uid == "nikhil_mehra":
            if _STATE is None:
                _STATE = SessionState()
            return _STATE
        state = _STATES.get(uid)
        if state is None:
            digest = hashlib.sha256(uid.encode("utf-8", errors="ignore")).hexdigest()[:24]
            state = SessionState(WORKSPACE_DIR / ".private-state" / f"{digest}.json")
            _STATES[uid] = state
        return state


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
try:
    from langchain_core.tools import tool
except Exception:  # pragma: no cover
    tool = None  # type: ignore

if tool is not None:

    @tool
    async def session_context() -> str:
        """Short-term context dikhao: current workspace, recent tasks, last error, pending steps."""
        return "🧭 Session state (jarvis_memory.json):\n" + get_state().context_block(max_tasks=8)

    @tool
    async def set_workspace(path: str) -> str:
        """Jarvis ka current working folder set karo ("ab is project par kaam karo" ke baad)."""
        p = Path(path).expanduser()
        if not p.exists():
            return f"❌ Path nahi mila: {p}"
        get_state().set_workspace(p)
        return f"📁 Current workspace set: {p}"

    @tool
    async def session_note(note: str) -> str:
        """Short-term scratchpad me note likho (agla step ise dekh lega)."""
        get_state().set("notes", (note or "")[:2000])
        return f"📝 Session note saved: {note[:120]}"

    @tool
    async def set_pending_steps(steps: list[str]) -> str:
        """Multi-step kaam ke pending steps save karo (loop se bachne ke liye)."""
        get_state().set_pending_steps(steps)
        return f"📌 {len(steps)} pending steps saved."

    @tool
    async def set_preference(key: str, value: str) -> str:
        """Owner ki preference yaad rakho short-term state me (e.g. language, tone, default browser)."""
        prefs = get_state().get("preferences", {}) or {}
        prefs[key] = value
        get_state().set("preferences", prefs)
        return f"⚙️ Preference set: {key} = {value}"

    SESSION_TOOLS = [session_context, set_workspace, session_note, set_pending_steps, set_preference]
else:  # pragma: no cover
    SESSION_TOOLS = []
