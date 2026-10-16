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

import asyncio
import inspect
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import AUTOMATION_FILE
from ..config import settings as S
from ..timezone import configured_timezone

log = logging.getLogger(__name__)


class TransientError(RuntimeError):
    """Automation runner may retry once only when this explicit error is raised."""


RUNNER: Callable[..., Any] | None = None   # (task_text, automation_id, run_id, origin_surface) → result
_DELIVERY_HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {}
_RUN_HISTORY: deque[dict[str, Any]] = deque(maxlen=200)
_RUN_PROGRESS: dict[str, list[dict[str, Any]]] = {}
_RUN_LOCK = threading.RLock()


def register_delivery_handler(surface: str, handler: Callable[[dict[str, Any]], Any] | None) -> None:
    key = str(surface or "").strip()
    if not key:
        return
    if handler is None:
        _DELIVERY_HANDLERS.pop(key, None)
    else:
        _DELIVERY_HANDLERS[key] = handler


def recent_runs(surface: str | None = None) -> list[dict[str, Any]]:
    with _RUN_LOCK:
        items = [dict(item) for item in _RUN_HISTORY]
    if surface:
        items = [item for item in items if item.get("surface") == surface]
    return items


def _timezone():
    return configured_timezone(str(S.TIMEZONE or "Asia/Kolkata"))


def _now() -> datetime:
    return datetime.now(_timezone())


def _from_timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(value, _timezone())


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
    last_fired_slot: float = 0.0
    last_result: str = ""
    last_run_id: str = ""
    last_status: str = ""
    last_surface: str = ""
    runs: int = 0
    errors: int = 0
    channel: str = "cli_owner"

    @property
    def next_run_str(self) -> str:
        if not self.next_run:
            return "-"
        return _from_timestamp(self.next_run).strftime("%d %b %Y, %H:%M %Z")

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
    now = _now()

    # every X minutes / hours
    m = re.search(r"(?:every|har|each)\s*(\d+)?\s*"
                  r"(m|min|mins|minute|minutes|घंट|h|hr|hrs|hour|hours|ghante|ghanta|d|day|days|din)\b", low)
    if m:
        count = max(1, int(m.group(1) or 1))
        unit = m.group(2)
        if unit in {"m", "min", "mins", "minute", "minutes"}:
            minutes = count
        elif unit in {"d", "day", "days", "din"}:
            minutes = count * 24 * 60
        else:
            minutes = count * 60
        unit_name = "min" if minutes < 60 else "h" if minutes < 24 * 60 else "d"
        return {"kind": "interval", "interval_minutes": minutes,
                "schedule": f"every {count} {unit_name}"}

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
        if not (0 <= hour <= 23) or not (0 <= minute <= 59):
            return {"kind": "", "error": "time range invalid hai — 00:00 se 23:59 use karein"}
        return {"kind": "daily", "hour": hour, "minute": minute, "schedule": f"daily {hour:02d}:{minute:02d}"}

    # "kal 6 baje" / "tomorrow 18:00"
    if re.search(r"\b(kal|tomorrow)\b", low):
        t = _TIME_RX.search(low)
        hour = int(t.group(1)) if t else 9
        minute = int(t.group(2) or 0) if t else 0
        ampm = (t.group(3) or "").lower() if t else ""
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if not (0 <= hour <= 23) or not (0 <= minute <= 59):
            return {"kind": "", "error": "time range invalid hai — 00:00 se 23:59 use karein"}
        when = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return {"kind": "once", "at": when.timestamp(), "schedule": when.strftime("once %d %b %H:%M %Z")}

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
        if not (0 <= hour <= 23) or not (0 <= minute <= 59):
            return {"kind": "", "error": "time range invalid hai — 00:00 se 23:59 use karein"}
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=1)
        return {"kind": "once", "at": when.timestamp(), "schedule": when.strftime("once %d %b %H:%M")}

    return {"kind": "", "error": "schedule samajh nahi aaya — try: 'har subah 8 baje', 'every 2h', "
                                 "'kal 6 PM', 'daily 18:30'"}


