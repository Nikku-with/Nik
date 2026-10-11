"""
jarvis/tools/file_tools.py — File & folder brain (v2: dhoondho, padho, kholo, manage karo)
=========================================================================================

Owner ka ask: *"mai name bolu ye dhunde — chars se similar, alag alag jagah; koi bhi file read kare"*

Upgrade (original `Jarvis_file_opner.py` + `folder_file` se aage):
  * multi-root fuzzy search (default: home dir; env se custom roots) + TTL index cache
  * **multiple candidates** return karta hai (score ke saath) — sirf ek nahi
  * extension filter, "recent N days" filter, size filter, sorting
  * `search_inside_files` → content search (file ke andar text dhoondho)
  * `read_file` → koi bhi type (PDF/DOCX/XLSX/CSV/image/zip...) `file_readers` se
  * `open_path` → OS default app me kholo (Windows par PowerShell Start-Process, detached)
  * write/delete par protected-path guard
"""
from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from langchain_core.tools import tool

from ..config import (
    FILE_CONTENT_SEARCH_MAX,
    FILE_SEARCH_MAX_RESULTS,
    FILE_SEARCH_ROOTS,
    MAX_FILE_READ_CHARS,
    SAFETY_ALLOW_FILE_DELETE,
    WORKSPACE_DIR,
)
from .file_readers import read_any_file

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

_INDEX_CACHE: dict[str, object] = {"ts": 0.0, "items": [], "roots": ()}
INDEX_TTL_S = 20
MAX_INDEX_ITEMS = 60000

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "AppData", "$Recycle.Bin",
              ".cache", "site-packages", ".npm", ".cargo", "Library", ".local/share/Trash"}
_PROTECTED_PREFIXES: tuple[str, ...] = (
    "C:\\Windows", "C:\\Program Files", "/System", "/usr", "/etc", "/bin", "/boot", "/var",
)


def _is_protected(path: Path) -> bool:
    s = str(path)
    try:
        if path.exists():
            s = str(path.resolve())
    except Exception:
        pass
    return any(s.startswith(pref) for pref in _PROTECTED_PREFIXES)


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------
def invalidate_index() -> None:
    """Index cache clear — jab hamare tools khud file likhein (save_note/write_file/code)

    warna wahi file agle hi second 'dhoondho' par stale cache me na milti.
    """
    _INDEX_CACHE.update({"ts": 0.0, "items": [], "roots": ()})


async def _fresh_index(roots: list[str] | None = None, max_age_s: float = 5.0) -> list[dict]:
    """Index do, agAR cache purana ho to dobara banao — nayi banayi file turant mile."""
    age = time.time() - float(_INDEX_CACHE.get("ts") or 0)
    if _INDEX_CACHE["items"] and age <= max_age_s:
        return list(_INDEX_CACHE["items"])  # type: ignore[arg-type]
    return await _index_items(roots, force=True)


async def _index_items(roots: list[str] | None = None, force: bool = False) -> list[dict]:
    roots = tuple(roots or FILE_SEARCH_ROOTS)
    now = time.time()
    if (not force and _INDEX_CACHE["items"] and _INDEX_CACHE["roots"] == roots
            and now - float(_INDEX_CACHE["ts"]) < INDEX_TTL_S):
        return list(_INDEX_CACHE["items"])  # type: ignore[arg-type]

    items: list[dict] = []

    def _walk() -> None:
        for root in roots:
            base = Path(root).expanduser()
            if not base.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(base, onerror=lambda e: None):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
                for d in dirnames:
                    try:
                        st = os.stat(os.path.join(dirpath, d))
                        items.append({"name": d, "path": os.path.join(dirpath, d), "type": "folder",
                                      "mtime": st.st_mtime, "size": 0, "ext": ""})
                    except Exception:
                        items.append({"name": d, "path": os.path.join(dirpath, d), "type": "folder",
                                      "mtime": 0, "size": 0, "ext": ""})
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        st = os.stat(fp)
                        items.append({"name": f, "path": fp, "type": "file", "mtime": st.st_mtime,
                                      "size": st.st_size, "ext": Path(f).suffix.lower()})
                    except Exception:
                        items.append({"name": f, "path": fp, "type": "file", "mtime": 0, "size": 0,
                                      "ext": Path(f).suffix.lower()})
                if len(items) > MAX_INDEX_ITEMS:
                    return

    await asyncio.to_thread(_walk)
    _INDEX_CACHE.update({"ts": now, "items": items, "roots": roots})
    return items


