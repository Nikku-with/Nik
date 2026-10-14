"""
jarvis/tools/file_readers.py — "kisi bhi file ko padh lo"
========================================================

Owner ka ask: *"ye kesi bhi file jaake read kr paaye"*.

Ye module file type detect karke sahi tarike se content nikalta hai:

  .txt .md .py .js .json .yaml .csv .log ...   → seedha text (encoding-tolerant)
  .pdf                                         → pypdf (page by page)
  .docx                                        → python-docx (paragraphs + tables)
  .xlsx .xlsm                                  → openpyxl (sheets → rows preview)
  .csv .tsv                                    → column-aware preview (sniffer)
  .json                                        → pretty-print / keys summary
  .zip .tar .gz                                → archive listing (+ ek file ke content optional)
  images (.png/.jpg/.webp/...)                 → size/dimensions metadata (vision LLM ko path de sakte ho)
  .ipynb                                       → notebook cells (code + markdown)
  .pptx                                        → python-pptx (slide text)
  audio/video                                  → duration/size metadata (ffprobe optional)
  unknown binary                               → hex preview + magic bytes (guess kare)

Sab readers **graceful degrade** karte hain: package missing ho to clean message dete hain.
"""
from __future__ import annotations

import csv
import io
import json
import mimetypes
import platform
import tarfile
import zipfile
from pathlib import Path
from typing import Any

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".ini", ".cfg", ".conf", ".env", ".toml", ".yaml", ".yml",
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp", ".cs",
    ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".sql", ".html", ".htm", ".css",
    ".scss", ".xml", ".svg", ".vue", ".svelte", ".dart", ".kt", ".swift", ".r", ".lua", ".pl", ".tex",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".ico", ".heic"}
ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar"}
MEDIA_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}


def detect_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return "text"
    if ext in {".pdf"}:
        return "pdf"
    if ext in {".docx", ".docm"}:
        return "docx"
    if ext in {".xlsx", ".xlsm", ".xls"}:
        return "excel"
    if ext in {".pptx"}:
        return "pptx"
    if ext in {".csv", ".tsv"}:
        return "csv"
    if ext in {".json", ".jsonl"}:
        return "json"
    if ext in {".ipynb"}:
        return "notebook"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in ARCHIVE_EXTS:
        return "archive"
    if ext in MEDIA_EXTS:
        return "media"

    # content sniffing
    try:
        head = path.read_bytes()[:2048]
        if head.startswith(b"%PDF"):
            return "pdf"
        if head.startswith(b"PK\x03\x04"):
            return "zip_like"
        if b"\x00" not in head:
            return "text"
    except Exception:
        pass
    return "binary"


def _read_text(path: Path, max_chars: int) -> str:
    raw = path.read_bytes()[: max_chars * 4]
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="ignore")
    return text[:max_chars]


def _read_pdf(path: Path, max_chars: int, pages: str = "all") -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return "⚠️ PDF padhne ke liye: pip install pypdf"
    reader = PdfReader(str(path))
    n = len(reader.pages)
    wanted = range(n) if pages == "all" else range(min(int(pages or 5), n))
    chunks = [f"📄 PDF: {path.name} — {n} pages"]
    total = 0
    for i in wanted:
        try:
            text = (reader.pages[i].extract_text() or "").strip()
        except Exception as exc:
            text = f"(page {i + 1} extract fail: {exc})"
        if not text:
            continue
        chunks.append(f"\n--- page {i + 1} ---\n{text}")
        total += len(text)
        if total > max_chars:
            chunks.append(f"\n… (truncated after {i + 1} pages)")
            break
    meta = {}
    try:
        meta = dict(reader.metadata or {})
    except Exception:
        pass
    if meta:
        title = meta.get("/Title")
        author = meta.get("/Author")
        if title or author:
            chunks.insert(1, f"(title: {title or '-'} | author: {author or '-'})")
    return "\n".join(chunks)


def _read_docx(path: Path, max_chars: int) -> str:
    try:
        import docx  # type: ignore
    except Exception:
        return "⚠️ DOCX padhne ke liye: pip install python-docx"
    d = docx.Document(str(path))
    parts = [f"📄 DOCX: {path.name} — {len(d.paragraphs)} paragraphs, {len(d.tables)} tables"]
    body: list[str] = []
    for p in d.paragraphs:
        if p.text.strip():
            style = (p.style.name or "").lower()
            prefix = "# " if "heading 1" in style else "## " if "heading" in style else ""
            body.append(prefix + p.text.strip())
    for ti, table in enumerate(d.tables[:5], 1):
        body.append(f"\n--- table {ti} ---")
        for row in table.rows[:25]:
            body.append(" | ".join(c.text.strip() for c in row.cells))
    text = "\n".join(body)
    return "\n".join(parts) + "\n\n" + text[:max_chars]


