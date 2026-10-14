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

import json
import time
from pathlib import Path
from typing import Any

from ..config import OWNER_NAME, SESSION_STATE_FILE, WORKSPACE_DIR

MAX_HISTORY = 30


class SessionState:
    """Thread-safe JSON scratchpad (chhota, readable, fast)."""

    def __init__(self, path: Path = SESSION_STATE_FILE):
        self.path = Path(path)
        self.data: dict[str, Any] = {}
        self.load()

    # ---------------------------------------------------------------- io
    def load(self) -> dict[str, Any]:
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
                default.update(loaded or {})
        except Exception:
            pass
        self.data = default
        return self.data

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------ mutators
    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set_workspace(self, path: str | Path) -> None:
        self.data["current_workspace"] = str(path)
        self.data["cwd"] = str(path)
        self.save()

    def add_task(
        self,
        goal: str,
        workers: list[str] | None = None,
        status: str = "running",
        result: str = "",
        artifacts: list[str] | None = None,
    ) -> None:
        self.data.setdefault("task_history", []).append(
            {
                "goal": (goal or "")[:300],
                "workers": workers or [],
                "status": status,
                "result": (result or "")[:400],
                "artifacts": (artifacts or [])[:8],
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        self.data["task_history"] = self.data["task_history"][-MAX_HISTORY:]
        self.save()

    def finish_task(self, status: str = "done", result: str = "", artifacts: list[str] | None = None) -> None:
        hist = self.data.get("task_history") or []
        if hist:
            hist[-1].update(
                {
                    "status": status,
                    "result": (result or "")[:400],
                    "artifacts": (artifacts or [])[:8],
                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            self.save()

    def remember_error(self, error: str) -> None:
        self.data["last_error"] = {"text": (error or "")[:600], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.save()

    def note_app_opened(self, app: str) -> None:
        """App-launcher memory: recents + per-app usage stats (count/last)."""
        name = (app or "").strip()
        if not name:
            return
        apps = [a for a in (self.data.get("open_apps") or []) if a.get("app") != name]
        apps.append({"app": name, "at": time.strftime("%H:%M:%S")})
        self.data["open_apps"] = apps[-10:]
        usage = self.data.get("app_usage") or {}
        key = name.lower()
        prev = usage.get(key) or {}
        usage[key] = {"name": name, "count": int(prev.get("count", 0)) + 1, "last": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.data["app_usage"] = usage
        self.save()

    def recent_apps(self, n: int = 5) -> list[str]:
        """Haal me khole apps (naya pehle)."""
        apps = [a.get("app", "") for a in (self.data.get("open_apps") or []) if a.get("app")]
        return list(reversed(apps[-max(1, n) :]))

    def most_used_apps(self, n: int = 5) -> list[tuple[str, int]]:
        """Sabse zyada khole apps [(name, count)]."""
        usage = self.data.get("app_usage") or {}
        ranked = sorted(
            ((v.get("name", k), int(v.get("count", 0))) for k, v in usage.items() if isinstance(v, dict)),
            key=lambda t: (-t[1], t[0].lower()),
        )
        return ranked[: max(1, n)]

    def recent_apps_hint(self) -> str:
        """Failure messages ke liye ek-line hint (khali ho to '')."""
        recent = self.recent_apps(3)
        if not recent:
            return ""
        return "Haal me khole apps: " + ", ".join(recent)

    def note_file_touched(self, path: str) -> None:
        files = [f for f in (self.data.get("recent_files") or []) if f.get("path") != path]
        files.append({"path": str(path), "at": time.strftime("%H:%M:%S")})
        self.data["recent_files"] = files[-15:]
        self.save()

    def set_pending_steps(self, steps: list[str]) -> None:
        self.data["pending_steps"] = [str(s)[:200] for s in (steps or [])][:10]
        self.save()

    # -------------------------------------------------- loop prevention
    def bump_counter(self, key: str) -> int:
        counters = self.data.setdefault("counters", {})
        counters[key] = int(counters.get(key, 0)) + 1
        self.save()
        return counters[key]

    def reset_counter(self, key: str) -> None:
        counters = self.data.setdefault("counters", {})
        if key in counters:
            counters[key] = 0
            self.save()

    def seen_signature(self, sig: str, limit: int = 6) -> int:
        """Kitni baar yahi (tool, args) signature dekha gaya — loop detection ke liye."""
        sigs = self.data.setdefault("tool_signatures", [])
        count = sigs.count(sig)
        sigs.append(sig)
        self.data["tool_signatures"] = sigs[-200:]
        self.save()
        return count

    # ------------------------------------------------------------- prompts
    def context_block(self, max_tasks: int = 5) -> str:
        """Supervisor/planner prompt me daalne ke liye compact context."""
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
                lines.append(
                    f"  - [{t.get('status')}] {t.get('goal', '')[:120]} (workers: {', '.join(t.get('workers') or [])})"
                )
        if d.get("preferences"):
            lines.append("Owner preferences: " + json.dumps(d["preferences"], ensure_ascii=False))
        return "\n".join(lines)


_STATE: SessionState | None = None


def get_state() -> SessionState:
    global _STATE
    if _STATE is None:
        _STATE = SessionState()
    return _STATE


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
