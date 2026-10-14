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
from .md import md_to_html

MAX_EVENTS_SHOWN = 8
FLUSH_INTERVAL = 1.1


async def _attempt(factory, what: str = "live send"):
    """Send with retry (429/5xx/timeout) — .bot se lazy import (cycle-safe)."""
    try:
        from .bot import send_with_retry
    except Exception:
        return await factory()
    return await send_with_retry(factory, tries=2, what=what)


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
            self._live_msg = await _attempt(
                lambda: self.message.reply_text(self._html(), parse_mode="HTML",
                                                disable_web_page_preview=True),
                "live start")
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
            await _attempt(
                lambda: self._live_msg.edit_text(self._html(), parse_mode="HTML",
                                                 disable_web_page_preview=True),
                "live flush")
        except Exception:
            pass  # "message is not modified" / flood-wait — ignore, final message phir bhi jayega

    def on_event(self, event: ProgressEvent) -> None:
        """Sync callback (agent ke andar se aata hai) — sirf state update, koi I/O nahi."""
        self.events.append(event)
        self._dirty = True
        if event.kind == "tool_start":
            self._current = f"{event.tool or 'tool'} chal raha hai…"
        elif event.kind == "tool_end":
            self._current = "result aa gaya — agla step…"
        elif event.kind == "thinking":
            self._current = event.text or "soch raha hoon…"
        elif event.kind == "plan":
            self._current = "plan ready — execute kar raha hoon…"
        elif event.kind == "verify":
            self._current = "verify kar raha hoon…"
        elif event.kind == "error":
            self._current = f"⚠️ {event.text or 'error'}"
        elif event.kind == "done":
            self._current = "kaam khatam ✅"

    # ------------------------------------------------------------------ render
    def _html(self) -> str:
        spin = SPINNER[self._frame % len(SPINNER)]
        lines = [f"<b>{spin} {md_to_html(self._current)}</b>",
                 f"<i>{time.time() - self._started:.1f}s • {len(self.events)} events</i>", ""]
        if self.events:
            lines.extend(md_to_html(ev.line()) for ev in self.events[-self.max_events:])
        else:
            lines.append("…")
        return "\n".join(lines)

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
        """Progress message ko final jawab se replace karo (+ error block agar fail hua)."""
        await self._stop_flusher()
        body = md_to_html(f"{answer}\n{footer}")
        if errors:
            body += self._error_block(errors)
        if not self.enabled or self._live_msg is None:
            await _attempt(lambda: self.message.reply_text(body[:4000]), "live final")
            return
        try:
            await _attempt(lambda: self._live_msg.edit_text(body[:4000], parse_mode="HTML",
                                                             disable_web_page_preview=True),
                           "live final")
        except Exception:
            await self._fallback_send(body)

    async def fail(self, exc: Exception, where: str = "agent") -> None:
        """Crash — chup nahi rehna, poora error dikhana (user ki explicit demand)."""
        import traceback

        await self._stop_flusher()
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
        parts = [f"❌ <b>Error aa gaya</b> ({md_to_html(where)})",
                 f"<code>{md_to_html(type(exc).__name__)}: {md_to_html(str(exc)[:300])}</code>"]
        trail = [ev for ev in self.events[-6:] if ev.kind in ("tool_start", "tool_end", "error", "plan")]
        if trail:
            parts.append("<b>Progress trail:</b>\n" + "\n".join(md_to_html(e.line()) for e in trail))
        parts.append(f"<b>Traceback (tail):</b>\n<pre>{md_to_html(tb[-700:])}</pre>")
        text = "\n\n".join(parts)
        if self._live_msg is not None:
            try:
                await _attempt(lambda: self._live_msg.edit_text(text[:4000], parse_mode="HTML"),
                               "live error")
                return
            except Exception:
                pass
        await _attempt(lambda: self.message.reply_text(text[:4000], parse_mode="HTML"), "live error")

    async def _fallback_send(self, body: str) -> None:
        """HTML fail hua (gandha input) → progress message hatao, plain text bhejo."""
        if self._live_msg is not None:
            try:
                await _attempt(lambda: self._live_msg.delete(), "live cleanup")
            except Exception:
                pass
        plain = body.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
        plain = plain.replace("<code>", "").replace("</code>", "").replace("<pre>", "").replace("</pre>", "")
        await _attempt(lambda: self.message.reply_text(plain[:4000]), "live fallback")

    def _error_block(self, errors: list[str]) -> str:
        shown = "\n".join(f"• {md_to_html(str(e)[:300])}" for e in errors[:4])
        block = f"\n\n⚠️ <b>Errors ({len(errors)}):</b>\n{shown}"
        trail = self._trace_block()
        if trail:
            block += f"\n\n<b>Failed steps:</b>\n{trail}"
        return block

    def _trace_block(self) -> str:
        failed = [e for e in self.events if e.kind == "error" or e.status == "fail"]
        if not failed:
            return ""
        return "\n".join(md_to_html(e.line()) for e in failed[-3:])[:600]


def progress_enabled_for(role: str = "owner") -> bool:
    """Settings se on/off (default: on)."""
    try:
        from ..config import runtime as R

        val = str(R.get("telegram_progress", "on")).lower()
        return val not in ("off", "0", "false", "no", "band", "none", "")
    except Exception:
        return True
