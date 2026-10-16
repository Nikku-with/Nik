"""Observe-before-act deterministic desktop intent parser/executor."""
from __future__ import annotations

import re
import time
from typing import Any

from ...runtime.progress import emit
from ..idempotency import completed_action, remember_completed_action
from .common import _tool_call_record

_ACTION_KEYS = (
    "open", "kholo", "khol do", "khol", "launch", "chalu kar", "chala do", "start karo",
    "band kar", "close", "likho", "likh do", "type kar", "type karo", "likhde", "likh de",
    "save as", "save file", "save a file", "save the file", "write file", "write a file", "write the file",
    "create file", "create a file", "create the file", "file me save", "file mein save", "file banao", "file bana",
    "screenshot", "screen shot", "hotkey", "ctrl+", "alt+", "volume", "aawaz", "sunna",
    "minimize", "maximize", "focus",
)

# `khol` alone never matched "kholde"/"khol de"/"kholna"/"kholke" because the trailing `\b`
# failed on the Hinglish suffix, so "chrome kholde" silently fell through to the LLM planner.
# The suffix set below is explicit (not `khol\w*`) so questions like "kholta hai?" stay inert.
_OPEN_VERBS = (r"(?:open|khol\s*(?:o|de|do|na|ke|lo|diya|diye|dena|dunga|dijiye)?"
               r"|launch|chalu\s*kar\w*|chala\s*do|start\s*kar\w*)")

_TYPE_VERBS = r"(?:likh\w*|type\s*kar\w*|type|likh\s*de|likh\s*do)"

# A connector left over after the open-clause is not text the owner asked us to type.
# "notebook open krke likh" must ask what to write, not type the word "krke".
_TYPE_CONNECTOR_STOPWORDS = frozenset({
    "krke", "karke", "kar", "kr", "ke", "baad", "then", "phir", "aur", "and", "usme", "usmein",
    "us", "me", "mein", "upar", "par", "pe", "so", "ab", "hi", "to", "se", "ka", "ki", "ye",
    "yeh", "wo", "isko", "isse", "kuch", "txt", "text",
})

_CONNECTORS = (r"\b(?:krke|karke|ke\s*baad|then|aur|and|usme|usmein|us\s*me|me|mein|upar|par|pe|"
               r"phir|so|ab)\b")

_DIRECT_ACTION_SPLIT_RX = re.compile(r"\b(?:and|aur|then|phir)\b", re.I)

_INSTRUCTIONAL_QUESTION_RX = re.compile(
    r"\b(?:how\s+(?:do|can|should)\s+i|how\s+to|kaise|kaisay|steps\s+to|explain|tutorial)\b", re.I
)

def _is_instructional_question(text: str) -> bool:
    return bool(_INSTRUCTIONAL_QUESTION_RX.search(text or ""))