try:  # optional fast matcher
    from rapidfuzz import fuzz as _RF_FUZZ  # type: ignore
except Exception:  # pragma: no cover
    _RF_FUZZ = None


def _similarity(query: str, name: str) -> float:
    """0-100 fuzzy score — chhote/typo query ko lambe naam ke andar bhi pakadta hai.

    Scoring = max(full ratio, query-coverage, longest common run, best token match).
    Chhote query (<4 chars) ke liye sirf literal match maano (warna 'ui' → 'runtime' jaisa galat match).
    """
    import difflib
    import re as _re

    ql = (query or "").strip().lower()
    nl = (name or "").strip().lower()
    if not ql or not nl:
        return 0.0
    stem = Path(nl).stem
    if ql in (nl, stem):
        return 100.0
    tokens = [t for t in _re.split(r"[^a-z0-9]+", stem) if t]
    if ql in tokens:
        return 96.0
    if len(ql) < 4:
        return 85.0 if ql in nl else 0.0

    sm = difflib.SequenceMatcher(None, ql, nl, autojunk=False)
    full = sm.ratio() * 100.0
    matched = sum(b.size for b in sm.get_matching_blocks())
    coverage = matched / max(1, len(ql))              # 0..1 fraction (contiguity-agnostic)
    longest = sm.find_longest_match(0, len(ql), 0, len(nl)).size
    run = longest / max(1, len(ql)) * 90.0            # contiguous run ka weight zyada
    token_best = max((difflib.SequenceMatcher(None, ql, t).ratio() * 95.0 for t in tokens), default=0.0)

    score = max(full, coverage * 62.0, run, token_best)   # fraction*62 → non-contiguous match kam score
    if _RF_FUZZ is not None:
        score = max(score, float(_RF_FUZZ.partial_ratio(ql, nl)) * 0.97)
    return min(score, 99.5)


def _best_match(query: str, choices: list[str]) -> tuple[str | None, int]:
    q = (query or "").strip()
    if not q or not choices:
        return None, 0
    try:
        from rapidfuzz import process as rf_process  # type: ignore

        return rf_process.extractOne(q, choices)  # type: ignore[misc]
    except Exception:
        pass
    try:
        from fuzzywuzzy import process as fz_process  # type: ignore

        return fz_process.extractOne(q, choices)  # type: ignore[misc]
    except Exception:
        pass
    scored = [(_similarity(q, c), c) for c in choices]
    score, best = max(scored, key=lambda t: t[0])
    if best is None or score < 55:
        return None, 0
    return best, int(score)


def fuzzy_search(
    query: str,
    kind: str | None = None,
    ext: str = "",
    recent_days: int = 0,
    min_kb: float = 0,
    limit: int = FILE_SEARCH_MAX_RESULTS,
    roots: list[str] | None = None,
    items: list[dict] | None = None,
) -> list[dict]:
    """Multi-candidate fuzzy search with filters. Score ke saath sorted results."""
    pool = items if items is not None else _INDEX_CACHE["items"]  # type: ignore[assignment]
    if pool is None:
        return []
    if kind:
        pool = [i for i in pool if i["type"] == kind]
    if ext:
        wanted = {e.strip().lower() if e.startswith(".") else "." + e.strip().lower() for e in ext.split(",") if e.strip()}
        pool = [i for i in pool if i.get("ext") in wanted]
    if recent_days > 0:
        cutoff = time.time() - recent_days * 86400
        pool = [i for i in pool if (i.get("mtime") or 0) >= cutoff]
    if min_kb > 0:
        pool = [i for i in pool if i["type"] == "folder" or (i.get("size") or 0) >= min_kb * 1024]
    if not pool:
        return []

    q = (query or "").strip()
    if not q:
        return sorted(pool, key=lambda i: -(i.get("mtime") or 0))[:limit]

    results: list[dict] = []
    ql = q.lower()
    for item in pool:
        name = item["name"].lower()
        stem = Path(name).stem
        score = 0.0
        if name == ql or stem == ql:
            score = 100.0
        elif name.startswith(ql) or stem.startswith(ql):
            score = 92.0
        elif ql in name:
            score = 85.0
        else:
            score = _similarity(ql, name)
        # recently modified files ko halka boost
        if score >= 55 and item.get("mtime"):
            age_days = (time.time() - item["mtime"]) / 86400
            if age_days < 7:
                score += 4
            elif age_days < 30:
                score += 2
        if score >= 60:
            results.append({**item, "score": round(min(score, 100.0), 1)})

    results.sort(key=lambda i: (-i["score"], -(i.get("mtime") or 0)))
    return results[:limit]


