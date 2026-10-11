"""
Automations — scheduled + recurring tasks
========================================

Owner bol sakta hai:
    "har subah 8 baje mausam batao"     → daily 08:00
    "har ghante ye check karo"          → every 1h
    "kal 6 baje ye karna"               → one-shot
    "6 PM par ye task chala do"         → daily 18:00

Sab `workspace/automations.json` me persist hoti hain, process restart ke baad bhi zinda rehti hain.
Scheduler ek daemon thread hai jo har 20 second tick karta hai (koi external dep nahi).
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from ..config import AUTOMATION_FILE

RUNNER: Callable[[str, str], str] | None = None   # (task_text, automation_id) → result text


@dataclass
class Automation:
    id: str
    task: str
    schedule: str                     # human-readable: "daily 08:00" | "every 1h" | "once 2026-10-05 18:00"
    kind: str = "daily"               # once | daily | hourly | interval
    interval_minutes: int = 0
    next_run: float = 0.0
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    last_run: float = 0.0
    last_result: str = ""
    runs: int = 0
    errors: int = 0
    channel: str = "cli"

    @property
    def next_run_str(self) -> str:
        if not self.next_run:
            return "-"
        return datetime.fromtimestamp(self.next_run).strftime("%d %b %Y, %H:%M")

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["next_run_str"] = self.next_run_str
        return d


# ---------------------------------------------------------------------------
# schedule parsing
# ---------------------------------------------------------------------------
_TIME_RX = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|baje|bje|o'?clock)?", re.I)


def parse_schedule(text: str) -> dict[str, Any]:
    """Natural language → schedule spec. Fail ho to clear error."""
    low = (text or "").lower().strip()
    now = datetime.now()

    # every X minutes / hours
    m = re.search(r"(?:every|har|each)\s*(\d+)?\s*"
                  r"(m|min|mins|minute|minutes|घंट|h|hr|hrs|hour|hours|ghante|ghanta)\b", low)
    if m:
        count = int(m.group(1) or 1)
        unit = m.group(2)
        minutes = count if unit in ("m", "min", "mins", "minute", "minutes") else count * 60
        return {"kind": "interval", "interval_minutes": max(1, minutes),
                "schedule": f"every {count} {'min' if minutes < 60 else 'h'}"}

    # daily at HH:MM
    m = re.search(r"(?:daily|roz|har\s*din|every\s*day|subah|shaam|raat|morning|evening)", low)
    t = _TIME_RX.search(low)
    if m and t:
        hour = int(t.group(1))
        minute = int(t.group(2) or 0)
        ampm = (t.group(3) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if "shaam" in low or "evening" in low:
            hour = hour + 12 if hour < 12 else hour
        if "raat" in low and hour < 12:
            hour += 12
        if not (0 <= hour <= 23):
            hour = 8
        return {"kind": "daily", "hour": hour, "minute": minute, "schedule": f"daily {hour:02d}:{minute:02d}"}

    # "kal 6 baje" / "tomorrow 18:00"
    if re.search(r"\b(kal|tomorrow)\b", low):
        t = _TIME_RX.search(low)
        hour = int(t.group(1)) if t else 9
        minute = int(t.group(2) or 0) if t else 0
        ampm = (t.group(3) or "").lower() if t else ""
        if ampm == "pm" and hour < 12:
            hour += 12
        when = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return {"kind": "once", "at": when.timestamp(), "schedule": when.strftime("once %d %b %H:%M")}

    # "6 PM par" (aaj ka time)
    t = _TIME_RX.search(low)
    if t and ("pm" in low or "am" in low or "baje" in low or ":" in low):
        hour = int(t.group(1))
        minute = int(t.group(2) or 0)
        ampm = (t.group(3) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        when = now.replace(hour=min(hour, 23), minute=minute, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=1)
        return {"kind": "once", "at": when.timestamp(), "schedule": when.strftime("once %d %b %H:%M")}

    return {"kind": "", "error": "schedule samajh nahi aaya — try: 'har subah 8 baje', 'every 2h', "
                                 "'kal 6 PM', 'daily 18:30'"}


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
class AutomationStore:
    def __init__(self, path=AUTOMATION_FILE) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._items: dict[str, Automation] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            self._items.clear()
            try:
                if self.path.exists():
                    raw = json.loads(self.path.read_text(encoding="utf-8")) or {}
                    for item in raw.get("automations", []):
                        try:
                            self._items[item["id"]] = Automation(**{
                                k: v for k, v in item.items() if k in Automation.__dataclass_fields__
                            })
                        except Exception:
                            continue
            except Exception:
                pass

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                payload = {"automations": [a.as_dict() for a in self._items.values()],
                           "_updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

    # -------------------------------------------------------------- operations
    def create(self, task: str, schedule_text: str, channel: str = "cli", enabled: bool = True) -> dict[str, Any]:
        spec = parse_schedule(schedule_text)
        if not spec.get("kind"):
            return {"ok": False, "message": spec.get("error", "schedule samajh nahi aaya")}
        now = time.time()
        if spec["kind"] == "once":
            next_run = float(spec["at"])
            interval = 0
        elif spec["kind"] == "daily":
            today = datetime.now().replace(hour=spec["hour"], minute=spec["minute"], second=0, microsecond=0)
            if today.timestamp() <= now:
                today += timedelta(days=1)
            next_run = today.timestamp()
            interval = 0
        elif spec["kind"] == "interval":
            interval = int(spec["interval_minutes"])
            next_run = now + interval * 60
        else:
            interval = 60
            next_run = now + 3600
        auto = Automation(id=uuid.uuid4().hex[:12], task=task.strip(), schedule=spec["schedule"],
                          kind=spec["kind"], interval_minutes=interval, next_run=next_run,
                          enabled=enabled, channel=channel)
        with self._lock:
            self._items[auto.id] = auto
        self.save()
        return {"ok": True, "automation": auto.as_dict(),
                "message": f"automation ban gayi #{auto.id[:8]} — {spec['schedule']} par: {auto.task[:70]}"}

    def list(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            items = [a.as_dict() for a in self._items.values()]
        if enabled_only:
            items = [i for i in items if i["enabled"]]
        items.sort(key=lambda i: i.get("next_run") or 0)
        return items

    def get(self, ident: str) -> Automation | None:
        with self._lock:
            if ident in self._items:
                return self._items[ident]
            for auto in self._items.values():
                if auto.id.startswith(ident) or ident.lower() in auto.task.lower():
                    return auto
        return None

    def set_enabled(self, ident: str, enabled: bool) -> dict[str, Any]:
        auto = self.get(ident)
        if not auto:
            return {"ok": False, "message": f"automation '{ident}' nahi mili"}
        auto.enabled = enabled
        if enabled and (not auto.next_run or auto.next_run < time.time()):
            auto.next_run = time.time() + max(60, (auto.interval_minutes or 1) * 60)
        self.save()
        return {"ok": True, "message": f"automation #{auto.id[:8]} {'ON' if enabled else 'OFF'} kar di"}

    def cancel(self, ident: str) -> dict[str, Any]:
        auto = self.get(ident)
        if not auto:
            return {"ok": False, "message": f"automation '{ident}' nahi mili"}
        with self._lock:
            self._items.pop(auto.id, None)
        self.save()
        return {"ok": True, "message": f"automation #{auto.id[:8]} delete kar diya ({auto.task[:50]})"}

    def due(self, now: float | None = None) -> list[Automation]:
        now = now or time.time()
        with self._lock:
            return [a for a in self._items.values() if a.enabled and a.next_run and a.next_run <= now]

    def mark_run(self, auto: Automation, result: str, error: str = "") -> None:
        auto.last_run = time.time()
        auto.runs += 1
        auto.last_result = (result or "")[:400]
        if error:
            auto.errors += 1
        if auto.kind == "once":
            auto.enabled = False
            auto.next_run = 0.0
        elif auto.kind == "daily":
            nxt = datetime.fromtimestamp(auto.next_run) + timedelta(days=1)
            auto.next_run = nxt.timestamp()
        elif auto.kind == "interval":
            auto.next_run = time.time() + max(1, auto.interval_minutes) * 60
        else:
            auto.next_run = time.time() + 3600
        self.save()


_STORE: AutomationStore | None = None
_THREAD: threading.Thread | None = None
_STOP = threading.Event()


def get_store() -> AutomationStore:
    global _STORE
    if _STORE is None:
        _STORE = AutomationStore()
    return _STORE


def run_now(ident: str) -> dict[str, Any]:
    """Automation ko turant chalao (schedule ka wait nahi)."""
    store = get_store()
    auto = store.get(ident)
    if not auto:
        return {"ok": False, "message": f"automation '{ident}' nahi mili"}
    result = _execute(auto)
    return {"ok": True, "message": f"automation #{auto.id[:8]} chala diya → {result[:300]}"}


def _execute(auto: Automation) -> str:
    if RUNNER is None:
        return "(runner set nahi hai — agent load hone ke baad chalega)"
    try:
        out = RUNNER(auto.task, auto.id)
        get_store().mark_run(auto, out or "(no output)")
        return out or "(no output)"
    except Exception as exc:
        get_store().mark_run(auto, "", error=str(exc))
        return f"(error: {exc})"


def _loop(interval: float = 20.0) -> None:
    while not _STOP.is_set():
        try:
            for auto in get_store().due():
                _execute(auto)
        except Exception:
            pass
        _STOP.wait(interval)


def start_background(interval: float = 20.0) -> bool:
    """Scheduler thread start karo (idempotent)."""
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return False
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, args=(interval,), name="nikhilai-scheduler", daemon=True)
    _THREAD.start()
    return True


def stop_background() -> None:
    _STOP.set()


def status() -> dict[str, Any]:
    store = get_store()
    items = store.list()
    return {
        "running": bool(_THREAD and _THREAD.is_alive()),
        "total": len(items),
        "enabled": len([i for i in items if i["enabled"]]),
        "next": items[0] if items else None,
    }
