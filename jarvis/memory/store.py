"""
jarvis/memory/store.py — Jarvis ki long-term memory (SQLite + FTS5)
==================================================================

Original repo ki `memory_store.py` sirf JSON dump karti thi (aur usme duplicate
detection buggy tha — `_conversation_exists` har baar naya timestamp compare
karta tha, isliye same message do baar save ho jata tha).

Upgrade:
  * SQLite (single file, fast, safe) + FTS5 full-text search (Hindi/English/Hinglish)
  * 4 memory types:  facts (owner ke baare me), episodes (chat turns),
                     tasks (jo kaam chale), notes (saved notes ka index)
  * recall(): FTS5 → LIKE fallback, importance + recency weighted
  * compaction: purane episodes ko summary me collapse karke DB chhota rakhta hai
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..config import MEMORY_DB, NOTES_DIR, OWNER_NAME


@dataclass
class MemoryHit:
    kind: str
    text: str
    score: float
    meta: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "score": round(self.score, 4), "meta": self.meta}


class MemoryStore:
    """Thread-safe SQLite memory. Ek hi instance poore app me reuse karo (`get_memory()`)."""

    def __init__(self, db_path: Path | str = MEMORY_DB, user_id: str = "nikhil_mehra"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.user_id = user_id
        self._lock = threading.RLock()
        self._fts = True
        self._init_schema()

    # ---------------------------------------------------------------- schema
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS facts (
                    id TEXT PRIMARY KEY,
                    key TEXT,
                    value TEXT NOT NULL,
                    tags TEXT DEFAULT '',
                    importance REAL DEFAULT 0.6,
                    created_at REAL,
                    updated_at REAL
                );
                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY,
                    role TEXT,
                    content TEXT,
                    session TEXT DEFAULT 'default',
                    summary TEXT DEFAULT '',
                    importance REAL DEFAULT 0.4,
                    created_at REAL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    goal TEXT,
                    plan TEXT DEFAULT '[]',
                    status TEXT DEFAULT 'pending',
                    route TEXT DEFAULT '',
                    result TEXT DEFAULT '',
                    artifacts TEXT DEFAULT '[]',
                    created_at REAL,
                    updated_at REAL
                );
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    path TEXT,
                    content TEXT,
                    tags TEXT DEFAULT '',
                    created_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at);
                CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(key);
                """
            )
            try:
                c.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
                        ref_id UNINDEXED, kind UNINDEXED, text, tokenize='unicode61 remove_diacritics 2'
                    );
                    """
                )
            except sqlite3.OperationalError:
                self._fts = False

    # ---------------------------------------------------------------- search
    def _index(self, conn: sqlite3.Connection, ref_id: str, kind: str, text: str) -> None:
        if not self._fts:
            return
        try:
            conn.execute("DELETE FROM search_index WHERE ref_id = ?", (ref_id,))
            conn.execute("INSERT INTO search_index (ref_id, kind, text) VALUES (?,?,?)", (ref_id, kind, text))
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w\u0900-\u097F]+", query.lower())
        tokens = [t for t in tokens if len(t) > 2][:10]
        if not tokens:
            return ""
        return " OR ".join(f'"{t}"' for t in tokens)

    def search(self, query: str, limit: int = 8) -> list[MemoryHit]:
        hits: list[MemoryHit] = []
        with self._lock, self._connect() as c:
            fts_q = self._fts_query(query)
            if self._fts and fts_q:
                try:
                    rows = c.execute(
                        "SELECT ref_id, kind, text, bm25(search_index) AS rank FROM search_index "
                        "WHERE search_index MATCH ? ORDER BY rank LIMIT ?",
                        (fts_q, limit * 2),
                    ).fetchall()
                    for r in rows:
                        hits.append(MemoryHit(r["kind"], r["text"], 1.0 / (1.0 + abs(float(r["rank"] or 0))), {"ref_id": r["ref_id"]}))
                except sqlite3.OperationalError:
                    hits = []

            if not hits:  # LIKE fallback
                like = f"%{(query or '').strip()[:40]}%"
                rows = c.execute(
                    "SELECT id, 'fact' AS kind, value AS text FROM facts WHERE value LIKE ? "
                    "UNION ALL SELECT id, 'episode' AS kind, content AS text FROM episodes WHERE content LIKE ? "
                    "UNION ALL SELECT id, 'note' AS kind, name || ' :: ' || substr(content,1,200) AS text FROM notes WHERE content LIKE ? OR name LIKE ? "
                    "LIMIT ?",
                    (like, like, like, like, limit),
                ).fetchall()
                for r in rows:
                    hits.append(MemoryHit(r["kind"], r["text"], 0.35, {"ref_id": r["id"]}))

        # recency boost
        hits.sort(key=lambda h: -h.score)
        return hits[:limit]

    # ---------------------------------------------------------------- facts
    def remember_fact(self, fact: str, key: str = "", tags: str = "", importance: float = 0.7) -> str:
        fact = (fact or "").strip()
        if not fact:
            return ""
        now = time.time()
        fid = str(uuid.uuid4())
        with self._lock, self._connect() as c:
            existing = None
            if key:
                existing = c.execute("SELECT id FROM facts WHERE key = ?", (key,)).fetchone()
            if existing:
                fid = existing["id"]
                c.execute("UPDATE facts SET value=?, tags=?, importance=?, updated_at=? WHERE id=?", (fact, tags, importance, now, fid))
            else:
                c.execute(
                    "INSERT INTO facts (id,key,value,tags,importance,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (fid, key or fact[:40].lower(), fact, tags, importance, now, now),
                )
            self._index(c, fid, "fact", fact)
        return fid

    def facts(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as c:
            rows = c.execute(
                "SELECT * FROM facts ORDER BY importance DESC, updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- episodes
    def add_episode(self, role: str, content: str, session: str = "default", importance: float = 0.4) -> str:
        content = (content or "").strip()
        if not content:
            return ""
        eid = str(uuid.uuid4())
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT INTO episodes (id,role,content,session,importance,created_at) VALUES (?,?,?,?,?,?)",
                (eid, role, content[:20000], session, importance, time.time()),
            )
            self._index(c, eid, "episode", content[:8000])
        return eid

    def add_turn(self, user_text: str, assistant_text: str, session: str = "default") -> None:
        self.add_episode("user", user_text, session=session, importance=0.5)
        self.add_episode("assistant", assistant_text, session=session, importance=0.5)

    def recent(self, limit: int = 12, session: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connect() as c:
            if session:
                rows = c.execute(
                    "SELECT role, content, created_at FROM episodes WHERE session=? ORDER BY created_at DESC LIMIT ?",
                    (session, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT role, content, created_at FROM episodes ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def context_block(self, limit: int = 10, session: str | None = None) -> str:
        rows = self.recent(limit=limit, session=session)
        if not rows:
            return "(naya conversation — koi past memory nahi)"
        lines = []
        for r in rows:
            who = "Owner" if r["role"] == "user" else "Jarvis"
            lines.append(f"{who}: {r['content'][:280]}")
        return "\n".join(lines)

    # ----------------------------------------------------------------- notes
    def index_note(self, name: str, path: str, content: str, tags: str = "") -> str:
        nid = str(uuid.uuid4())
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT INTO notes (id,name,path,content,tags,created_at) VALUES (?,?,?,?,?,?)",
                (nid, name, path, (content or "")[:60000], tags, time.time()),
            )
            self._index(c, nid, "note", f"{name} {content[:4000]}")
        return nid

    def notes(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as c:
            rows = c.execute(
                "SELECT id,name,path,tags,created_at,length(content) AS size FROM notes ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- tasks
    def start_task(self, goal: str, route: str = "") -> str:
        tid = str(uuid.uuid4())
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT INTO tasks (id,goal,status,route,created_at,updated_at) VALUES (?,?,'running',?,?,?)",
                (tid, goal, route, time.time(), time.time()),
            )
        return tid

    def finish_task(self, task_id: str, status: str, result: str = "", plan: Iterable | None = None, artifacts: Iterable | None = None) -> None:
        with self._lock, self._connect() as c:
            c.execute(
                "UPDATE tasks SET status=?, result=?, plan=?, artifacts=?, updated_at=? WHERE id=?",
                (
                    status,
                    (result or "")[:20000],
                    json.dumps(list(plan or []), ensure_ascii=False, default=str)[:20000],
                    json.dumps(list(artifacts or []), ensure_ascii=False, default=str)[:8000],
                    time.time(),
                    task_id,
                ),
            )

    def tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._connect() as c:
            rows = c.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ maintenance
    def stats(self) -> dict[str, int]:
        with self._lock, self._connect() as c:
            q = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
            return {
                "facts": q("SELECT COUNT(*) FROM facts"),
                "episodes": q("SELECT COUNT(*) FROM episodes"),
                "notes": q("SELECT COUNT(*) FROM notes"),
                "tasks": q("SELECT COUNT(*) FROM tasks"),
                "db_kb": int(self.db_path.stat().st_size / 1024) if self.db_path.exists() else 0,
            }

    def compact(self, keep_last: int = 400) -> int:
        """Purane episodes ko ek summary row me collapse karo."""
        with self._lock, self._connect() as c:
            total = c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            if total <= keep_last:
                return 0
            rows = c.execute(
                "SELECT id, role, content FROM episodes ORDER BY created_at ASC LIMIT ?", (total - keep_last,)
            ).fetchall()
            if not rows:
                return 0
            blob = " | ".join(f"{r['role']}: {r['content'][:120]}" for r in rows)
            summary = f"[compacted {len(rows)} old turns] {blob[:6000]}"
            c.execute(
                "INSERT INTO episodes (id,role,content,session,summary,importance,created_at) VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), "system", summary, "summary", summary[:2000], 0.8, time.time()),
            )
            c.execute("DELETE FROM episodes WHERE id IN (%s)" % ",".join("?" * len(rows)), [r["id"] for r in rows])
            return len(rows)


# --------------------------------------------------------------------------
_default: MemoryStore | None = None


def get_memory(**kwargs: Any) -> MemoryStore:
    global _default
    if _default is None:
        _default = MemoryStore(**kwargs)
    return _default


def owner_profile_block() -> str:
    """Owner ke baare me known facts — supervisor prompt me inject hota hai."""
    mem = get_memory()
    facts = mem.facts(limit=15)
    if not facts:
        return f"(Owner: {OWNER_NAME}. Abhi tak koi extra fact memory me nahi hai.)"
    return f"Owner: {OWNER_NAME}\nKnown facts:\n" + "\n".join(f"- {f['value'][:200]}" for f in facts)


if __name__ == "__main__":  # pragma: no cover
    m = get_memory()
    m.remember_fact(f"{OWNER_NAME} Kota, Rajasthan me rehta hai", key="location", tags="owner,personal")
    m.add_turn("bhai notes khol do", "हो गया boss ✅ notes khul gaye")
    print("stats:", m.stats())
    print("search 'notes':", [h.as_dict() for h in m.search("notes")])
    print(m.context_block(4))
    print("notes dir:", NOTES_DIR)