def _next_daily_slot(auto: Automation, now: float) -> float:
    match = re.search(r"\bdaily\s+(\d{1,2}):(\d{2})\b", auto.schedule, re.I)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        spec = parse_schedule(auto.schedule)
        hour, minute = int(spec.get("hour", 8)), int(spec.get("minute", 0))
    current = _from_timestamp(now)
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def _same_slot(left: float, right: float) -> bool:
    return left > 0 and right > 0 and abs(float(left) - float(right)) < 0.001


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
class AutomationStore:
    def __init__(self, path=AUTOMATION_FILE) -> None:
        self.path = Path(path)
        self.backup_path = self.path.with_name(self.path.name + ".bak")
        self._lock = threading.RLock()
        self._items: dict[str, Automation] = {}
        self.load_error = ""
        self._primary_valid = False
        self.load()

    def _decode_items(self, candidate: Path) -> dict[str, Automation]:
        raw = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("automations", []), list):
            raise ValueError("invalid automation document shape")
        items: dict[str, Automation] = {}
        skipped = 0
        for item in raw.get("automations", []):
            try:
                if not isinstance(item, dict) or not item.get("id"):
                    raise ValueError("invalid automation row")
                values = {k: v for k, v in item.items() if k in Automation.__dataclass_fields__}
                if values.get("channel") == "cli":
                    values["channel"] = "cli_owner"
                items[str(values["id"])] = Automation(**values)
            except Exception:
                skipped += 1
        if skipped:
            self.load_error = f"skipped {skipped} malformed automation row(s) from {candidate.name}"
        return items

    def load(self) -> None:
        with self._lock:
            self._items.clear()
            self.load_error = ""
            self._primary_valid = False
            if self.path.exists():
                try:
                    self._items = self._decode_items(self.path)
                    self._primary_valid = True
                    return
                except Exception as exc:
                    primary_error = type(exc).__name__
                    self.load_error = f"primary automation file unreadable ({primary_error})"
            else:
                primary_error = "missing"
            if self.backup_path.exists():
                try:
                    self._items = self._decode_items(self.backup_path)
                    self.load_error = f"{self.load_error}; restored {len(self._items)} automation(s) from backup"
                    return
                except Exception as exc:
                    self.load_error = f"automation primary/backup unreadable ({primary_error}/{type(exc).__name__})"

    def save(self) -> bool:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                payload = {"automations": [a.as_dict() for a in self._items.values()],
                           "_updated_at": _now().isoformat(timespec="seconds")}
                encoded = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
                if self.path.exists() and self._primary_valid:
                    fd, backup_tmp = tempfile.mkstemp(prefix=f".{self.backup_path.name}.", dir=self.path.parent)
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(self.path.read_bytes())
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(backup_tmp, self.backup_path)
                    finally:
                        try:
                            os.unlink(backup_tmp)
                        except FileNotFoundError:
                            pass
                fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                        handle.write(encoded)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp, self.path)
                finally:
                    try:
                        os.unlink(tmp)
                    except FileNotFoundError:
                        pass
                self._primary_valid = True
                self.load_error = ""
                return True
            except Exception as exc:
                self.load_error = f"automation persistence failed ({type(exc).__name__})"
                log.exception("Automation store save failed")
                return False

    # -------------------------------------------------------------- operations
    def create(self, task: str, schedule_text: str, channel: str = "", enabled: bool = True) -> dict[str, Any]:
        if not channel:
            try:
                from ..security.permissions import current_surface

                channel = current_surface()
            except Exception:
                channel = "cli_owner"
        spec = parse_schedule(schedule_text)
        if not spec.get("kind"):
            return {"ok": False, "message": spec.get("error", "schedule samajh nahi aaya")}
        now = time.time()
        if spec["kind"] == "once":
            next_run = float(spec["at"])
            interval = 0
        elif spec["kind"] == "daily":
            today = _now().replace(hour=spec["hour"], minute=spec["minute"], second=0, microsecond=0)
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
        if not self.save():
            with self._lock:
                self._items.pop(auto.id, None)
            return {"ok": False, "message": f"automation persist nahi hui: {self.load_error}"}
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
        now = time.time()
        previous = (auto.enabled, auto.next_run)
        auto.enabled = enabled
        if enabled and (not auto.next_run or auto.next_run <= now):
            if auto.kind == "daily":
                auto.next_run = _next_daily_slot(auto, now)
            elif auto.kind == "interval":
                auto.next_run = now + max(1, auto.interval_minutes) * 60
            elif auto.kind == "once":
                auto.next_run = now + 60
            else:
                auto.next_run = now + 3600
        if not self.save():
            auto.enabled, auto.next_run = previous
            return {"ok": False, "message": f"automation setting persist nahi hui: {self.load_error}"}
        return {"ok": True, "message": f"automation #{auto.id[:8]} {'ON' if enabled else 'OFF'} kar di"}

    def cancel(self, ident: str) -> dict[str, Any]:
        auto = self.get(ident)
        if not auto:
            return {"ok": False, "message": f"automation '{ident}' nahi mili"}
        with self._lock:
            self._items.pop(auto.id, None)
        if not self.save():
            with self._lock:
                self._items[auto.id] = auto
            return {"ok": False, "message": f"automation delete persist nahi hua: {self.load_error}"}
        return {"ok": True, "message": f"automation #{auto.id[:8]} delete kar diya ({auto.task[:50]})"}

    def due(self, now: float | None = None) -> list[Automation]:
        now = time.time() if now is None else now
        with self._lock:
            return [
                auto for auto in self._items.values()
                if auto.enabled and auto.next_run and auto.next_run <= now
                and not _same_slot(auto.last_fired_slot, auto.next_run)
            ]

    def claim_due(self, auto: Automation, now: float | None = None) -> float | None:
        """Atomically claim one scheduled slot so concurrent ticks cannot run it twice."""
        now = time.time() if now is None else now
        with self._lock:
            stored = self._items.get(auto.id)
            if not stored or not stored.enabled or not stored.next_run or stored.next_run > now:
                return None
            slot = float(stored.next_run)
            if _same_slot(stored.last_fired_slot, slot):
                return None
            previous = stored.last_fired_slot
            stored.last_fired_slot = slot
            if not self.save():
                stored.last_fired_slot = previous
                return None
            return slot

    def mark_run(
        self,
        auto: Automation,
        result: str,
        error: str = "",
        *,
        now: float | None = None,
        fired_slot: float | None = None,
        run_id: str = "",
        status: str = "",
        surface: str = "",
    ) -> None:
        now = time.time() if now is None else now
        auto.last_run = now
        auto.runs += 1
        auto.last_result = (result or "")[:400]
        auto.last_run_id = str(run_id or "")
        auto.last_status = str(status or ("error" if error else "done"))
        auto.last_surface = str(surface or auto.channel)
        if error:
            auto.errors += 1
        if fired_slot is not None:
            auto.last_fired_slot = float(fired_slot)
        if auto.kind == "once":
            auto.enabled = False
            auto.next_run = 0.0
        elif auto.kind == "daily":
            auto.next_run = _next_daily_slot(auto, now)
        elif auto.kind == "interval":
            auto.next_run = now + max(1, auto.interval_minutes) * 60
        else:
            auto.next_run = now + 3600
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
    """Automation ko turant chalao (schedule ka wait nahi); result status honest rahe."""
    store = get_store()
    auto = store.get(ident)
    if not auto:
        return {"ok": False, "message": f"automation '{ident}' nahi mili"}
    result = _execute(auto, scheduled=False)
    success = auto.last_status.lower() not in {
        "failed", "error", "blocked", "cancelled", "partial_failure", "done_with_errors", "unavailable",
    }
    status_text = "complete" if success else f"not successful ({auto.last_status})"
    latest = next((item for item in reversed(recent_runs(auto.channel))
                   if str(item.get("run_id")) == str(auto.last_run_id)), {})
    return {"ok": success, "run_id": auto.last_run_id, "status": auto.last_status,
            "progress": latest.get("progress", []),
            "message": f"automation #{auto.id[:8]} {status_text} → {result[:300]}"}


