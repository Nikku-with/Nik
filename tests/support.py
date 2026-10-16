"""Small test-only helpers that are not part of the Jarvis runtime API."""
from __future__ import annotations

from jarvis.runtime.progress import ProgressEvent, Sink


class CollectingSink(Sink):
    """Collect progress events for deterministic behavioral assertions."""

    def __init__(self, limit: int = 500) -> None:
        self.events: list[ProgressEvent] = []
        self.limit = limit

    def __call__(self, event: ProgressEvent) -> None:
        self.events.append(event)
        if len(self.events) > self.limit:
            del self.events[: len(self.events) - self.limit]

    def lines(self) -> list[str]:
        return [event.line() for event in self.events]

    def errors(self) -> list[ProgressEvent]:
        return [event for event in self.events if event.kind == "error" or event.status == "fail"]
