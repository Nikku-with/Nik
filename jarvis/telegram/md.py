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

__all__ = ["md_to_html", "chunk_text", "html_chunks"]

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

    # 2) Escape once before inserting formatting tags; also quote-escape link attributes.
    text = html.escape(text, quote=True)

    # 3) markdown → HTML (captured text/URLs are already safely escaped)
    text = _HEADING.sub(lambda m: _stash(stash, f"<b>{m.group(2)}</b>"), text)
    text = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
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


def html_chunks(text: str, limit: int = 3900) -> list[str]:
    """Convert Markdown to independently valid Telegram-HTML chunks under ``limit``.

    Split before Markdown conversion so tags/entities are never cut in half. If escaping
    expands a chunk beyond the Telegram limit, split that source chunk again and render
    each half independently; every returned item therefore has balanced formatting.
    """
    if limit < 16:
        raise ValueError("HTML chunk limit must be at least 16 characters")
    source = text or ""
    if not source:
        return [""]

    def render_part(part: str) -> list[str]:
        rendered = md_to_html(part)
        if len(rendered) <= limit:
            return [rendered]
        if len(part) <= 1:
            # Escaped single-character entities are shorter than the validated limit.
            return [rendered[:limit]]
        middle = len(part) // 2
        low = max(1, middle - max(1, limit // 8))
        high = min(len(part) - 1, middle + max(1, limit // 8))
        cut = middle
        for separator in ("\\n\\n", "\\n", " "):
            candidates = [part.rfind(separator, low, middle + 1), part.find(separator, middle, high)]
            candidates = [candidate for candidate in candidates if candidate >= 0]
            if candidates:
                cut = min(candidates, key=lambda candidate: abs(candidate - middle)) + len(separator)
                break
        cut = max(1, min(cut, len(part) - 1))
        return render_part(part[:cut]) + render_part(part[cut:])

    chunks: list[str] = []
    for piece in chunk_text(source, limit=limit):
        chunks.extend(render_part(piece))
    return chunks
