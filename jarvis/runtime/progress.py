"""Live progress events — "thinking / planning / tool chal raha hai" ka real-time feed.

Problem jo ye solve karta hai:
    Pehle agent chup-chaap kaam karta tha. User ko Telegram/CLI par kuch dikhta hi nahi tha
    jab tak final jawab na aa jaye — lagta tha bot hang ho gaya hai.

Design:
    * `ProgressEvent` = ek chhota readable event (kind, text, tool, status, ms).
    * `emit()` ko koi bhi layer call kar sakta hai (graph nodes, tool loop, agent) —
      current sink ek `ContextVar` me hota hai, isliye koi function signature change nahi.
    * Default = `NullSink` (library/test use me kuch print nahi hota, honest + silent).
    * `CallbackSink` → CLI/TG apna renderer de dete hain.

Kinds: start · thinking · plan · tool_start · tool_end · verify · warn · error · done
"""
from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

__all__ = [
    "ProgressEvent", "Sink", "NullSink", "CallbackSink", "CollectingSink",
    "emit", "current_sink", "use_sink", "spinner_frame", "SPINNER",
]

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass
class ProgressEvent:
    kind: str                       # start|thinking|plan|tool_start|tool_end|verify|warn|error|done
    text: str = ""                  # human line (Hinglish)
    detail: str = ""                # extra: tool args / output snippet
    tool: str = ""
    status: str = ""                # ok | fail | skipped
    ms: int = 0
    ts: float = field(default_factory=time.time)

    def line(self) -> str:
        icons = {
            "start": "🚀", "thinking": "🧠", "plan": "📋", "tool_start": "🛠️",
            "tool_end": "✅", "verify": "🔎", "warn": "⚠️", "error": "❌", "done": "🏁",
        }
        icon = icons.get(self.kind, "•")
        if self.kind == "tool_end" and self.status == "fail":
            icon = "❌"
        tail = f" ({self.ms}ms)" if self.ms else ""
        return f"{icon} {self.text or self.tool}{tail}"


class Sink:
    """Base sink — sab methods optional override karo."""

    def __call__(self, event: ProgressEvent) -> None:  # pragma: no cover
        pass


class NullSink(Sink):
    """Kuch nahi karta (default)."""


class CallbackSink(Sink):
    def __init__(self, fn: Callable[[ProgressEvent], None]):
        self.fn = fn

    def __call__(self, event: ProgressEvent) -> None:
        try:
            self.fn(event)
        except Exception:  # noqa: BLE001 — progress kabhi main kaam na toote
            pass


class CollectingSink(Sink):
    """Tesla/debug ke liye — events list me jama karta hai."""

    def __init__(self, limit: int = 500):
        self.events: list[ProgressEvent] = []
        self.limit = limit

    def __call__(self, event: ProgressEvent) -> None:
        self.events.append(event)
        if len(self.events) > self.limit:
            del self.events[: len(self.events) - self.limit]

    def lines(self) -> list[str]:
        return [e.line() for e in self.events]

    def errors(self) -> list[ProgressEvent]:
        return [e for e in self.events if e.kind == "error" or e.status == "fail"]


_sink: contextvars.ContextVar[Sink] = contextvars.ContextVar("jarvis_progress_sink", default=NullSink())
_enabled: contextvars.ContextVar[bool] = contextvars.ContextVar("jarvis_progress_enabled", default=True)


def current_sink() -> Sink:
    return _sink.get()


class use_sink:
    """Context manager: `with use_sink(s): ...` → is block ke andar emit(s) isi sink par jayenge."""

    def __init__(self, sink: Sink | None, enabled: bool = True):
        self.sink = sink or NullSink()
        self.enabled = enabled
        self._token: contextvars.Token | None = None
        self._token2: contextvars.Token | None = None

    def __enter__(self) -> "use_sink":
        self._token = _sink.set(self.sink)
        self._token2 = _enabled.set(self.enabled)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            _sink.reset(self._token)
        if self._token2 is not None:
            _enabled.reset(self._token2)


def emit(kind: str, text: str = "", *, detail: str = "", tool: str = "", status: str = "",
         ms: int = 0, sink: Sink | None = None) -> ProgressEvent:
    """Event bhejo — koi sink nahi hai to chup-chaap no-op (honest, silent)."""
    ev = ProgressEvent(kind=kind, text=text, detail=detail, tool=tool, status=status, ms=ms)
    target = sink or _sink.get()
    if not _enabled.get():
        return ev
    try:
        target(ev)
    except Exception:  # noqa: BLE001
        pass
    return ev


def spinner_frame(i: int) -> str:
    return SPINNER[i % len(SPINNER)]
