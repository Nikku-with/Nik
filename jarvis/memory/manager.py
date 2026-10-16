"""
Memory manager — remember / recall / update / forget
====================================================

`store.MemoryStore` low-level SQLite hai. Ye layer upar baithti hai aur owner ki natural-language
baatein handle karti hai:

    "remember mera naam Nikhil hai"          → FACT save (duplicate ho to skip/update)
    "mujhe concise jawab pasand hain"        → PREFERENCE
    "forget meri purani preference"          → asli DELETE (FTS + row dono se)
    "update karo — mera model OpenRouter"    → purani memory replace
    "show my memories"                       → list, kind-wise

Kinds: fact | preference | profile | episode | task | note | setting
Sirf useful/explicit cheezein store hoti hain — har message save nahi hota.
"""
from __future__ import annotations

import difflib
import logging
import re
import time
from typing import Any

from ..config import settings as S
from .store import MemoryHit, MemoryStore, get_memory

_LOG = logging.getLogger(__name__)

KINDS = ("fact", "preference", "profile", "episode", "task", "note", "setting")

# do memory ko duplicate maanne ka threshold (0..1)
DUP_THRESHOLD = 0.86
_SENSITIVE_RX = re.compile(
    r"\b(?:password|passcode|passphrase|secret|aadhaar|aadhar|pan card|bank account|account number|"
    r"credit card|debit card|card number|card no|cvv|cvc|otp|one time password|"
    r"api key|api token|access token|private key|secret key|auth token|bearer token|"
    r"salary|income|diagnosis|medical|medicine|medication|allerg(?:y|ic)|religion|political affiliation|"
    r"sexual orientation|precise address|home address|house address|mera address|mere address|my address|"
    r"passport|driving licen[cs]e|social security|ssn|health condition|mental health)\b", re.I
)
# A 13–19 digit run (with or without spaces/dashes) is a card or account number whatever
# the surrounding words are — "mera card number 4111 1111 1111 1111 hai" matched no keyword.
_LONG_NUMBER_RX = re.compile(r"(?:\d[ \-]?){13,19}")


def is_sensitive_fact(text: str) -> bool:
    body = text or ""
    return bool(_SENSITIVE_RX.search(body) or _LONG_NUMBER_RX.search(body))


