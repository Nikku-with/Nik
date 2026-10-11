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
import re
import time
from typing import Any

from ..config import settings as S
from .store import MemoryStore, MemoryHit, get_memory

KINDS = ("fact", "preference", "profile", "episode", "task", "note", "setting")

# do memory ko duplicate maanne ka threshold (0..1)
DUP_THRESHOLD = 0.86

_TRIGGERS = {
    "fact": ("yaad rakh", "remember", "note kar le", "note kar lo", "yaad rakhna", "dhyaan rakh"),
    "preference": ("mujhe", "pasand", "prefer", "chahiye", "preference", "style"),
    "profile": ("mera naam", "meri age", "main rehta", "mera kaam", "mai hu", "main hu", "my name"),
    "setting": ("setting", "default", "config"),
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _similar(a: str, b: str) -> float:
    try:
        from rapidfuzz import fuzz  # type: ignore

        return float(fuzz.token_set_ratio(a, b)) / 100.0
    except Exception:
        return difflib.SequenceMatcher(None, a, b).ratio()


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
    def remember(self, text: str, kind: str = "", key: str = "", importance: float = 0.7) -> dict[str, Any]:
        """Memory save karo — duplicate ho to update, warna insert."""
        text = (text or "").strip(" ,।:-")
        if not text:
            return {"ok": False, "action": "none", "message": "khali memory save nahi hoti"}
        kind = classify(text, kind)
        key = key or self._guess_key(text)

        dup = self._find_duplicate(text)
        if dup:
            if _norm(dup["text"]) == _norm(text):
                return {"ok": True, "action": "duplicate", "id": dup["id"], "kind": dup.get("kind", kind),
                        "message": f"ye memory pehle se hai (id {dup['id'][:8]}) — dobara save nahi kiya"}
            self.update(dup["id"], text)
            return {"ok": True, "action": "updated", "id": dup["id"], "kind": kind,
                    "message": f"pehle wali milti-julti memory update kar di (id {dup['id'][:8]})"}

        ident = self.store.remember_fact(text, key=key, tags=f"{kind},{self._tags(key)}", importance=importance)
        return {"ok": True, "action": "created", "id": ident, "kind": kind,
                "message": f"yaad rakh liya ({kind}): {text[:80]}"}

    def remember_preference(self, text: str, key: str = "") -> dict[str, Any]:
        return self.remember(text, kind="preference", key=key or "preference")

    def _tags(self, key: str) -> str:
        return "owner" if key else "general"

    @staticmethod
    def _guess_key(text: str) -> str:
        low = _norm(text)
        for key, pats in (
            ("name", (r"naam", r"name")),
            ("model", (r"\bmodel\b",)),
            ("provider", (r"provider", r"openrouter", r"gemini", r"groq")),
            ("language", (r"bhasha", r"language", r"hindi", r"english")),
            ("style", (r"style", r"concise", r"detailed", r"short")),
            ("workspace", (r"workspace", r"folder")),
        ):
            if any(re.search(p, low) for p in pats):
                return key
        return ""

    # ------------------------------------------------------------------- read
    def _all_facts(self, limit: int = 500) -> list[dict[str, Any]]:
        try:
            return self.store.facts(limit=limit)
        except Exception:
            return []

    def _find_duplicate(self, text: str) -> dict[str, Any] | None:
        target = _norm(text)
        best: tuple[float, dict[str, Any]] | None = None
        for row in self._all_facts():
            val = row.get("value") or ""
            score = _similar(target, _norm(val))
            if score >= DUP_THRESHOLD and (best is None or score > best[0]):
                best = (score, {**row, "id": row.get("id") or row.get("key", ""), "text": val})
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
                        "updated": row.get("updated_at", 0)})
        out.sort(key=lambda r: -(r.get("updated") or r.get("created") or 0))
        return out[:limit]

    # ------------------------------------------------------------------ mutate
    def update(self, memory_id: str, new_text: str, kind: str = "") -> dict[str, Any]:
        """Memory ka text badlo (id ke hisaab se). Real UPDATE + re-index."""
        new_text = (new_text or "").strip()
        if not new_text:
            return {"ok": False, "message": "naya text khali hai"}
        with self.store._lock, self.store._connect() as c:  # noqa: SLF001
            row = c.execute("SELECT id, value FROM facts WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                return {"ok": False, "message": f"memory id {memory_id[:8]} nahi mili"}
            c.execute("UPDATE facts SET value = ?, updated_at = ? WHERE id = ?",
                      (new_text, time.time(), memory_id))
            self.store._index(c, memory_id, "fact", new_text)  # noqa: SLF001
        return {"ok": True, "action": "updated", "id": memory_id,
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
        if re.fullmatch(r"[0-9a-f]{8,32}", q.lower()):
            return self._delete_ids([q], reason="id se delete")

        targets: list[dict[str, Any]] = []
        rows = self._all_facts()
        qn = _norm(q)
        for row in rows:
            val = _norm(row.get("value", ""))
            tags = (row.get("tags") or "").lower()
            row_kind = next((k for k in KINDS if k in tags), "fact")
            if kind and row_kind != kind.lower():
                continue
            if qn and (qn in val or _similar(qn, val) >= 0.6):
                targets.append({"id": row.get("id", ""), "text": row.get("value", ""), "kind": row_kind})

        if not targets:
            return {"ok": False, "deleted": 0,
                    "message": f"'{q}' jaisi koi memory nahi mili — kuch delete nahi hua 🧐"}

        targets.sort(key=lambda t: -_similar(qn, _norm(t["text"])))
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
                if self.store._fts:
                    try:
                        c.execute("DELETE FROM search_index WHERE ref_id = ?", (mem_id,))
                    except Exception:
                        pass
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


_MANAGER: MemoryManager | None = None


def get_manager() -> MemoryManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = MemoryManager()
    return _MANAGER


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