def detect_desktop_action(request: str) -> dict[str, Any]:
    """Request se actions nikalo (order ke saath). Kuch na mile to {} — tab AI normal kaam karega."""
    t = (request or "").strip()
    low = t.lower()
    if not low or _is_instructional_question(low):
        return {}
    if not any(k in low for k in _ACTION_KEYS):
        return {}

    actions: list[dict[str, Any]] = []

    # App opens are individual ordered actions: "Notepad kholo aur Chrome kholo" must not
    # execute only the first clause. Support both app-before-verb and "open App" syntax.
    for clause in _DIRECT_ACTION_SPLIT_RX.split(low):
        m_open = re.search(rf"\b([a-z0-9][a-z0-9 ._+-]{{1,28}}?)\s+{_OPEN_VERBS}\b", clause)
        if not m_open:
            m_open = re.search(rf"\b{_OPEN_VERBS}\s+([a-z0-9][a-z0-9 ._+-]{{1,28}}?)\s*$", clause)
        if not m_open:
            continue
        # sirf aage-peeche punctuation hatao — `strip(" ,.yehuthis")` bug tha:
        # wo "chrome" ko "chrom" aur "vs code" ko "vs cod" bana deta tha.
        app = m_open.group(1).strip(" ,.-")
        app = re.sub(r"^(?:jarvis|bhai|boss|please|plz|mera|meri|ye|yeh|wo|the|a|an)\s+", "", app).strip()
        if app and app not in {"kuch", "kaam", "isse", "isko"}:
            actions.append({"kind": "open_app", "app": app[:40]})

    # text typing: "... likho: TEXT" / "... TEXT likho" / "type karo TEXT"
    m_type_sep = re.search(rf"{_TYPE_VERBS}\s*[:\-]\s*(.+)$", t, re.I)
    m_type_tail = re.search(rf"(.+?)\s+{_TYPE_VERBS}\s*$", t, re.I)
    typed = ""
    if m_type_sep:
        typed = m_type_sep.group(1)
    elif m_type_tail:
        typed = m_type_tail.group(1)
        # pehle wale open-clause ko hatao: "notepad open krke i hate you" → "i hate you"
        typed = re.sub(rf"^.*?{_OPEN_VERBS}\b", "", typed, flags=re.I)
        typed = re.sub(rf"^[\s,]+(?:{_CONNECTORS.strip('()?')})[\s,]+", "", typed, flags=re.I)
        typed = re.sub(rf"^(?:{_CONNECTORS.strip('()?')})[\s,]+", "", typed, flags=re.I)
    typed = (typed or "").strip(' .,"\'“”‘’')
    wants_typing = ("likh" in low or "type" in low)
    typed_stop = re.sub(r"[^a-z0-9]+", "", typed.lower())
    if wants_typing and typed and typed_stop not in _TYPE_CONNECTOR_STOPWORDS:
        actions.append({"kind": "type_text", "text": typed[:400], "press_enter": bool(
            re.search(r"(enter|⏎|nayi line|new line)", low))})
    elif wants_typing:
        # A typing verb with no real payload ("notepad kholke likho", "notebook open krke
        # likh"). Record the intent so the caller asks one question instead of typing the
        # leftover connector word or silently doing nothing.
        actions.append({"kind": "type_text", "text": "", "press_enter": False})

    # screenshot
    if "screenshot" in low or "screen shot" in low:
        actions.append({"kind": "screenshot"})

    # hotkey: "ctrl+s dabao", "alt+f4"
    m_hk = re.search(r"\b((?:ctrl|alt|shift|win|cmd|command|super)(?:\s*\+\s*[a-z0-9]+)+)\b", low)
    if m_hk:
        actions.append({"kind": "hotkey", "keys": m_hk.group(1).replace(" ", "")})

    # volume
    if "volume" in low or "aawaz" in low:
        if any(k in low for k in ("mute", "band", "silent")):
            actions.append({"kind": "volume", "action": "mute"})
        elif any(k in low for k in ("badha", "up", "tez", "increase", "zyada")):
            actions.append({"kind": "volume", "action": "up"})
        elif any(k in low for k in ("kam", "down", "decrease", "ghata")):
            actions.append({"kind": "volume", "action": "down"})

    # close app: "notepad band karo"
    m_close = re.search(r"\b([a-z0-9][a-z0-9 ._+-]{1,24}?)\s*(?:band|close|kill)\w*", low)
    if m_close and any(k in low for k in ("band", "close", "kill")):
        app = m_close.group(1).strip(" ,.")
        if app and app not in {"ye", "yeh", "wo", "isko", "isse", "sab"}:
            actions.append({"kind": "close_app", "app": app[:40]})

    # dedupe (same kind + same key) order preserve
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for a in actions:
        key = f"{a['kind']}:{a.get('app') or a.get('text') or a.get('keys') or a.get('action')}"
        if key not in seen:
            seen.add(key)
            unique.append(a)
    return {"actions": unique} if unique else {}