def _call_runner(runner: Callable[..., Any], task: str, auto_id: str, run_id: str, surface: str) -> Any:
    try:
        parameters = list(inspect.signature(runner).parameters.values())
        positional = [
            parameter for parameter in parameters
            if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        variadic = any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters)
        count = 4 if variadic or len(positional) >= 4 else 3 if len(positional) >= 3 else 2
    except (TypeError, ValueError):
        count = 4
    args = (task, auto_id, run_id, surface)
    return runner(*args[:count])


def _normalise_outcome(out: Any) -> tuple[str, str, str]:
    if hasattr(out, "as_dict"):
        try:
            out = out.as_dict()
        except Exception:
            pass
    if isinstance(out, dict):
        text = str(out.get("answer") or out.get("response") or out.get("spoken") or "")
        status = str(out.get("status") or "done")
        errors = out.get("errors") or []
        error = "; ".join(str(item)[:160] for item in errors if str(item).strip())
        if out.get("execution_required") and not out.get("verified_execution"):
            error = error or "execution evidence not verified"
        return text or "(no output)", status, error
    if hasattr(out, "answer"):
        text = str(getattr(out, "answer", "") or getattr(out, "spoken", "") or "(no output)")
        status = str(getattr(out, "status", "done") or "done")
        errors = getattr(out, "errors", []) or []
        error = "; ".join(str(item)[:160] for item in errors if str(item).strip())
        if getattr(out, "execution_required", False) and not getattr(out, "verified_execution", False):
            error = error or "execution evidence not verified"
        return text, status, error
    return str(out or "(no output)"), "done", ""


