"""Deterministic offline tools and compact local file/note helpers."""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from ...config import FILE_SEARCH_ROOTS
from ...memory.session_state import get_state
from ...runtime.progress import emit
from .common import _tool_call_record
from .desktop_actions import _OPEN_VERBS

_STOP_HINTS = {"workspace", "jo", "wo", "woh", "mein", "me", "meri", "mera", "the", "file", "koi",
               "ko", "se", "par", "pe", "ek", "kya", "hai", "kaun", "kahan", "jarvis", "merko", "mujhe"}
_EXT_HINTS: dict[str, tuple[str, ...]] = {
    "pdf": (".pdf",), "word": (".docx", ".doc"), "docx": (".docx",), "excel": (".xlsx", ".xls", ".csv"),
    "xlsx": (".xlsx",), "sheet": (".xlsx", ".csv"), "csv": (".csv",), "ppt": (".pptx",),
    "powerpoint": (".pptx",), "image": (".png", ".jpg", ".jpeg", ".webp", ".gif"),
    "photo": (".png", ".jpg", ".jpeg"), "screenshot": (".png", ".jpg"), "markdown": (".md",),
    "note": (".md", ".txt"), "notes": (".md", ".txt"), "text": (".txt",), "txt": (".txt",),
    "json": (".json",), "zip": (".zip",),
}
_NOTE_NAME_STOP = {"karo", "kar", "krdo", "kardo", "likho", "likh", "de", "do", "save", "notepad",
                   "me", "file", "banao", "bana", "naam", "name", "se", "aur", "content", "ek", "ye",
                   "is", "ko", "note", "wala", "wali", "bhai"}
_LAST_RESCAN = [0.0]


