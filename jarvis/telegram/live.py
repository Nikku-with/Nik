"""Telegram live progress — "🧠 soch raha hoon → 🛠️ tool chal raha → 🏁 done".

Ye module user ki us shikayat ka fix hai: "na thinking na planning na live progress
animations aati, error aaye to bot chup ho jata hai".

Kaam kaise karta hai:
    * Ek hi message bhejte hain, aur use Telegram `edit_message_text` se update karte
      rehte hain (spinner + last events) — isse chat spam nahi hota.
    * `on_event()` **sync** hai (fire-and-forget) — events ek queue me jaate hain aur ek
      background flusher task har ~1.1s me ek baar message edit karta hai (Telegram
      rate-limit safe). Isse agent ke tool calls kabhi block nahi hote.
    * Error aane par: readable error block + progress trail + traceback tail — chup nahi.
    * `/progress off` (ya settings `telegram_progress=off`) karne par sab normal.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from ..runtime.progress import SPINNER, ProgressEvent
from .md import chunk_text, html_chunks, md_to_html

MAX_EVENTS_SHOWN = 8
FLUSH_INTERVAL = 1.1


class LiveProgress:
    """Ek Telegram message jo live update hota hai (sync on_event + async flusher)."""

    def __init__(self, message: Any, *, enabled: bool = True, min_interval: float = FLUSH_INTERVAL,
                 max_events: int = MAX_EVENTS_SHOWN):
        self.message = message              # telegram Message (reply target)
        self.enabled = enabled
        self.min_interval = min_interval
        self.max_events = max_events
        self.events: list[ProgressEvent] = []
        self._live_msg = None               # humara progress message
        self._frame = 0
        self._started = time.time()
        self._current = "shuru kar raha hoon…"
        self._finished = False
        self._flusher: asyncio.Task | None = None
        self._dirty = True

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if not self.enabled:
            return
        try:
            self._live_msg = await self.message.reply_text(self._html(), parse_mode="HTML",
                                                           disable_web_page_preview=True)
        except Exception:
            self._live_msg = None
            return
        try:
            self._flusher = asyncio.create_task(self._loop())
        except RuntimeError:  # koi running loop nahi (tests) — theek hai
            self._flusher = None

    async def _loop(self) -> None:
        while not self._finished:
            await asyncio.sleep(self.min_interval)
            if self._finished:
                break
            if self._dirty:
                await self._flush()

    async def _flush(self) -> None:
        self._dirty = False
        self._frame += 1
        if self._live_msg is None:
            return
        try:
            await self._live_msg.edit_text(self._html(), parse_mode="HTML",
                                           disable_web_page_preview=True)
        except Exception:
            pass  # "message is not modified" / flood-wait — ignore, final message phir bhi jayega

    def on_event(self, event: ProgressEvent) -> None:
        """Sync callback (agent ke andar se aata hai) — sirf state update, koi I/O nahi."""
        self.events.append(event)
        self._dirty = True
        if event.kind == "tool_start":
            self._current = f"{event.tool or 'tool'} chal raha hai…"
        elif event.kind == "tool_end":
            self._current = (f"{event.tool or 'tool'} result received"
                             if event.status == "ok" else f"{event.tool or 'tool'} failed")
        elif event.kind == "model_start":
            self._current = f"Model request started: {event.tool or 'model'}"
        elif event.kind == "model_end":
            self._current = (f"Model response received: {event.tool or 'model'}"
                             if event.status == "ok" else f"Model request failed: {event.tool or 'model'}")
        elif event.kind == "thinking":
            self._current = event.text or "work in progress"
        elif event.kind == "plan":
            self._current = event.text or "plan event received"
        elif event.kind == "verify":
            self._current = event.text or "verification event received"
        elif event.kind in {"error", "warn"}:
            self._current = f"⚠️ {event.text or 'runtime warning'}"
        elif event.kind == "done" and event.status == "ok":
            self._current = "Request complete ✅"

    # ------------------------------------------------------------------ render
    def _html(self) -> str:
        spin = SPINNER[self._frame % len(SPINNER)]
        current = str(self._current or "")[:240]
        header = [f"<b>{spin} {md_to_html(current)}</b>",
                  f"<i>{time.time() - self._started:.1f}s • {len(self.events)} events</i>"]
        event_lines = [md_to_html(ev.line()[:260]) for ev in self.events[-self.max_events:]]
        if not event_lines:
            event_lines = ["…"]
        def render() -> str:
            return "\n".join(header + [""] + event_lines)

        while event_lines and len(render()) > 3900:
            event_lines.pop(0)
        if len(render()) > 3900:
            header[0] = f"<b>{spin} {md_to_html(current[:80])}</b>"
            event_lines = ["Progress details trimmed to Telegram's message limit."]
        rendered = render()
        return rendered if len(rendered) <= 3900 else f"<b>{spin} Progress update</b>"

    # ------------------------------------------------------------------ finish
    async def _stop_flusher(self) -> None:
        self._finished = True
        if self._flusher is not None and not self._flusher.done():
            self._flusher.cancel()
            try:
                await self._flusher
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def finish(self, answer: str, footer: str = "", errors: list[str] | None = None) -> None:
        """Progress message ko safe, independently valid Telegram HTML chunks se replace karo."""
        await self._stop_flusher()
        plain = f"{answer}\n{footer}".strip()
        if errors:
            shown = "\n".join(f"• {str(error)[:300]}" for error in errors[:4])
            plain += f"\n\n⚠️ Errors ({len(errors)}):\n{shown}"
            failed = [event for event in self.events if event.kind == "error" or event.status == "fail"]
            if failed:
                plain += "\n\nFailed steps:\n" + "\n".join(event.line() for event in failed[-3:])
        chunks = html_chunks(plain, limit=3900)
        if not self.enabled or self._live_msg is None:
            for chunk in chunks:
                await self.message.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)
            return
        try:
            await self._live_msg.edit_text(chunks[0], parse_mode="HTML", disable_web_page_preview=True)
            for chunk in chunks[1:]:
                await self.message.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            await self._fallback_send(plain)

    async def fail(self, exc: Exception, where: str = "agent") -> None:
        """Crash — safe HTML chunks me error, progress trail aur traceback tail dikhana."""
        import traceback

        await self._stop_flusher()
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
        parts = [f"❌ Error aa gaya ({where})", f"{type(exc).__name__}: {str(exc)[:300]}"]
        trail = [ev for ev in self.events[-6:] if ev.kind in ("tool_start", "tool_end", "error", "plan")]
        if trail:
            parts.append("Progress trail:\n" + "\n".join(ev.line() for ev in trail))
        parts.append(f"Traceback (tail):\n{tb[-700:]}")
        chunks = html_chunks("\n\n".join(parts), limit=3900)
        if self._live_msg is not None:
            try:
                await self._live_msg.edit_text(chunks[0], parse_mode="HTML", disable_web_page_preview=True)
                for chunk in chunks[1:]:
                    await self.message.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)
                return
            except Exception:
                pass
        for chunk in chunks:
            await self.message.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)

    async def _fallback_send(self, body: str) -> None:
        """HTML parse/edit fail ho to plain, length-bounded chunks me fallback bhejo."""
        if self._live_msg is not None:
            try:
                await self._live_msg.delete()
            except Exception:
                pass
        for chunk in chunk_text(body, limit=3900):
            await self.message.reply_text(chunk)


def progress_enabled_for(role: str = "owner") -> bool:
    """Settings se on/off (default: on)."""
    try:
        from ..config import runtime as R

        val = str(R.get("telegram_progress", "on")).lower()
        return val not in ("off", "0", "false", "no", "band", "none", "")
    except Exception:
        return True
