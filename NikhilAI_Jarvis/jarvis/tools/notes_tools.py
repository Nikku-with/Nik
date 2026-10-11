"""
jarvis/tools/notes_tools.py — Notes / Notepad brain
===================================================

Owner bola: "jarvis ... note pad me ye xyz naam rakh ke save kardo" — yahi tool wo karta hai.

Do modes:
  1. `save_note(name, content)`            → workspace/notes/<name>.md (ya .txt) me likhta hai
  2. `save_note(..., open_in_notepad=True)` → Windows Notepad / macOS TextEdit me file khol deta hai

Plus: append, read, list, search notes (memory DB me bhi index hota hai, taaki baad me
"wo wala note nikaalo" bolne par mil jaye).
"""
from __future__ import annotations

import platform
import re
import subprocess
from datetime import datetime
from pathlib import Path

from langchain_core.tools import tool

from ..config import ASSISTANT_NAME, NOTES_DIR, OWNER_NAME
from ..memory.store import get_memory

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"


def _safe_name(name: str) -> str:
    n = (name or "jarvis_note").strip()
    n = re.sub(r"[^\w\u0900-\u097F .\-]+", "_", n)
    n = re.sub(r"\s+", "_", n.strip()) or "jarvis_note"
    return n[:80]


def _note_path(name: str, ext: str = ".md") -> Path:
    base = _safe_name(name)
    if base.lower().endswith((".md", ".txt")):
        return NOTES_DIR / base
    return NOTES_DIR / f"{base}{ext}"


def _open_in_editor(path: Path) -> str:
    try:
        if IS_WINDOWS:
            subprocess.Popen(["notepad.exe", str(path)])
            return "Notepad me khol diya"
        if IS_MAC:
            subprocess.Popen(["open", "-a", "TextEdit", str(path)])
            return "TextEdit me khol diya"
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "default editor me khol diya"
    except Exception as exc:
        return f"(editor open nahi hua: {exc})"


def _header(title: str, extra: str = "") -> str:
    ts = datetime.now().strftime("%d %b %Y, %I:%M %p")
    return (
        f"# {title}\n\n"
        f"> Saved by **{ASSISTANT_NAME}** for **{OWNER_NAME}** • {ts}{(' • ' + extra) if extra else ''}\n\n---\n\n"
    )


@tool
async def save_note(
    name: str,
    content: str,
    open_in_notepad: bool = False,
    as_txt: bool = False,
    tags: str = "",
) -> str:
    """Save a note to disk (and index it in memory). Optionally open it in Notepad.

    Use jab owner bole:
      - "notepad me meeting_notes naam se ye save kardo: ..."
      - "ye research notes me save karo"
      - "my_todo naam se file bana do"
    name = note/file ka naam, content = jo likhna hai.
    """
    path = _note_path(name, ".txt" if as_txt else ".md")
    body = content if content.strip().startswith(("#", ">")) else _header(_safe_name(name).replace("_", " ")) + content
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        try:
            from .file_tools import invalidate_index

            invalidate_index()
        except Exception:
            pass
        get_memory().index_note(name=_safe_name(name), path=str(path), content=content, tags=tags)
        opened = _open_in_editor(path) if open_in_notepad else ""
        return (
            f"✅ Note save ho gaya: **{path.name}**\n"
            f"📁 {path}\n"
            f"📝 {len(content)} chars" + (f"\n🪟 {opened}" if opened else "")
        )
    except Exception as exc:
        return f"❌ save_note error: {exc}"


@tool
async def append_note(name: str, content: str) -> str:
    """Append more content to an existing note (ya naya bana deta hai)."""
    path = _note_path(name)
    if not path.exists():
        # naam se dhoondhne ki koshish
        for p in NOTES_DIR.glob(f"*{_safe_name(name)}*"):
            path = p
            break
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n\n## Update — {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n\n{content}\n")
        get_memory().index_note(name=path.stem, path=str(path), content=content, tags="append")
        return f"➕ Append ho gaya: {path}"
    except Exception as exc:
        return f"❌ append_note error: {exc}"


@tool
async def read_note(name: str) -> str:
    """Read a saved note by name."""
    path = _note_path(name)
    if not path.exists():
        hits = list(NOTES_DIR.glob(f"*{_safe_name(name)}*"))
        if not hits:
            mem = get_memory()
            found = [h for h in mem.search(name, limit=3) if h.kind == "note"]
            if found:
                return f"🔎 Memory me mila:\n{found[0].text[:1500]}"
            return f"❌ Note nahi mila: {name}"
        path = hits[0]
    return f"📄 {path.name}\n---\n{path.read_text(encoding='utf-8', errors='ignore')[:6000]}"


@tool
async def list_notes(limit: int = 30) -> str:
    """List all saved notes (disk + memory index)."""
    files = sorted(NOTES_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    if not files:
        return "📭 Abhi tak koi note save nahi hua."
    lines = [f"- `{p.name}` — {p.stat().st_size/1024:.1f} KB" for p in files if p.is_file()]
    return f"📚 {len(lines)} notes ({NOTES_DIR}):\n" + "\n".join(lines)


@tool
async def search_notes(query: str) -> str:
    """Search inside all saved notes by keyword (Hinglish bhi chalta hai)."""
    hits = [h for h in get_memory().search(query, limit=8) if h.kind == "note"]
    if not hits:
        return f"❌ '{query}' kisi note me nahi mila."
    return "🔎 Notes me mila:\n" + "\n".join(f"- [{round(h.score,2)}] {h.text[:200]}" for h in hits)


NOTE_TOOLS = [save_note, append_note, read_note, list_notes, search_notes]