def _store_run_history(event: dict[str, Any]) -> None:
    with _RUN_LOCK:
        run_id = str(event.get("run_id", ""))
        for index in range(len(_RUN_HISTORY) - 1, -1, -1):
            if str(_RUN_HISTORY[index].get("run_id", "")) == run_id:
                _RUN_HISTORY[index] = dict(event)
                return
        _RUN_HISTORY.append(dict(event))


def _record_run_progress(
    task: str, auto_id: str, run_id: str, surface: str, progress_event: Any,
) -> None:
    kind = str(getattr(progress_event, "kind", "progress"))[:40]
    text = str(getattr(progress_event, "text", "") or getattr(progress_event, "tool", "") or kind)[:240]
    entry = {
        "kind": kind, "text": text, "tool": str(getattr(progress_event, "tool", ""))[:80],
        "status": str(getattr(progress_event, "status", ""))[:30],
        "ms": int(getattr(progress_event, "ms", 0) or 0), "at": time.time(),
    }
    with _RUN_LOCK:
        items = _RUN_PROGRESS.setdefault(str(run_id), [])
        items.append(entry)
        if len(items) > 80:
            del items[:-80]
        snapshot = [dict(item) for item in items]
    _store_run_history({
        "automation_id": str(auto_id), "run_id": str(run_id), "surface": str(surface),
        "task": str(task)[:500], "result": "", "status": "running", "error": "",
        "progress": snapshot, "updated_at": time.time(),
    })


def _take_run_progress(run_id: str) -> list[dict[str, Any]]:
    with _RUN_LOCK:
        return [dict(item) for item in _RUN_PROGRESS.pop(str(run_id), [])]