async def search_item(query: str, item_type: str | None = None, roots: list[str] | None = None) -> dict | None:
    """Backwards-compatible single-best search (original repo ka search_item)."""
    items = await _index_items(roots)
    hits = fuzzy_search(query, kind=item_type, items=items)
    return hits[0] if hits else None


sync_search_file = search_item


# ---------------------------------------------------------------------------
# open helpers
# ---------------------------------------------------------------------------
def _open_with_os(path: str, app: str = "") -> tuple[bool, str]:
    """
    File/folder ko OS default app (ya diye gaye app) se kholo.
    Windows par PowerShell Start-Process (quoting-safe) + detached (OpenClaw pattern).
    """
    p = Path(path).expanduser()
    if not p.exists():
        return False, f"path exist nahi karta: {path}"
    try:
        if app:
            if IS_MAC:
                subprocess.Popen(["open", "-a", app, str(p)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            elif IS_WINDOWS:
                ps = f"Start-Process -FilePath '{app}' -ArgumentList '{str(p).replace(chr(39), chr(39) * 2)}'"
                subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            else:
                subprocess.Popen([app, str(p)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            return True, f"{p} → {app}"
        if IS_WINDOWS:
            ps = f"Start-Process -FilePath '{str(p).replace(chr(39), chr(39) * 2)}'"
            subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        elif IS_MAC:
            subprocess.Popen(["open", str(p)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        else:
            if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
                subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            else:
                return False, "headless environment — GUI open possible nahi (path diya gaya hai)"
        return True, str(p)
    except Exception as exc:
        return False, str(exc)[:200]


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}GB"


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------
@tool
async def find_files(
    name: str = "",
    ext: str = "",
    kind: str = "any",
    recent_days: int = 0,
    min_kb: float = 0,
    limit: int = 10,
) -> str:
    """Fuzzy name se files/folders dhoondho — multiple matches, filters ke saath.

    Use jab owner bole: "resume dhoondho", "wo pdf nikaalo jo last week banaya tha",
    "saari excel files dikhao", "project folder kahan hai".
      • name : adhoora/galat naam chalega (chars similar ho to bhi match)
      • ext  : "pdf,docx" (comma separated)
      • kind : file | folder | any
      • recent_days : sirf last N din me badli files
      • min_kb : minimum size
    """
    items = await _fresh_index()
    hits = fuzzy_search(name, kind=None if kind == "any" else kind, ext=ext,
                        recent_days=recent_days, min_kb=min_kb, limit=limit, items=items)
    if not hits and name.strip():  # stale index shayad — ek forced rescan
        items = await _index_items(force=True)
        hits = fuzzy_search(name, kind=None if kind == "any" else kind, ext=ext,
                            recent_days=recent_days, min_kb=min_kb, limit=limit, items=items)
    if not hits:
        scope = ", ".join(FILE_SEARCH_ROOTS)
        return (f"❌ '{name or ext or kind}' ka kuch nahi mila.\n"
                f"• Search roots: {scope}\n"
                f"• Index me {len(items)} items hain — `JARVIS_FILE_SEARCH_ROOTS` badal kar aur jagah add kar sakte ho.")

    lines = [f"🔍 {len(hits)} match{'es' if len(hits) > 1 else ''}:"]
    for i, h in enumerate(hits, 1):
        when = datetime.fromtimestamp(h["mtime"]).strftime("%d %b %Y, %H:%M") if h.get("mtime") else "-"
        size = "DIR" if h["type"] == "folder" else _human_size(h.get("size") or 0)
        lines.append(f"{i}. {'📁' if h['type'] == 'folder' else '📄'} {h['name']}  [{size}, {when}, match {h['score']}%]\n   {h['path']}")
    return "\n".join(lines)


@tool
async def read_file(path: str, max_chars: int = 0, pages: str = "all", rows: int = 30, member: str = "") -> str:
    """KISI BHI type ki file padho — text/code/PDF/Word/Excel/CSV/JSON/notebook/PPT/image/zip.

    Agar pura exact path nahi pata to sirf naam do — ye khud dhoondh kar padh lega.
    member: archive (zip/tar) ke andar kisi specific file ka naam (optional).
    """
    target = Path(path).expanduser()
    if not target.exists():
        hits = fuzzy_search(path, items=await _fresh_index())
        if not hits or hits[0].get("score", 0) < 65:  # stale index ya kamzor match — forced rescan
            hits = fuzzy_search(path, items=await _index_items(force=True))
        if not hits or hits[0].get("score", 0) < 65:
            close = ", ".join(f"{h['name']} ({h['score']}%)" for h in hits[:3]) or "kuch nahi"
            return (f"❌ File nahi mili: {path}\n"
                    f"• Closest: {close}\n"
                    f"• Poora naam/path do, ya `find_files` se dhoondho.")
        target = Path(hits[0]["path"])
        prefix = f"🔎 '{path}' nahi mila tha, closest match use kiya: {target.name}\n\n"
    else:
        prefix = ""

    result = await asyncio.to_thread(
        read_any_file, target, max_chars or MAX_FILE_READ_CHARS, pages, rows, member
    )
    head = f"📄 {result['path']}  ({result['kind']}, {result['size_kb']} KB)\n" + "-" * 60 + "\n"
    return prefix + head + result["text"]


@tool
async def search_inside_files(query: str, ext: str = "", limit: int = 12, max_files: int = 0) -> str:
    """Files ke ANDAR ka content search karo (grep jaisa) — "kahan likha tha ye?" wale kaam ke liye.

    query: text/jugad (Hinglish bhi chalega), ext: sirf in extensions me dekho (e.g. "py,md,txt").
    """
    if not query.strip():
        return "❌ Query khali hai."
    wanted = {e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
              for e in ext.split(",") if e.strip()} or {".txt", ".md", ".py", ".json", ".csv", ".log",
                                                        ".js", ".ts", ".html", ".css", ".yaml", ".yml", ".sql"}
    items = await _fresh_index()
    candidates = [i for i in items if i["type"] == "file" and i.get("ext") in wanted
                  and 0 < (i.get("size") or 0) <= 3_000_000]
    candidates.sort(key=lambda i: -(i.get("mtime") or 0))
    cap = max_files or FILE_CONTENT_SEARCH_MAX
    candidates = candidates[:cap]

    ql = query.lower()
    matches: list[str] = []

    def _scan() -> list[str]:
        found: list[str] = []
        for item in candidates:
            try:
                text = Path(item["path"]).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if ql in text.lower():
                idx = text.lower().find(ql)
                start = max(0, idx - 90)
                snippet = re.sub(r"\s+", " ", text[start: idx + 160]).strip()
                found.append(f"📄 {item['name']}\n   {item['path']}\n   …{snippet}…")
                if len(found) >= limit:
                    break
        return found

    matches = await asyncio.to_thread(_scan)
    if not matches and not max_files:  # shayad index stale hai — ek baar forced rescan
        items = await _index_items(force=True)
        candidates = [i for i in items if i["type"] == "file" and i.get("ext") in wanted
                      and 0 < (i.get("size") or 0) <= 3_000_000]
        candidates.sort(key=lambda i: -(i.get("mtime") or 0))
        candidates = candidates[:cap]
        matches = await asyncio.to_thread(_scan)
    if not matches:
        return (f"❌ '{query}' {len(candidates)} files me nahi mila "
                f"(extensions: {', '.join(sorted(wanted))}).")
    return f"🔎 '{query}' {len(matches)} file(s) me mila:\n\n" + "\n\n".join(matches)


@tool
async def recent_files(days: int = 2, ext: str = "", limit: int = 15) -> str:
    """Recently modified files dikhao ("aaj kis file par kaam hua tha?")."""
    items = await _index_items()
    hits = fuzzy_search("", ext=ext, recent_days=days, limit=limit, items=items)
    if not hits:
        return f"❌ Last {days} din me koi file change nahi hui (ext filter: {ext or 'sab'})."
    lines = [f"🕒 Last {days} din ki {len(hits)} files:"]
    for h in hits:
        when = datetime.fromtimestamp(h["mtime"]).strftime("%d %b, %H:%M")
        lines.append(f"- {h['name']}  [{_human_size(h.get('size') or 0)}, {when}]\n  {h['path']}")
    return "\n".join(lines)


@tool
async def find_file(name: str, kind: str = "any") -> str:
    """(shortcut) Fuzzy naam se ek best file/folder dhoondho aur uska path do."""
    items = await _index_items()
    hits = fuzzy_search(name, kind=None if kind == "any" else kind, limit=3, items=items)
    if not hits:
        return f"❌ '{name}' nahi mila (roots: {', '.join(FILE_SEARCH_ROOTS)})."
    best = hits[0]
    extra = f"\n(baaki {len(hits) - 1} similar: " + ", ".join(h["name"] for h in hits[1:]) + ")" if len(hits) > 1 else ""
    return f"✅ Mil gaya: {best['name']} ({best['type']}, match {best['score']}%)\n📁 {best['path']}{extra}"


@tool
async def open_path(path: str, app: str = "") -> str:
    """File/folder ko kholo — default app se (ya `app` do to us app se, e.g. app="code").

    Agar exact path nahi pata to naam se dhoondh kar kholta hai.
    """
    target = Path(path).expanduser()
    if not target.exists():
        hits = fuzzy_search(path, items=await _fresh_index())
        if not hits:  # stale index ya abhi banayi gayi file — forced rescan
            hits = fuzzy_search(path, items=await _index_items(force=True))
        if not hits:
            return f"❌ '{path}' kuch nahi mila kholne ke liye."
        target = Path(hits[0]["path"])
    ok, detail = await asyncio.to_thread(_open_with_os, str(target), app)
    return f"📂 Khol diya: {detail}" if ok else f"❌ Open fail: {detail}"


@tool
async def play_file(name: str) -> str:
    """Media file dhoondh kar play karo (original Jarvis ka Play_file tool).

    Examples: "song chalao", "wo video play karo", "movie kholo".
    """
    items = await _index_items()
    media_exts = ".mp4,.mkv,.avi,.mov,.webm,.mp3,.wav,.m4a,.flac,.ogg"
    hits = fuzzy_search(name, ext=media_exts, limit=3, items=items) or fuzzy_search(name, limit=3, items=items)
    if not hits:
        return f"❌ '{name}' file nahi mili."
    ok, detail = await asyncio.to_thread(_open_with_os, hits[0]["path"])
    return f"▶️ {hits[0]['name']} play kar diya ({detail})" if ok else f"❌ Play fail: {detail}"


@tool
async def folder_file(command: str) -> str:
    """Folder/file manager — natural language: banao, rename karo, delete karo, kholo, copy karo.

    Examples: "projects folder banao", "old.txt ko new.txt rename karo",
              "temp.mp4 delete kar do", "Downloads folder kholo", "report.pdf ko D:/backup me copy karo".
    """
    if not command:
        return "❌ Khali command."
    low = command.lower()
    items = await _index_items()

    try:
        # --- create -------------------------------------------------------
        if any(k in low for k in ("create folder", "folder banao", "folder bana", "new folder", "folder bana do")):
            name = command
            for kw in ("create folder", "folder banao", "folder bana do", "folder bana", "new folder"):
                name = name.lower().replace(kw, " ")
            name = re.sub(r"\s+", "_", name.strip(" .-")) or "new_folder"
            target = WORKSPACE_DIR / name[:60]
            target.mkdir(parents=True, exist_ok=True)
            return f"📁 Folder ban gaya: {target}"

        # --- rename -------------------------------------------------------
        if "rename" in low or "naam badal" in low:
            for sep in (" to ", " ko ", " → ", "->"):
                if sep in command:
                    old_q, new_name = command.split(sep, 1)
                    break
            else:
                return "❌ Rename format: '<old> ko <new> rename karo'."
            old_q = re.sub(r"(rename|naam badal do|naam badal|karo|kar do)", " ", old_q, flags=re.I).strip()
            new_name = new_name.strip().strip(" .")
            hits = fuzzy_search(old_q, items=items, limit=1)
            if not hits:
                return f"❌ '{old_q}' nahi mila rename ke liye."
            src = Path(hits[0]["path"])
            dst = src.with_name(new_name)
            if dst.exists():
                dst = src.with_name(f"{Path(new_name).stem}_{int(time.time())}{Path(new_name).suffix}")
            src.rename(dst)
            return f"✅ Rename: {src.name} → {dst.name}\n📁 {dst}"

        # --- copy ---------------------------------------------------------
        if "copy" in low or "kopi" in low:
            for sep in (" to ", " me ", " mein ", " → "):
                if sep in command:
                    left, right = command.split(sep, 1)
                    break
            else:
                return "❌ Copy format: '<file> ko <destination folder> me copy karo'."
            src_q = re.sub(r"(copy|karo|kar do|ko)", " ", left, flags=re.I).strip()
            dest_q = right.replace("copy karo", "").replace("karo", "").strip()
            hits = fuzzy_search(src_q, items=items, limit=1)
            if not hits:
                return f"❌ Copy ke liye '{src_q}' nahi mila."
            src = Path(hits[0]["path"])
            dest_dir = Path(dest_q).expanduser()
            if not dest_dir.exists():
                dest_hits = fuzzy_search(dest_q, kind="folder", items=items, limit=1)
                if not dest_hits:
                    return f"❌ Destination folder nahi mila: {dest_q}"
                dest_dir = Path(dest_hits[0]["path"])
            target = dest_dir / src.name
            if src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=True)
            else:
                shutil.copy2(src, target)
            return f"📋 Copy ho gaya: {src.name} → {target}"

        # --- delete -------------------------------------------------------
        if any(k in low for k in ("delete", "hata do", "remove kar", "mita do")):
            if not SAFETY_ALLOW_FILE_DELETE:
                return "🚫 File delete disabled hai (JARVIS_ALLOW_FILE_DELETE=0)."
            q = re.sub(r"(delete|kar do|karo|hata do|remove kar|mita do|file|folder)", " ", low).strip()
            hits = fuzzy_search(q, items=items, limit=1) if q else []
            if not hits:
                return f"❌ Delete karne ke liye '{q}' nahi mila."
            p = Path(hits[0]["path"])
            if _is_protected(p):
                return f"🚫 Protected path — delete blocked: {p}"
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            return f"🗑️ Deleted: {p}"

        # --- open folder ---------------------------------------------------
        if "folder" in low and any(k in low for k in ("kholo", "open", "khol", "dikhao")):
            q = re.sub(r"(folder|kholo|open|khol|dikhao|kar do|karo)", " ", low).strip()
            hits = fuzzy_search(q, kind="folder", items=items, limit=1) if q else []
            if hits:
                _open_with_os(hits[0]["path"])
                return f"📂 Folder khol diya: {hits[0]['path']}"
            return f"❌ Folder '{q}' nahi mila. Roots: {', '.join(FILE_SEARCH_ROOTS)}"

        # --- default: kholo, warna naya file bana do -----------------------
        hits = fuzzy_search(command, items=items, limit=1)
        if hits:
            _open_with_os(hits[0]["path"])
            return f"📂 Khol diya: {hits[0]['path']}"
        target = WORKSPACE_DIR / f"{re.sub(r'[^\\w-]', '_', command)[:40]}.txt"
        target.write_text("", encoding="utf-8")
        return f"⚠️ Match nahi mila — naya file bana diya: {target}"
    except Exception as exc:
        return f"❌ folder_file error: {exc}"


@tool
async def write_file(path: str, content: str) -> str:
    """Text content file me likho (parent folders auto ban jaate hain; system paths blocked)."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = WORKSPACE_DIR / p
    if _is_protected(p):
        return f"🚫 Protected path — write blocked: {p}"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        invalidate_index()
        return f"✅ File likh di: {p} ({len(content)} chars)"
    except Exception as exc:
        return f"❌ write_file error: {exc}"


@tool
async def list_dir(path: str = "", pattern: str = "") -> str:
    """Directory listing (default: jarvis workspace). Pattern: '*.py' jaisa glob."""
    base = Path(path).expanduser() if path else WORKSPACE_DIR
    if not base.exists():
        hits = fuzzy_search(path, kind="folder", items=await _index_items(), limit=1)
        if not hits:
            return f"❌ Path nahi hai: {base}"
        base = Path(hits[0]["path"])
    try:
        entries = sorted(base.glob(pattern or "*"))[:200]
        lines = []
        for e in entries:
            size = _human_size(e.stat().st_size) if e.is_file() else "DIR"
            when = datetime.fromtimestamp(e.stat().st_mtime).strftime("%d %b %H:%M")
            lines.append(f"{'📁' if e.is_dir() else '📄'} {e.name:<45} {size:>9}  {when}")
        return f"📂 {base} ({len(lines)} entries)\n" + ("\n".join(lines) if lines else "(khali)")
    except Exception as exc:
        return f"❌ list_dir error: {exc}"


FILE_TOOLS = [find_files, find_file, read_file, search_inside_files, recent_files,
              open_path, play_file, folder_file, write_file, list_dir]