async def _offline_tool_run(
    task: str, tools: list, done_actions: dict[str, Any] | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Offline demo: intent-based tools plus a per-turn successful-action ledger."""
    from ..idempotency import completed_action, remember_completed_action

    by_name = {getattr(t, "name", ""): t for t in tools}
    ledger = done_actions if done_actions is not None else {}
    low = task.lower()
    results: list[str] = []
    calls: list[dict[str, Any]] = []
    # Sawaal ko action na samjho: "notepad kaise kholte hain" par app open karna galat tha.
    question_only = bool(re.search(
        r"\b(kaise|kaisay|how\s+(?:do|can|should)\s+i|how to|kya hai|batao|samjhao|explain|tutorial|"
        r"steps|kyun|kyu)\b", low
    ))
    if question_only and re.search(r"\b(?:please do|for me|mere liye|kar do|karke do)\b", low):
        question_only = False

    def _record(name: str, args: dict, output: str, ok: bool = True, **extra) -> None:
        """Real kaam ka record (offline path me bhi) — bina iske success claim proof-less hai.

        `ok` = tool chala aur result mila. Outcome ka content `output` me hota hai; search jaise
        cases me `found` field alag se rakhi jaati hai taaki "kuch nahi mila" ko success na samjha jaye.
        """
        rec = {"name": name, "args": args, "output": str(output)[:800], "ok": bool(ok),
               "source": "deterministic"}
        rec.update(extra)
        calls.append(rec)

    async def _call(name: str, args: dict) -> None:
        t = by_name.get(name)
        if not t:
            return
        cached = completed_action(ledger, name, args)
        if cached:
            output = str(cached.get("output", "(previously completed)"))
            results.append(f"[{name}] already completed: {output[:600]}")
            calls.append({
                **cached, "name": name, "args": args, "ok": True,
                "source": "idempotent_cache", "replayed": True,
            })
            return
        emit("tool_start", f"{name} chala raha hoon", tool=name,
             detail=json.dumps(args, default=str)[:160])
        _t0 = time.time()
        try:
            out = await t.ainvoke(args)
            record = _tool_call_record(name, args, out)
            emit("tool_end", f"{name}: {str(out)[:100]}", tool=name,
                 status="ok" if record["ok"] else "fail",
                 ms=int((time.time() - _t0) * 1000), detail=str(out)[:300])
            results.append(f"[{name}] {str(out)[:600]}")
            calls.append(record)
            if record["ok"]:
                remember_completed_action(ledger, name, args, out, output=str(out), worker="desktop")
            try:
                get_state().note_file_touched(str(args.get("path") or args.get("name") or ""))
            except Exception:
                pass
        except Exception as exc:
            emit("error", f"{name} fail: {str(exc)[:140]}", tool=name, status="fail",
                 ms=int((time.time() - _t0) * 1000))
            results.append(f"[{name}] error: {exc}")
            calls.append({"name": name, "args": args, "output": f"error: {exc}", "ok": False})
            try:
                get_state().remember_error(f"{name}: {exc}")
            except Exception:
                pass

    # ---- file-manager undo: reverse only the last verified, unchanged operation ----
    if not question_only and re.search(r"\b(?:undo|revert|restore)\b", low) and any(
            key in low for key in ("file", "folder", "operation", "last", "delete", "write", "copy", "rename")):
        await _call("undo_file_operation", {})

    # ---- automations: "har subah 8 baje X" / "har ghante Y" / "remind me" ----
    if any(k in low for k in ("har subah", "har sham", "har shaam", "har raat", "har din", "daily",
                              "every day", "every hour", "har ghante", "har ghanta", "every ",
                              "remind", "yaad dila", "schedule", "automation banao", "kal ")):
        m = re.search(r"(har\s+[\w\s]{0,20}?\s*\d{1,2}(?::\d{2})?\s*(?:am|pm|baje)?|"
                      r"daily\s*\d{1,2}(?::\d{2})?|every\s*\d*\s*\w+|kal\s*\d{1,2}(?::\d{2})?\s*(?:am|pm|baje)?|"
                      r"\d{1,2}(?::\d{2})?\s*(?:am|pm|baje))", task, re.I)
        sched = m.group(1).strip() if m else "daily 08:00"
        await _call("create_automation", {"task": task, "schedule": sched})

    # ---- system ops: refresh / status / cache / process clear ----
    if any(k in low for k in ("refresh", "reload", "dobara load", "cache", "system status",
                              "machine ki halat", "kya haal hai system", "system info")):
        if any(k in low for k in ("refresh", "reload", "dobara load", "cache")):
            await _call("refresh_system_tool", {"clear_caches": True})
        else:
            await _call("full_system_status", {})
    if ("process" in low or "background" in low) and any(
            k in low for k in ("clear", "band", "kill", "hata", "rok")):
        await _call("clear_process_tool", {})

    # ---- seedha typing: "notepad me hello likho" (offline) ----
    if not question_only:
        typed = ""
        m_typed = re.search(r"(.+?)\s*(?:likho|likh do|type karo|type)\s*$", task, re.I)
        if m_typed:
            typed = m_typed.group(1).strip(' .,:"\'“”‘’')
            typed = re.sub(rf"^.*?\b(?:{_OPEN_VERBS}|me|mein|usme|usmein|par|pe|upar)\b[\s,]*", "", typed, flags=re.I)
        if typed and len(typed) >= 2 and any(k in low for k in ("likho", "likh", "type")):
            await _call("type_text", {"text": typed[:300], "press_enter": False})

    # ---- explicit file creation/write; require both a path and caller-supplied content ----
    explicit_file_save = _is_explicit_file_save(task) and not question_only
    if explicit_file_save:
        save_path = _extract_save_path(task)
        save_content = _extract_save_content(task)
        if save_path and save_content is not None:
            await _call("write_file", {"path": save_path, "content": save_content})
        else:
            missing = "file name" if not save_path else "file content"
            results.append(f"[write_file] {missing} unclear; no file was created.")

    # ---- note save? (sirf jab save karne ko kaha ho, sirf 'note' word se nahi) ----
    if not explicit_file_save and any(k in low for k in ("save kar", "save kardo", "save krdo", "likh do", "likh de",
                                                          "notepad me", "note me save", "note bana", "note banao")):
        await _call("save_note", {"name": _extract_note_name(task), "content": _extract_note_body(task),
                                  "open_in_notepad": False})

    # ---- app kholna (ab kisi bhi naam se — search based) ----
    # question_only: "notepad kaise kholte hain" par app kholna galat tha (kaise/tutorial).
    if not question_only and any(k in low for k in ("khol", "open", "launch", "chalu kar", "chala do")):
        hint = _extract_app_hint(task)
        if hint and hint.lower() not in {"kaise", "kaisay", "how", "kya", "kyun", "steps", "tutorial"}:
            await _call("open_any_app", {"query": hint})

    # ---- file dhoondhna + padhna (asli offline power) ----
    want_read = any(k in low for k in ("padho", "padh", "read", "kya likha", "summary", "summar",
                                       "dikhao", "nikaal", "dekh"))
    want_find = any(k in low for k in ("dhoondh", "find", "kahan hai", "dhundo", "locate", "search"))
    if want_find or (want_read and any(n in low for n in ("file", "notes", "note", "pdf", "excel",
                                                          "csv", "docx", "folder", "report"))):
        hint = _extract_file_hint(task)
        exts = _ext_filter(task)
        if hint:
            hits = await _local_file_find(hint, limit=5, exts=exts)
            if not hits and exts:
                loose = await _local_file_find(hint, limit=3)
                if loose:
                    results.append(f"[find_files] '{hint}' ki file mili par wo {', '.join(exts)} type ki nahi hai: "
                                   + ", ".join(f"{hit['name']} ({hit['score']}%)" for hit in loose))
            if not hits:
                results.append(f"[find_files] '{hint}' naam ki koi file nahi mili "
                               f"({'/'.join(exts) if exts else 'koi bhi type'}, roots: {', '.join(FILE_SEARCH_ROOTS)})")
                # search chali (ye asli tool call hai) par kuch nahi mila → found=False
                _record("find_files", {"name": hint, "exts": list(exts)},
                        f"0 matches ('{hint}', {('/'.join(exts)) if exts else 'any type'}, "
                        f"roots: {', '.join(FILE_SEARCH_ROOTS)})", ok=True, found=False)
            else:
                top = hits[0]
                alt = ", ".join(f"{h['name']} ({h['score']}%)" for h in hits[1:4])
                results.append(f"[find_files] {len(hits)} match{' (' + '/'.join(exts) + ')' if exts else ''}"
                               f" • best: {top['name']} ({top['score']}%)"
                               f"\n   {top['path']}" + (f"\n   aur bhi: {alt}" if alt else ""))
                _record("find_files", {"name": hint, "exts": list(exts)},
                        f"{len(hits)} match • best: {top['name']} ({top['score']}%) → {top['path']}"
                        + (f" • aur: {alt}" if alt else ""), ok=True, found=True,
                        path=top["path"])
                if want_read:
                    body = await _local_file_read(top["path"], max_chars=1500)
                    results.append(f"[read_file] 📄 {top['name']}\n{body}")
                    _record("read_file", {"path": top["path"], "max_chars": 1500},
                            f"📄 {top['name']} ({len(body or '')} chars)", ok=bool(body),
                            found=bool(body), path=top["path"])
    if not results:
        results.append("(offline desktop worker: koi concrete tool action match nahi hua — plan ke steps: " + task[:200] + ")")
    return "\n".join(results), calls

def _is_explicit_file_save(task: str) -> bool:
    low = (task or "").lower()
    return bool(re.search(
        r"\b(?:save|write|create)\s+(?:(?:a|the|this)\s+)?file\b|"
        r"\bfile\s+(?:me|mein|ko)\s*(?:save|likh|bana)\w*\b|\bsave\s+as\b", low,
    ))


def _extract_save_path(task: str) -> str:
    text = task or ""
    quote_rx = re.compile(
        r"\b(?:file\s+(?:named|called)|save\s+as|(?:file\s+)?name\s*[:=])\s*"
        r"[\"'“”‘’]([^\"'“”‘’]{1,240})[\"'“”‘’]", re.I,
    )
    match = quote_rx.search(text)
    if match:
        return match.group(1).strip()
    token_rx = re.compile(
        r"\b(?:file\s+(?:named|called)|save\s+as|(?:file\s+)?name\s*[:=])\s*"
        r"[\"'“”‘’]?([^\s,;:\"'“”‘’]{1,240})", re.I,
    )
    match = token_rx.search(text)
    if match:
        return match.group(1).rstrip(".!?")
    return ""


def _extract_save_content(task: str) -> str | None:
    text = task or ""
    match = re.search(r"\bcontent\s*(?:is\s+|[:=]\s*)(.*)$", text, re.I | re.S)
    if match:
        return match.group(1).strip()
    instruction = text[:re.search(r"\bcontent\b", text, re.I).start()] if re.search(r"\bcontent\b", text, re.I) else text
    if re.search(r"\b(?:empty|blank|khali)\s+file\b|\bfile\s+(?:empty|blank|khali)\b", instruction, re.I):
        return ""
    return None


def _extract_file_hint(task: str) -> str:
    """Request se file ka naam guess karo — extension, quotes, ya 'X wali file' pattern se."""
    t = (task or "").strip()
    m = re.search(r"[\"'“”‘’]([^\"'“”‘’]{3,60})[\"'“”‘’]", t)
    if m and "." in m.group(1):
        return m.group(1).strip()
    m = re.search(r"([\w\-]{2,50}\.\w{2,5})\b", t)
    if m:
        return m.group(1).strip()
    m = re.search(r"([\w\-]{3,50})\s+(?:wala|wali|wale|ka|ki|ke|naam\s+ki|naam\s+ka)?\s*"
                  r"(?:file|files|notes?|pdf|excel|csv|docx|folder|document|report|sheet)", t, re.I)
    if m:
        return _clean_hint(m.group(1))
    m = re.search(r"(?:file|notes?|pdf|excel|docx|folder|document|report)\s+"
                  r"(?:named?|naam\s+ki|jisda?\s+naam)?\s*([\w\-]{3,50})", t, re.I)
    if m:
        return _clean_hint(m.group(1))
    return ""

def _clean_hint(hint: str) -> str:
    words = [w for w in re.split(r"[\s,]+", hint.strip()) if w and w.lower() not in _STOP_HINTS]
    return " ".join(words[-3:]) if words else ""

def _extract_app_hint(task: str) -> str:
    """'safari khol do' / 'notepad open karo' → app ka naam."""
    m = re.search(r"(?:open|launch|chalu|start)\s+(?:kar\s*do|karo|kardo|krdo)?\s*([\w\-\.]{2,40})", task, re.I)
    if m:
        return _clean_hint(m.group(1))
    m = re.search(r"([\w\-\.]{2,40})\s+(?:ko\s+)?(?:khol|open|launch|chalu|start)", task, re.I)
    if m:
        return _clean_hint(m.group(1))
    return ""

def _ext_filter(task: str) -> tuple[str, ...]:
    """Request me type mention ho to wahi type ki file dhoondho ('resume pdf' → sirf .pdf)."""
    low = (task or "").lower()
    out: list[str] = []
    for key, vals in _EXT_HINTS.items():
        if re.search(rf"\b{re.escape(key)}", low):
            for v in vals:
                if v not in out:
                    out.append(v)
    return tuple(out)

def _extract_note_body(task: str) -> str:
    """Instruction hata ke sirf asli content nikalo.

    "notes me deals naam se ye save kardo: laptop 55k"  →  "laptop 55k"
    "yaad dilao ki ... notepad me todo naam se save kardo: doodh lana"  → "doodh lana"
    Pehle poora request hi content ban jata tha — wahi bug fix.
    """
    t = (task or "").strip()
    if not t:
        return t

    # 1) explicit separator (: — – newline) ke baad ka content sabse bharosemand hai
    for sep in (":", "\n", "—", "–"):
        if sep in t:
            after = t.split(sep, 1)[1].strip(" \t")
            if len(after) >= 2:
                return after

    # 2) instruction prefix ("notes me <name> naam se ye save kardo") hatao
    body = re.sub(
        r"^\s*(?:jarvis[,\s]+)?(?:notes?|note\s*pad|notepad|file)\s*(?:me(?:in)?)?\s*"
        r"(?:ye|yeh)?\s*[\w\-\. ]{0,40}?\s*(?:naam|name)\s*(?:se|rakh\w*|ke\s*naam\s*se)?\s*(?:ke|kar|karke)?\s*"
        r"(?:ye|yeh)?\s*(?:save\s*kar\w*|likh\s*(?:do|de|ke\s*rakh\w*)|rakh\w*|store\s*kar\w*)\s*[:,-]?\s*",
        "", t, flags=re.I,
    ).strip()
    if body and body != t:
        return body

    # 3) generic "ye save kardo ..." / "save kardo ..."
    body = re.sub(r"^\s*(?:jarvis[,\s]+)?(?:ye\s*|yeh\s*)?(?:save\s*kar\w*|likh\s*(?:do|de)|store\s*kar\w*)\s*[:,-]?\s*",
                  "", t, flags=re.I).strip()
    return body or t

def _extract_note_name(task: str) -> str:
    """'... naam laptop_deals ...' → laptop_deals  (pehle 'karo' jaisa word pakda ja raha tha)."""
    t = task or ""
    pats = [
        # "todo naam se" / "todo naam rakh ke" — sabse common Hinglish structure, isliye pehle
        r"([\w\-\.]{3,40})\s*(?:naam|name)\s*(?:se|rakh\w*|ke\s*naam\s*se)",
        # "naam: todo" / "name = todo"
        r"(?:naam|name)\s*[:=]\s*[\"\']?([\w\-\.]{3,40})",
        r"(?:naam|name)\s*(?:se|rakh\w*)?\s*[:=]?\s*[\"\']?([\w\-\.]{3,40})",
    ]
    for pat in pats:
        m = re.search(pat, t, re.I)
        if m:
            cand = m.group(1).strip().strip(".,;:!?")
            if cand.lower() in _NOTE_NAME_STOP or any(w.lower() in _NOTE_NAME_STOP for w in cand.split()):
                continue
            return cand
    m = re.search(r"[\"\'“”‘’]([^\"\'“”‘’]{3,40})[\"\'“”’‘’]", t)
    if m and m.group(1).strip().lower() not in _NOTE_NAME_STOP:
        return m.group(1).strip()
    return "jarvis_note"

def _RESCAN_OK(min_gap_s: float = 1.0) -> bool:
    """Forced file-index rescan ko throttle karo (har search par poori disk walk na ho)."""
    if time.time() - _LAST_RESCAN[0] < min_gap_s:
        return False
    _LAST_RESCAN[0] = time.time()
    return True

async def _local_file_find(name: str, limit: int = 5, exts: tuple[str, ...] = ()) -> list:
    """Index se fuzzy file dhoondho (stale index ho to forced rescan). Optionally type-filter."""
    from ...tools.file_tools import _fresh_index, _index_items, fuzzy_search

    ext_arg = ",".join(exts)
    hits = fuzzy_search(name, items=await _fresh_index(), limit=limit, ext=ext_arg)
    # Stale index me koi *adhoora* match (65-92%) aa gaya to forced rescan bhi karo —
    # warna user ki abhi banayi file purane fuzzy hit ke peeche chhup jaati hai.
    if not hits or hits[0].get("score", 0) < 92:
        if _RESCAN_OK():
            fresh = fuzzy_search(name, items=await _index_items(force=True), limit=limit, ext=ext_arg)
            if fresh and (not hits or fresh[0].get("score", 0) >= hits[0].get("score", 0)):
                hits = fresh
    return [h for h in hits if h.get("score", 0) >= 65]

async def _local_file_read(path: str, max_chars: int = 1500) -> str:
    """Kisi bhi type ki file ka text nikaalo (PDF/DOCX/XLSX/text…)."""
    from ...tools.file_readers import read_any_file

    try:
        res = await asyncio.to_thread(read_any_file, path, max_chars)
        text = (res or {}).get("text") or ""
        return text.strip()[:max_chars] or "(file khali hai)"
    except Exception as exc:
        return f"(read fail: {exc})"
