"""Markdown → Telegram-HTML converter.

Kyun chahiye: Telegram ka `ParseMode.MARKDOWN` har `_`, `*`, `[` par toot jata hai —
agent ke jawab me aksar backticks/underscores hote hain, message fail ho jata tha
("can't parse entities") aur fallback me plain text chala jata tha (formatting gayab).

Solution: hum khud markdown ko HTML me convert karte hain aur `parse_mode="HTML"` use
karte hain — HTML me sirf `<`, `>`, `&` escape karne padte hain. Predictable + safe.
"""
from __future__ import annotations

import html
import re

__all__ = ["md_to_html", "chunk_text"]

_CODE_BLOCK = re.compile(r"```([a-zA-Z0-9_+-]*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC = re.compile(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BULLET = re.compile(r"^[\-\*]\s+", re.MULTILINE)


def _stash(store: list[str], value: str) -> str:
    store.append(value)
    return f"\x00{len(store) - 1}\x00"


def md_to_html(text: str) -> str:
    """Markdown → Telegram-safe HTML. Unknown syntax = plain text (kuch tootta nahi)."""
    if not text:
        return ""
    stash: list[str] = []

    # 1) code blocks pehle (andar ka sab literal chhodna hai)
    def _code(m: re.Match) -> str:
        lang, body = m.group(1), m.group(2)
        label = f'<b>{html.escape(lang)}</b>\n' if lang else ""
        return _stash(stash, f"{label}<pre>{html.escape(body.rstrip())}</pre>")

    text = _CODE_BLOCK.sub(_code, text)
    text = _INLINE_CODE.sub(lambda m: _stash(stash, f"<code>{html.escape(m.group(1))}</code>"), text)

    # 2) baaki text escape
    text = html.escape(text, quote=False)

    # 3) markdown → HTML
    text = _HEADING.sub(lambda m: _stash(stash, f"<b>{html.escape(m.group(2))}</b>"), text)
    text = _LINK.sub(lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    text = _BULLET.sub("• ", text)

    # 4) placeholders wapas
    for i, value in enumerate(stash):
        text = text.replace(f"\x00{i}\x00", value)
    return text


def chunk_text(text: str, limit: int = 3900) -> list[str]:
    """Telegram 4096 limit — smart chunking: paragraph → line → word par todo."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1
        for sep in ("\n\n", "\n", ". ", " "):
            idx = window.rfind(sep)
            if idx > limit * 0.5:
                cut = idx + len(sep)
                break
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