async def _execute_desktop_action(
    action: dict[str, Any], tools: list, done_actions: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Deterministic desktop action with per-turn successful-call deduplication."""

    by_name = {getattr(t, "name", ""): t for t in (tools or [])}
    ledger = done_actions if done_actions is not None else {}
    records: list[dict[str, Any]] = []

    async def call(name: str, args: dict) -> None:
        t = by_name.get(name)
        if t is None:
            emit("error", f"Tool '{name}' is worker me nahi mila", tool=name, status="fail")
            records.append({"name": name, "args": args, "ok": False,
                            "output": f"(tool '{name}' is worker me available nahi hai)"})
            return
        cached = completed_action(ledger, name, args)
        if cached:
            records.append({
                **cached, "name": name, "args": args, "ok": True,
                "source": "idempotent_cache", "replayed": True,
            })
            return
        emit("tool_start", f"{name} chala raha hoon", tool=name, detail=str(args)[:160])
        _t0 = time.time()
        try:
            out = await t.ainvoke(args)
            rec = _tool_call_record(name, args, out)
            ok = rec["ok"]
            emit("tool_end", f"{name}: {str(out)[:100]}", tool=name, ms=int((time.time() - _t0) * 1000),
                 status="ok" if ok else "fail", detail=str(out)[:300])
            rec["source"] = "deterministic"        # LLM se nahi, verification loop se chala
            records.append(rec)
            if ok:
                remember_completed_action(ledger, name, args, out, output=str(out), worker="desktop")
        except Exception as exc:
            emit("error", f"{name} fail: {str(exc)[:140]}", tool=name, status="fail",
                 ms=int((time.time() - _t0) * 1000))
            records.append({"name": name, "args": args, "ok": False, "output": f"error: {exc}",
                            "source": "deterministic"})

    app_open_ok: bool | None = None
    has_open_step = any(a.get("kind") == "open_app" for a in (action.get("actions") or []))
    for a in (action.get("actions") or []):
        kind = a.get("kind")
        if kind == "open_app":
            target = a.get("app") or ""
            # app search based tool pehle (kisi bhi naam se), warna registry wala open_app
            before = len(records)
            if "open_any_app" in by_name:
                await call("open_any_app", {"query": target})
            else:
                await call("open_app", {"app_title": target})
            app_open_ok = len(records) > before and bool(records[-1].get("ok"))
        elif kind == "type_text":
            text = str(a.get("text") or "").strip()
            if not text:
                message = ("Type step skipped: request me likhne ke liye koi text nahi mila "
                           "(sirf connector words the). Main andaza laga kar kuch type nahi karunga.")
                emit("warn", message, tool="type_text", status="skipped")
                records.append({"name": "type_text", "args": {"text": ""}, "ok": False,
                                "output": message, "source": "needs_clarification"})
                continue
            if has_open_step and app_open_ok is not True:
                message = "Type step skipped: requested app did not return a verified open/focus result."
                emit("warn", message, tool="type_text", status="skipped")
                records.append({"name": "type_text", "args": {"text": text},
                                "ok": False, "output": message, "source": "precondition_block"})
                continue
            await call("type_text", {"text": text, "press_enter": bool(a.get("press_enter"))})
        elif kind == "hotkey":
            await call("press_hotkey", {"keys": a.get("keys", "")})
        elif kind == "screenshot":
            await call("take_screenshot", {})
        elif kind == "volume":
            await call("control_volume", {"action": a.get("action", "up")})
        elif kind == "close_app":
            await call("close_app", {"window_title": a.get("app", "")})
        else:
            # PEHLE: unknown kind chup-chaap skip ho jata tha → calls khali → "kuch hua hi nahi"
            # ka confusion aur success claim ka rasta. Ab explicit failure record banta hai.
            records.append({"name": f"unsupported_action:{kind}", "args": dict(a), "ok": False,
                            "output": f"(action kind '{kind}' is version me supported nahi hai — "
                                      f"kaam nahi hua)", "source": "deterministic"})

    # invariant: har action ka ek record hona chahiye — warna "kuch hua nahi" ka pata nahi chalega
    expected = len(action.get("actions") or [])
    if len(records) != expected:
        records.append({"name": "action_record_invariant", "args": {}, "ok": False,
                        "output": f"(records={len(records)} expected={expected} — execution tracking "
                                  f"adhoora hai, isliye fail maana jaye)", "source": "deterministic"})
    return records

_ACTION_TOOL_NAMES: dict[str, tuple[str, ...]] = {
    "open_app": ("open_any_app", "open_app"),
    "type_text": ("type_text",),
    "hotkey": ("press_hotkey",),
    "screenshot": ("take_screenshot",),
    "volume": ("control_volume",),
    "close_app": ("close_app",),
}

def _needs_action(request: str) -> bool:
    """Request me asli action maanga gaya hai? (sirf sawaal ya tutorial nahi)."""
    low = (request or "").lower()
    if not any(k in low for k in _ACTION_KEYS):
        return False
    if _is_instructional_question(low):
        return False
    # Other explanatory questions should not be treated as commands merely because they mention
    # an action verb. "Can you open X?" is deliberately not in this instructional pattern.
    if re.search(r"\b(?:what is|kya hai|batao|samjhao|explain|tutorial|steps)\b", low) \
            and not re.search(r"\b(?:please|for me|mere liye|kar do|karo)\b", low):
        return False
    return True