def _read_excel(path: Path, max_chars: int, rows: int = 30) -> str:
    try:
        import openpyxl  # type: ignore
    except Exception:
        return "⚠️ Excel padhne ke liye: pip install openpyxl"
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    out = [f"📊 Excel: {path.name} — sheets: {', '.join(wb.sheetnames)}"]
    budget = max_chars
    for ws in wb.worksheets[:4]:
        out.append(f"\n--- sheet: {ws.title} ({ws.max_row} rows × {ws.max_column} cols) ---")
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= rows:
                out.append(f"… (+{max(0, (ws.max_row or 0) - rows)} more rows)")
                break
            cells = ["" if c is None else str(c) for c in row]
            if any(cells):
                out.append(" | ".join(cells))
            budget -= 40
            if budget <= 0:
                break
    return "\n".join(out)[:max_chars]


def _read_csv_like(path: Path, max_chars: int, rows: int = 40) -> str:
    raw = _read_text(path, max_chars * 2)
    sample = raw[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
    except Exception:
        delim = "\t" if path.suffix.lower() == ".tsv" else ","
    reader = csv.reader(io.StringIO(raw), delimiter=delim)
    lines: list[str] = []
    header: list[str] = []
    for i, row in enumerate(reader):
        if i == 0:
            header = row
            lines.append(" | ".join(header))
            lines.append("-" * min(60, max(10, len(lines[0]))))
        elif i <= rows:
            lines.append(" | ".join(row))
        else:
            lines.append(f"… (+more rows)")
            break
    return f"📑 {path.name} ({len(header)} columns, delimiter '{delim}')\n" + "\n".join(lines)[:max_chars] if lines else "(khali file)"


def _read_json(path: Path, max_chars: int) -> str:
    raw = _read_text(path, max_chars * 3)
    try:
        if path.suffix.lower() == ".jsonl":
            items = [json.loads(l) for l in raw.splitlines()[:50] if l.strip()]
            return f"🧾 JSONL: {len(items)} records (pehle 50)\n" + json.dumps(items[:10], indent=2, ensure_ascii=False)[:max_chars]
        data = json.loads(raw)
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
        keys = list(data.keys())[:30] if isinstance(data, dict) else f"list of {len(data)}"
        return f"🧾 JSON keys: {keys}\n\n{pretty[:max_chars]}"
    except Exception as exc:
        return f"⚠️ JSON parse fail ({exc}) — raw preview:\n{raw[:max_chars]}"


def _read_notebook(path: Path, max_chars: int) -> str:
    try:
        nb = json.loads(_read_text(path, max_chars * 4))
    except Exception as exc:
        return f"⚠️ notebook parse fail: {exc}"
    out = [f"📓 Notebook: {path.name} — {len(nb.get('cells', []))} cells"]
    for i, cell in enumerate(nb.get("cells", [])[:40], 1):
        src = "".join(cell.get("source", []))
        if not src.strip():
            continue
        kind = cell.get("cell_type", "?")
        out.append(f"\n--- cell {i} ({kind}) ---\n{src[:1500]}")
    return "\n".join(out)[:max_chars]


def _read_pptx(path: Path, max_chars: int) -> str:
    try:
        from pptx import Presentation  # type: ignore
    except Exception:
        return "⚠️ PPTX padhne ke liye: pip install python-pptx"
    prs = Presentation(str(path))
    out = [f"📽️ PPTX: {path.name} — {len(prs.slides)} slides"]
    for i, slide in enumerate(prs.slides[:20], 1):
        texts = [shape.text.strip() for shape in slide.shapes if hasattr(shape, "text") and shape.text.strip()]
        if texts:
            out.append(f"\n--- slide {i} ---\n" + "\n".join(texts))
    return "\n".join(out)[:max_chars]


def _read_image(path: Path, max_chars: int) -> str:
    info = [f"🖼️ Image: {path.name}", f"size: {path.stat().st_size / 1024:.1f} KB"]
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as im:
            info.append(f"dimensions: {im.width}×{im.height}, mode: {im.mode}, format: {im.format}")
    except Exception:
        pass
    info.append(f"mime: {mimetypes.guess_type(str(path))[0]}")
    info.append("(vision-capable LLM ko ye path do ya browser/vision tool se analyse karwao)")
    return "\n".join(info)


def _read_archive(path: Path, max_chars: int, member: str = "") -> str:
    names: list[str] = []
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                if member:
                    with z.open(member) as f:
                        data = f.read(max_chars * 4)
                    return _slice_bytes(f"📦 {path.name} :: {member}", data, max_chars)
        elif tarfile.is_tarfile(path):
            with tarfile.open(path) as t:
                names = t.getnames()
                if member:
                    f = t.extractfile(member)
                    if f:
                        return _slice_bytes(f"📦 {path.name} :: {member}", f.read(max_chars * 4), max_chars)
    except Exception as exc:
        return f"⚠️ archive read fail: {exc}"
    listing = "\n".join(f"- {n}" for n in names[:200])
    more = f"\n… (+{len(names) - 200} more)" if len(names) > 200 else ""
    return f"📦 {path.name} — {len(names)} entries:\n{listing}{more}"


def _slice_bytes(label: str, data: bytes, max_chars: int) -> str:
    try:
        return f"{label}\n\n" + data.decode("utf-8", errors="ignore")[:max_chars]
    except Exception:
        return f"{label}\n(hex preview)\n" + data[:400].hex(" ")


def _read_media(path: Path, max_chars: int) -> str:
    info = [f"🎬 Media: {path.name}", f"size: {path.stat().st_size / 1e6:.2f} MB"]
    if platform.system() != "Windows" and __import__("shutil").which("ffprobe"):
        try:
            import subprocess

            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration,bit_rate", "-of", "json", str(path)],
                capture_output=True, text=True, timeout=15,
            )
            data = json.loads(out.stdout or "{}").get("format", {})
            if data.get("duration"):
                info.append(f"duration: {float(data['duration']):.1f}s")
            if data.get("bit_rate"):
                info.append(f"bitrate: {int(data['bit_rate']) // 1000} kbps")
        except Exception:
            pass
    return "\n".join(info)