def _deliver_event(event: dict[str, Any]) -> None:
    _store_run_history(event)
    handler = _DELIVERY_HANDLERS.get(str(event.get("surface", "")))
    if handler is None:
        event["delivery"] = "no handler registered; result remains available in automation status"
        _store_run_history(event)
        return
    try:
        response = handler(dict(event))
        if inspect.isawaitable(response):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(response)
            else:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                raise RuntimeError("async delivery handler must be bridged to its origin event loop")
        event["delivery"] = "delivered"
    except Exception as exc:
        event["delivery"] = f"delivery failed ({type(exc).__name__})"
        log.warning("Automation result delivery failed to %s: %s", event.get("surface"), type(exc).__name__)
    _store_run_history(event)


def _execute(auto: Automation, *, scheduled: bool = False, now: float | None = None) -> str:
    store = get_store()
    fired_slot: float | None = None
    if scheduled:
        fired_slot = store.claim_due(auto, now=now)
        if fired_slot is None:
            return "(scheduled slot already claimed or not due)"
        run_id = f"{auto.id}:{fired_slot:.6f}"
    else:
        run_id = uuid.uuid4().hex

    surface = auto.channel if auto.channel != "cli" else "cli_owner"
    runner = RUNNER
    if runner is None:
        message = "runner unavailable; agent runtime was not started"
        store.mark_run(auto, "", error=message, now=now, fired_slot=fired_slot,
                       run_id=run_id, status="unavailable", surface=surface)
        _deliver_event({"automation_id": auto.id, "run_id": run_id, "surface": surface,
                        "task": auto.task, "result": "", "status": "unavailable", "error": message,
                        "ran_at": now or time.time()})
        return f"({message})"
    with _RUN_LOCK:
        _RUN_PROGRESS[str(run_id)] = []
    try:
        out = _call_runner(runner, auto.task, auto.id, run_id, surface)
        result, status, error = _normalise_outcome(out)
        progress = _take_run_progress(run_id)
        failed = bool(error) or status.lower() in {
            "failed", "error", "blocked", "cancelled", "partial_failure", "done_with_errors", "unavailable",
        }
        final_status = "failed" if failed else status
        store.mark_run(auto, result, error=error or (status if failed else ""), now=now,
                       fired_slot=fired_slot, run_id=run_id, status=final_status, surface=surface)
        event = {"automation_id": auto.id, "run_id": run_id, "surface": surface,
                 "task": auto.task, "result": result, "status": final_status,
                 "error": error, "progress": progress, "ran_at": now or time.time()}
        _deliver_event(event)
        return result
    except Exception as exc:
        message = f"automation failed ({type(exc).__name__})"
        progress = _take_run_progress(run_id)
        store.mark_run(auto, "", error=message, now=now, fired_slot=fired_slot,
                       run_id=run_id, status="error", surface=surface)
        _deliver_event({"automation_id": auto.id, "run_id": run_id, "surface": surface,
                        "task": auto.task, "result": "", "status": "error", "error": message,
                        "progress": progress, "ran_at": now or time.time()})
        return f"({message})"


def _run_coroutine_sync(coroutine_factory: Callable[[], Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine_factory())
    result: dict[str, Any] = {}
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            result["value"] = asyncio.run(coroutine_factory())
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker, name="nikhilai-scheduled-agent", daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result.get("value")


def configure_agent_runner() -> Callable[..., Any]:
    """Bind scheduled automation runs to the same shared agent and origin surface."""
    global RUNNER

    def runner(task: str, auto_id: str, run_id: str, surface: str = "cli_owner") -> dict[str, Any]:
        async def invoke():
            from ..runtime.agent import run_agent

            active_surface = surface or "cli_owner"
            result = await run_agent(
                task, session_id=f"automation:{auto_id}", surface=active_surface, run_id=run_id,
                on_progress=lambda event: _record_run_progress(task, auto_id, run_id, active_surface, event),
            )
            return result.as_dict() if hasattr(result, "as_dict") else dict(result)

        return _run_coroutine_sync(invoke)

    RUNNER = runner
    return runner


def _loop(interval: float = 20.0) -> None:
    while not _STOP.is_set():
        try:
            for auto in get_store().due():
                _execute(auto, scheduled=True)
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