_TRIGGERS = {
    "fact": ("yaad rakh", "remember", "note kar le", "note kar lo", "yaad rakhna", "dhyaan rakh"),
    "preference": ("mujhe", "pasand", "prefer", "chahiye", "preference", "style"),
    "profile": ("mera naam", "meri age", "main rehta", "mera kaam", "mai hu", "main hu", "my name"),
    "setting": ("setting", "default", "config"),
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _similar(a: str, b: str) -> float:
    """Order-sensitive similarity; token-set subset matches are too permissive for writes."""
    try:
        from rapidfuzz import fuzz  # type: ignore

        return float(fuzz.ratio(a, b)) / 100.0
    except Exception:
        return difflib.SequenceMatcher(None, a, b).ratio()


_FORGET_PREFIX_RX = re.compile(
    r"^(?:(?:please|can\s+you|could\s+you|jarvis|nikhilai|bhai|boss)[,\s]+)*"
    r"(?:forget(?:\s+about)?|delete|remove|bhool(?:\s+(?:jao|do|dena|lena))?|"
    r"bhul(?:\s+(?:jao|do|dena|lena))?|hata\s+(?:do|de|dijiye))\b[\s,.:;!?-]*",
    re.IGNORECASE,
)
_FORGET_SUFFIX_RX = re.compile(
    r"[\s,.:;!?-]*(?:bhool(?:\s+(?:jao|do|dena|lena))?|bhul(?:\s+(?:jao|do|dena|lena))?|"
    r"hata\s+(?:do|de|dijiye)|delete\s+it|remove\s+it)$",
    re.IGNORECASE,
)
_FORGET_NOISE = frozenset({
    "please", "can", "you", "could", "jarvis", "nikhilai", "bhai", "boss",
    "forget", "about", "delete", "remove", "bhool", "bhul", "jao", "do", "dena", "lena",
    "hata", "de", "dijiye", "my", "the", "a", "an", "mera", "meri", "mere",
    "purana", "purani", "purane", "old", "previous", "memory", "memories", "yaad",
    "baat", "fact", "preference", "preferences", "record", "entry", "item",
    "wali", "wala", "waali", "waala", "for", "this", "that",
    "ka", "ki", "ke", "ko", "se", "mein", "me",
})


def _forget_description(query: str) -> str:
    """Reduce a delete instruction to its identifying content, not its command words."""
    text = _FORGET_PREFIX_RX.sub("", str(query or "").strip())
    text = _FORGET_SUFFIX_RX.sub("", text)
    words = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    return " ".join(word for word in words if word not in _FORGET_NOISE)


def classify(text: str, hint: str = "") -> str:
    """Text se memory ka kind guess karo."""
    low = _norm(text)
    if hint and hint.lower() in KINDS:
        return hint.lower()
    if re.search(r"\b(mera naam|my name|meri umar|meri age|i am|main hu|mai hu)\b", low):
        return "profile"
    if re.search(r"\b(pasand|prefer|chahiye|like|dislike|style|bhasha|language)\b", low):
        return "preference"
    if re.search(r"\b(setting|default|config|mode)\b", low):
        return "setting"
    return "fact"


class MemoryManager:
    """High-level memory ops (dedupe + real deletion ke saath)."""

    def __init__(self, store: MemoryStore | None = None) -> None:
        self.store = store or get_memory()

    # ------------------------------------------------------------------ write
    def remember(
        self, text: str, kind: str = "", key: str = "", importance: float = 0.7,
        confirmed: bool = False, source_turn: str = "", source_session: str = "",
    ) -> dict[str, Any]:
        """Save a fact, but stage sensitive values until the owner confirms explicitly."""
        text = (text or "").strip(" ,।:-")
        if not text:
            return {"ok": False, "action": "none", "message": "khali memory save nahi hoti"}
        kind = classify(text, kind)
        key = key or self._guess_key(text)
        sensitive = is_sensitive_fact(text)
        if sensitive and not confirmed:
            pending_id = self.store.stage_pending_fact(
                text, key=key, tags=f"{kind},{self._tags(key)}", source_turn=source_turn,
                source_session=source_session,
            )
            return {"ok": False, "action": "pending_confirmation", "id": pending_id,
                    "message": "Ye sensitive detail hai. Memory me save karne se pehle confirm karo: 'haan, save karo'."}

        dup = self._find_duplicate(text, kind=kind, key=key)
        if dup:
            if _norm(dup["text"]) == _norm(text):
                return {"ok": True, "action": "duplicate", "id": dup["id"], "kind": dup.get("kind", kind),
                        "message": f"ye memory pehle se hai (id {dup['id'][:8]}) — dobara nahi likhi"}
            changed = self.update(dup["id"], text, confirmed=confirmed)
            if not changed.get("ok"):
                return changed
            return {"ok": True, "action": "updated", "id": dup["id"], "kind": kind,
                    "message": f"pehle wali milti-julti memory update kar di (id {dup['id'][:8]})"}

        try:
            ident = self.store.remember_fact(
                text, key=key, tags=f"{kind},{self._tags(key)}", importance=importance,
                confidence=0.95 if confirmed else 0.85, source_turn=source_turn,
                source_session=source_session, sensitivity="sensitive" if sensitive else "normal",
                confirmed=confirmed or not sensitive,
            )
        except Exception as exc:
            # A full/locked database must not take the whole turn down: `run_agent` had no
            # guard here, so the owner got a traceback instead of an answer.
            _LOG.warning("memory write failed (%s); nothing was saved", exc)
            return {"ok": False, "action": "storage_error",
                    "message": f"memory save nahi ho paayi (storage error: {exc}). "
                               "Kuch bhi save nahi hua."}
        return {"ok": True, "action": "created", "id": ident, "kind": kind,
                "message": f"yaad rakh liya ({kind}): {text[:80]}"}

    def pending(self) -> list[dict[str, Any]]:
        return self.store.pending_facts(limit=50)

    def confirm_pending(self, confirmed: bool = True, pending_id: str = "") -> dict[str, Any]:
        if pending_id:
            return self.store.resolve_pending_fact(str(pending_id), bool(confirmed))
        pending = self.pending()
        if len(pending) == 1:
            return self.store.resolve_pending_fact(str(pending[0]["id"]), bool(confirmed))
        if len(pending) > 1:
            return {"ok": False, "action": "needs_user",
                    "message": "Multiple sensitive memories are pending; provide the exact pending ID. Nothing was changed."}
        return {"ok": False, "message": "confirm karne ke liye koi pending sensitive memory nahi."}

    def remember_preference(self, text: str, key: str = "") -> dict[str, Any]:
        return self.remember(text, kind="preference", key=key or "preference")

    def _tags(self, key: str) -> str:
        normalized = str(key or "").casefold()
        if normalized.startswith(("other_", "other:")):
            return "other"
        return "owner" if key else "general"

    @staticmethod
    def _guess_key(text: str) -> str:
        low = _norm(text)
        if re.search(r"\b(?:my\s+friend(?:'s)?|mere\s+(?:dost|friend)|mera\s+(?:dost|friend))\b.*\b(?:name|naam)\b", low):
            return "other_friend_name"
        if re.search(r"\b(?:my name|mera naam)\b", low):
            return "owner_name"
        for key, pats in (
            ("model", (r"\bmodel\b",)),
            ("provider", (r"provider", r"openrouter", r"gemini", r"groq")),
            ("language", (r"bhasha", r"language", r"hindi", r"english")),
            ("style", (r"style", r"concise", r"detailed", r"short")),
            ("workspace", (r"workspace", r"folder")),
            ("location", (r"location", r"live in", r"from", r"city", r"address")),
            ("project", (r"project",)),
            ("workplace", (r"work at", r"employer", r"company")),
            ("response_style", (r"reply", r"answer", r"concise", r"detailed", r"response")),
        ):
            if any(re.search(p, low) for p in pats):
                return key
        if re.search(r"\b(?:name|naam)\b", low):
            return "other_name"
        return ""

    # ------------------------------------------------------------------- read
    def _all_facts(self, limit: int = 500) -> list[dict[str, Any]]:
        try:
            return self.store.facts(limit=limit)
        except Exception:
            return []

    def _find_duplicate(self, text: str, kind: str, key: str) -> dict[str, Any] | None:
        target = _norm(text)
        target_key = _norm(key)
        target_words = re.findall(r"\w+", target, flags=re.UNICODE)
        best: tuple[float, dict[str, Any]] | None = None
        for row in self._all_facts():
            val = str(row.get("value") or "")
            row_tags = {item.strip().casefold() for item in str(row.get("tags") or "").split(",") if item.strip()}
            row_kind = next((candidate for candidate in KINDS if candidate in row_tags), classify(val))
            row_key = _norm(str(row.get("key") or ""))
            if row_kind != kind or (target_key and row_key and row_key != target_key):
                continue

            normalized_value = _norm(val)
            if target == normalized_value:
                score = 1.0
            else:
                candidate_words = re.findall(r"\w+", normalized_value, flags=re.UNICODE)
                # Short fragments and one-word subsets should never rewrite a longer fact.
                if min(len(target), len(normalized_value)) < 16 or min(len(target_words), len(candidate_words)) < 3:
                    continue
                score = _similar(target, normalized_value)
            if score >= DUP_THRESHOLD and (best is None or score > best[0]):
                best = (score, {**row, "id": row.get("id") or row.get("key", ""), "text": val,
                                "kind": row_kind})
        return best[1] if best else None

    def recall(self, query: str, limit: int = 6) -> list[MemoryHit]:
        return self.store.search(query, limit=limit)

    def list_memories(self, kind: str = "", limit: int = 40) -> list[dict[str, Any]]:
        rows = self._all_facts(limit=400)
        out: list[dict[str, Any]] = []
        for row in rows:
            tags = (row.get("tags") or "").lower()
            row_kind = next((k for k in KINDS if k in tags), "fact")
            if kind and row_kind != kind.lower():
                continue
            out.append({"id": row.get("id", ""), "kind": row_kind, "text": row.get("value", ""),
                        "key": row.get("key", ""), "created": row.get("created_at", 0),
                        "updated": row.get("updated_at", 0), "confidence": row.get("confidence", 0.75),
                        "source_turn": row.get("source_turn", ""), "source_session": row.get("source_session", ""),
                        "importance": row.get("importance", 0.6), "sensitivity": row.get("sensitivity", "normal"),
                        "confirmed": bool(row.get("confirmed", 1))})
        out.sort(key=lambda r: -(r.get("updated") or r.get("created") or 0))
        return out[:limit]

    # ------------------------------------------------------------------ mutate
    def update(self, memory_id: str, new_text: str, kind: str = "", confirmed: bool = False) -> dict[str, Any]:
        """Update a fact by full/unambiguous prefix ID; sensitive replacements need consent."""
        memory_id = (memory_id or "").strip()
        new_text = (new_text or "").strip()
        if not memory_id:
            return {"ok": False, "message": "memory ID zaroori hai"}
        if not new_text:
            return {"ok": False, "message": "naya text khali hai"}
        with self.store._lock, self.store._connect() as c:  # noqa: SLF001
            row = c.execute("SELECT id FROM facts WHERE id = ?", (memory_id,)).fetchone()
            if row is None and len(memory_id) >= 8:
                rows = c.execute("SELECT id FROM facts WHERE id LIKE ? LIMIT 2", (memory_id + "%",)).fetchall()
                if len(rows) > 1:
                    return {"ok": False, "message": "ID prefix ambiguous hai; full ID use karo."}
                row = rows[0] if rows else None
            if row is None:
                return {"ok": False, "message": f"memory id {memory_id[:8]} nahi mili"}
            ident = str(row["id"])

        sensitive = is_sensitive_fact(new_text)
        if sensitive and not confirmed:
            key = self._guess_key(new_text)
            pending_id = self.store.stage_pending_fact(
                new_text, key=key, tags=f"{classify(new_text)},{self._tags(key)}", replace_id=ident,
            )
            return {"ok": False, "action": "pending_confirmation", "id": pending_id,
                    "message": "Nayi value sensitive hai. Replace karne se pehle owner confirmation chahiye."}

        with self.store._lock, self.store._connect() as c:  # noqa: SLF001
            row = c.execute("SELECT id FROM facts WHERE id = ?", (ident,)).fetchone()
            if row is None:
                return {"ok": False, "message": "memory update nahi hui; original fact ab maujood nahi."}
            now = time.time()
            c.execute("UPDATE facts SET value=?, updated_at=?, confidence=0.95, sensitivity=?, confirmed=1 WHERE id=?",
                      (new_text, now, "sensitive" if sensitive else "normal", ident))
            self.store._index(c, ident, "fact", new_text)  # noqa: SLF001
        return {"ok": True, "action": "updated", "id": ident,
                "message": f"memory update ho gayi: {new_text[:80]}"}

    def update_by_query(self, query: str, new_text: str) -> dict[str, Any]:
        """'update karo mera model' jaisi request — best matching memory dhoondh ke replace."""
        hit = self.find_one(query)
        if not hit:
            return self.remember(new_text, key=self._guess_key(new_text))
        return self.update(hit["id"], new_text)

    def find_one(self, query: str) -> dict[str, Any] | None:
        """Query se sabse relevant memory (exact substring pehle, phir fuzzy)."""
        q = _norm(query)
        rows = self._all_facts()
        if not q:
            return None
        for row in rows:
            if q and q in _norm(row.get("value", "")):
                return {"id": row.get("id", ""), "text": row.get("value", "")}
        best: tuple[float, dict[str, Any]] | None = None
        for row in rows:
            score = _similar(q, _norm(row.get("value", "")))
            if best is None or score > best[0]:
                best = (score, {"id": row.get("id", ""), "text": row.get("value", "")})
        if best and best[0] >= 0.55:
            return best[1]
        return None

    def forget(self, query: str, kind: str = "", all_matches: bool = False) -> dict[str, Any]:
        """Asli deletion — FTS index + facts table dono se hatao.

        query: text/jugad ("mera model", "purani preference"), ya direct memory id.
        """
        q = (query or "").strip()
        if not q:
            return {"ok": False, "message": "kya bhoolna hai wo batao"}

        # direct id?
        if re.fullmatch(r"[0-9a-f]{8,32}|[0-9a-f]{8}-[0-9a-f-]{27}", q.lower()):
            with self.store._lock, self.store._connect() as c:  # noqa: SLF001
                rows = c.execute("SELECT id, value FROM facts WHERE id LIKE ? LIMIT 2", (q + "%",)).fetchall()
            if not rows:
                return {"ok": False, "deleted": 0, "message": f"memory id {q[:8]} nahi mili."}
            if len(rows) > 1:
                return {"ok": False, "deleted": 0, "message": "ID prefix ambiguous hai; full ID use karo."}
            return self._delete_ids([str(rows[0]["id"])], reason="id se delete", preview=[str(rows[0]["value"])])

        targets: list[dict[str, Any]] = []
        rows = self._all_facts()
        qn = _forget_description(q)
        if not qn:
            return {"ok": False, "deleted": 0,
                    "message": "Memory ka naam/description nahi mila — kuch delete nahi hua 🧐"}
        query_terms = set(qn.split())
        for row in rows:
            val = _norm(row.get("value", ""))
            tags = (row.get("tags") or "").lower()
            row_kind = next((k for k in KINDS if k in tags), "fact")
            if kind and row_kind != kind.lower():
                continue
            value_terms = set(val.split())
            key_text = _norm(str(row.get("key") or ""))
            key_terms = set(key_text.split())
            overlap = len(query_terms & value_terms) / len(query_terms)
            key_overlap = len(query_terms & key_terms) / len(query_terms)
            score = max(_similar(qn, val), overlap, key_overlap)
            if qn in val or qn == key_text or score >= 0.6:
                targets.append({"id": row.get("id", ""), "text": row.get("value", ""),
                                "kind": row_kind, "score": score})

        if not targets:
            return {"ok": False, "deleted": 0,
                    "message": f"'{q}' jaisi koi memory nahi mili — kuch delete nahi hua 🧐"}

        targets.sort(key=lambda t: -t["score"])
        chosen = targets if all_matches else targets[:1]
        return self._delete_ids([t["id"] for t in chosen], reason=f"query: {q}",
                                preview=[t["text"] for t in chosen], extra=len(targets) - len(chosen))

    def _delete_ids(self, ids: list[str], reason: str = "", preview: list[str] | None = None,
                    extra: int = 0) -> dict[str, Any]:
        deleted = 0
        with self.store._lock, self.store._connect() as c:  # noqa: SLF001
            for mem_id in ids:
                cur = c.execute("DELETE FROM facts WHERE id = ?", (mem_id,))
                deleted += cur.rowcount or 0
                self.store._delete_index(c, mem_id)
        msg = f"{deleted} memory delete kar di ({reason})"
        if preview:
            msg += ": " + "; ".join(p[:60] for p in preview)
        if extra:
            msg += f" (aur {extra} similar memory bachi hai — 'all' bolo to wo bhi hata dun)"
        return {"ok": deleted > 0, "deleted": deleted, "message": msg, "ids": ids}

    def forget_all(self, kind: str = "") -> dict[str, Any]:
        """Sab memories (ya ek kind ki saari) delete — confirm ke baad hi call karo."""
        rows = self._all_facts()
        ids = []
        for row in rows:
            tags = (row.get("tags") or "").lower()
            row_kind = next((k for k in KINDS if k in tags), "fact")
            if kind and row_kind != kind.lower():
                continue
            ids.append(row.get("id", ""))
        if not ids:
            return {"ok": False, "deleted": 0, "message": "delete karne layak kuch nahi mila"}
        return self._delete_ids([i for i in ids if i], reason=f"sab {kind or 'memories'}")

    # ------------------------------------------------------------------ digest
    def summarize(self) -> str:
        """Memory ka compact digest — prompt me daalne ke liye."""
        rows = self.list_memories(limit=60)
        if not rows:
            return "(memory khali hai)"
        by_kind: dict[str, list[str]] = {}
        for row in rows:
            by_kind.setdefault(row["kind"], []).append(row["text"])
        lines: list[str] = []
        for kind in KINDS:
            items = by_kind.get(kind) or []
            if not items:
                continue
            lines.append(f"{kind.upper()} ({len(items)}):")
            lines += [f"  - {t[:120]}" for t in items[:8]]
        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        rows = self.list_memories(limit=1000)
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
        return {"total": len(rows), "by_kind": counts}


_MANAGERS: dict[str, MemoryManager] = {}


def get_manager(user_id: str | None = None) -> MemoryManager:
    store = get_memory(user_id=user_id)
    uid = store.user_id
    if uid not in _MANAGERS:
        _MANAGERS[uid] = MemoryManager(store)
    return _MANAGERS[uid]


def is_memory_request(text: str) -> str:
    """Ye message memory op hai? → 'remember' | 'forget' | 'update' | 'list' | ''"""
    low = _norm(text)
    if re.search(r"\b(forget|bhool|delete.*memory|memory.*delete|hata do.*memory)\b", low):
        return "forget"
    if re.search(r"\b(update|badal|change|replace|correct)\b.*\b(memory|yaad|preference|model|naam)\b", low):
        return "update"
    if re.search(r"\b(show|dikhao|list|batao)\b.*\b(memories|memory|yaad)\b", low) or "my memories" in low:
        return "list"
    if any(t in low for t in _TRIGGERS["fact"]) or re.search(r"\bremember\b", low):
        return "remember"
    return ""


def strip_trigger(text: str) -> str:
    """'yaad rakh mera naam Nikhil hai' → 'mera naam Nikhil hai'"""
    out = re.sub(r"^(please\s+)?(jarvis|nikhilai|bhai|boss)?[, ]*", "", (text or "").strip(), flags=re.I)
    out = re.sub(r"^(yaad\s+rakh\w*|remember\s*(karo|kar|this|that)?|note\s+kar\s*le?\w*|dhyaan\s+rakh\w*)\s*[:,-]?\s*",
                 "", out, flags=re.I)
    return out.strip(" ,।:-")


__all__ = [
    "KINDS", "MemoryManager", "get_manager", "get_memory", "memory_enabled",
    "is_memory_request", "strip_trigger", "classify",
]


def memory_enabled() -> bool:
    from ..config import runtime as R

    return bool(R.get("memory_enabled", S.MEMORY_ENABLED))