def _read_binary(path: Path, max_chars: int) -> str:
    head = path.read_bytes()[:64]
    return (
        f"🔢 Binary file: {path.name} ({path.stat().st_size / 1024:.1f} KB)\n"
        f"magic bytes: {head[:16].hex(' ')}\n"
        "→ text ke roop me padha nahi ja sakta. Agar ye document hai to sahi extension do "
        "(pdf/docx/xlsx), ya hex/parse tool use karo."
    )


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def read_any_file(path: str | Path, max_chars: int = 20000, pages: str = "all",
                  rows: int = 30, member: str = "") -> dict[str, Any]:
    """Kisi bhi file ka content nikaalo. Returns {ok, kind, text, path, size_kb}."""
    p = Path(path).expanduser()
    if not p.exists():
        return {"ok": False, "kind": "missing", "text": f"❌ File nahi mili: {p}", "path": str(p), "size_kb": 0}
    if p.is_dir():
        entries = sorted(p.iterdir())[:100]
        listing = "\n".join(f"{'📁' if e.is_dir() else '📄'} {e.name}" for e in entries)
        return {"ok": True, "kind": "dir", "text": f"📂 {p} ({len(entries)} entries)\n{listing}", "path": str(p), "size_kb": 0}

    kind = detect_type(p)
    size_kb = round(p.stat().st_size / 1024, 1)
    try:
        if kind == "text":
            text = _read_text(p, max_chars)
        elif kind == "pdf":
            text = _read_pdf(p, max_chars, pages)
        elif kind == "docx":
            text = _read_docx(p, max_chars)
        elif kind == "excel":
            text = _read_excel(p, max_chars, rows)
        elif kind == "csv":
            text = _read_csv_like(p, max_chars, rows)
        elif kind == "json":
            text = _read_json(p, max_chars)
        elif kind == "notebook":
            text = _read_notebook(p, max_chars)
        elif kind == "pptx":
            text = _read_pptx(p, max_chars)
        elif kind == "image":
            text = _read_image(p, max_chars)
        elif kind == "archive":
            text = _read_archive(p, max_chars, member)
        elif kind == "media":
            text = _read_media(p, max_chars)
        elif kind == "zip_like":
            text = _read_archive(p, max_chars, member)
        else:
            text = _read_binary(p, max_chars)
        return {"ok": True, "kind": kind, "text": text, "path": str(p), "size_kb": size_kb}
    except Exception as exc:
        return {"ok": False, "kind": kind, "text": f"❌ read fail ({kind}): {exc}", "path": str(p), "size_kb": size_kb}


def supported_types() -> str:
    return (
        "text/code/config, PDF (pypdf), Word (python-docx), Excel (openpyxl), CSV/TSV, JSON/JSONL, "
        "Jupyter notebooks, PowerPoint (python-pptx), images (metadata), archives (zip/tar listing), "
        "audio/video (ffprobe metadata), unknown binary (hex preview)"
    )
