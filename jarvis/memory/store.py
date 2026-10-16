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

import contextvars
import hashlib
import json
import math
import queue
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import MEMORY_DB, NOTES_DIR, OWNER_NAME, WORKSPACE_DIR


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
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield one transactional connection and always close it on scope exit."""
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            with conn:
                yield conn
        finally:
            conn.close()

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
                    requirements TEXT DEFAULT '{}',
                    created_at REAL,
                    updated_at REAL
                );
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    path TEXT,
                    content TEXT,
                    tags TEXT DEFAULT '',
                    created_at REAL,
                    updated_at REAL,
                    importance REAL DEFAULT 0.6,
                    confidence REAL DEFAULT 0.75,
                    source_turn TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at);
                CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(key);
                """
            )
            # Additive migrations keep older workspace databases readable.
            for table, column, declaration in (
                ("facts", "confidence", "REAL DEFAULT 0.75"),
                ("facts", "source_turn", "TEXT DEFAULT ''"),
                ("facts", "source_session", "TEXT DEFAULT ''"),
                ("facts", "sensitivity", "TEXT DEFAULT 'normal'"),
                ("facts", "confirmed", "INTEGER DEFAULT 1"),
                ("episodes", "turn_id", "TEXT DEFAULT ''"),
                ("episodes", "metadata", "TEXT DEFAULT '{}'"),
                ("episodes", "confidence", "REAL DEFAULT 0.75"),
                ("notes", "updated_at", "REAL DEFAULT 0"),
                ("notes", "importance", "REAL DEFAULT 0.6"),
                ("notes", "confidence", "REAL DEFAULT 0.75"),
                ("notes", "source_turn", "TEXT DEFAULT ''"),
                ("tasks", "requirements", "TEXT DEFAULT '{}'"),
            ):
                columns = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in columns:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            c.executescript("""
                CREATE TABLE IF NOT EXISTS pending_facts (
                    id TEXT PRIMARY KEY, value TEXT NOT NULL, key TEXT DEFAULT '', tags TEXT DEFAULT '',
                    source_turn TEXT DEFAULT '', source_session TEXT DEFAULT '', replace_id TEXT DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pending_facts_created ON pending_facts(created_at);
            """)
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
            refs_table_exists = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_refs'"
            ).fetchone() is not None
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS search_refs (
                    ref_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    fts_rowid INTEGER NOT NULL,
                    PRIMARY KEY (ref_id, fts_rowid)
                ) WITHOUT ROWID
                """
            )
            if self._fts and not refs_table_exists:
                c.execute(
                    """
                    INSERT OR IGNORE INTO search_refs (ref_id, kind, fts_rowid)
                    SELECT ref_id, kind, rowid FROM search_index
                    """
                )

    # ---------------------------------------------------------------- search
    def _delete_index(self, conn: sqlite3.Connection, ref_id: str) -> None:
        """Delete one FTS row via the indexed reference map, never a content scan."""
        if not self._fts:
            return
        rows = conn.execute(
            "SELECT fts_rowid FROM search_refs WHERE ref_id = ?", (ref_id,)
        ).fetchall()
        for row in rows:
            conn.execute("DELETE FROM search_index WHERE rowid = ?", (row["fts_rowid"],))
        conn.execute("DELETE FROM search_refs WHERE ref_id = ?", (ref_id,))

    def _index(self, conn: sqlite3.Connection, ref_id: str, kind: str, text: str) -> None:
        if not self._fts:
            return
        self._delete_index(conn, ref_id)
        rowid: int | None = None
        try:
            cursor = conn.execute(
                "INSERT INTO search_index (ref_id, kind, text) VALUES (?,?,?)", (ref_id, kind, text)
            )
            rowid = cursor.lastrowid
            conn.execute(
                "INSERT INTO search_refs (ref_id, kind, fts_rowid) VALUES (?,?,?)",
                (ref_id, kind, rowid),
            )
        except sqlite3.OperationalError:
            if rowid is not None:
                conn.execute("DELETE FROM search_index WHERE rowid = ?", (rowid,))

    @staticmethod
    def _like_pattern(query: str) -> str:
        escaped = query[:48].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w\u0900-\u097F]+", query.lower())
        stop = {"what", "when", "where", "which", "who", "how", "the", "and", "for", "are", "was",
                "were", "with", "from", "that", "this", "have", "has", "had", "you", "your", "my",
                "our", "their", "its", "does", "did", "will", "would", "can", "could", "is", "am",
                "me", "to", "in", "of", "on", "aaj", "hai", "tha", "thee", "ka", "ki", "ke"}
        tokens = [t for t in tokens if len(t) > 2 and t not in stop][:10]
        if not tokens:
            return ""
        return " OR ".join(f'"{t}"' for t in tokens)

    def search(self, query: str, limit: int = 8) -> list[MemoryHit]:
        """Hybrid top-k recall: FTS candidates + lexical overlap + recency/importance/confidence."""
        query = (query or "").strip()
        qstop = {"what", "when", "where", "which", "who", "how", "the", "and", "for", "are", "was",
                 "were", "with", "from", "that", "this", "have", "has", "had", "you", "your", "my",
                 "our", "their", "its", "does", "did", "will", "would", "can", "could", "is", "am",
                 "me", "to", "in", "of", "on", "aaj", "hai", "tha", "thee", "ka", "ki", "ke"}
        qtokens = {token for token in re.findall(r"[\w\u0900-\u097F]+", query.lower())
                   if token not in qstop and len(token) > 2}
        if not query or not qtokens or limit <= 0:
            return []
        candidates: dict[tuple[str, str], dict[str, Any]] = {}
        with self._lock, self._connect() as c:
            fts_q = self._fts_query(query)
            if self._fts and fts_q:
                try:
                    rows = c.execute(
                        "SELECT ref_id, kind, text, bm25(search_index) AS rank FROM search_index "
                        "WHERE search_index MATCH ? ORDER BY rank LIMIT ?",
                        (fts_q, max(32, min(limit * 8, 160))),
                    ).fetchall()
                    for row in rows:
                        key = (str(row["kind"]), str(row["ref_id"]))
                        candidates[key] = {"kind": key[0], "id": key[1], "text": row["text"],
                                           "fts": 1.0 / (1.0 + abs(float(row["rank"] or 0))),
                                           "created_at": 0.0, "updated_at": 0.0,
                                           "importance": 0.5, "confidence": 0.65,
                                           "source_turn": "", "source_session": "", "sensitivity": "normal"}
                except sqlite3.OperationalError:
                    candidates.clear()

            # Index metadata and a LIKE fallback also support databases without FTS5.
            if candidates:
                ids_by_kind: dict[str, list[str]] = {}
                for kind, ident in candidates:
                    ids_by_kind.setdefault(kind, []).append(ident)
                for kind, ids in ids_by_kind.items():
                    table = {"fact": ("facts", "id"), "episode": ("episodes", "id"),
                             "note": ("notes", "id")}.get(kind)
                    if not table:
                        continue
                    name, key_col = table
                    placeholders = ",".join("?" for _ in ids)
                    try:
                        rows = c.execute(f"SELECT * FROM {name} WHERE {key_col} IN ({placeholders})", ids).fetchall()
                    except sqlite3.OperationalError:
                        continue
                    for row in rows:
                        data = dict(row)
                        ident = str(data.get("id", ""))
                        item = candidates.get((kind, ident))
                        if not item:
                            continue
                        item.update({
                            "created_at": float(data.get("created_at") or 0),
                            "updated_at": float(data.get("updated_at") or 0),
                            "importance": float(data.get("importance") or 0.5),
                            "confidence": float(data.get("confidence") or 0.65),
                            "source_turn": str(data.get("source_turn") or ""),
                            "source_session": str(data.get("source_session") or ""),
                            "sensitivity": str(data.get("sensitivity") or "normal"),
                        })

            if not candidates:
                # LIKE search is the graceful fallback for SQLite builds without FTS5.
                like = self._like_pattern(query)
                rows = c.execute(
                    "SELECT id, 'fact' AS kind, value AS text, created_at, updated_at, importance, confidence, "
                    "source_turn, source_session, sensitivity FROM facts WHERE value LIKE ? ESCAPE '\\' "
                    "UNION ALL SELECT id, 'episode' AS kind, content AS text, created_at, created_at, importance, "
                    "0.65 AS confidence, turn_id AS source_turn, session AS source_session, 'normal' AS sensitivity "
                    "FROM episodes WHERE content LIKE ? ESCAPE '\\' "
                    "UNION ALL SELECT id, 'note' AS kind, name || ' :: ' || substr(content,1,200) AS text, "
                    "created_at, created_at, 0.5 AS importance, 0.65 AS confidence, '' AS source_turn, "
                    "'' AS source_session, 'normal' AS sensitivity FROM notes "
                    "WHERE content LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' LIMIT ?",
                    (like, like, like, like, max(32, limit * 8)),
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    item["id"] = str(item.get("id", ""))
                    item["fts"] = 0.0
                    candidates[(str(item["kind"]), item["id"])] = item

        now = time.time()
        hits: list[MemoryHit] = []
        for item in candidates.values():
            text = str(item.get("text") or "")
            tokens = set(re.findall(r"[\w\u0900-\u097F]+", text.lower()))
            overlap = len(qtokens & tokens) / max(1, len(qtokens))
            phrase = 1.0 if query.casefold() in text.casefold() else 0.0
            fts = max(0.0, min(1.0, float(item.get("fts", 0.0))))
            stamp = float(item.get("updated_at") or item.get("created_at") or 0.0)
            recency = math.exp(-max(0.0, now - stamp) / (180 * 86400)) if stamp else 0.0
            importance = max(0.0, min(1.0, float(item.get("importance", 0.5) or 0.5)))
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.65) or 0.65)))
            score = (0.48 * overlap + 0.18 * phrase + 0.18 * fts + 0.08 * recency
                     + 0.05 * importance + 0.03 * confidence)
            if overlap == 0 and fts == 0 and phrase == 0:
                continue
            meta = {key: item.get(key, "") for key in
                    ("id", "source_turn", "source_session", "created_at", "updated_at", "sensitivity")}
            meta.update({"importance": importance, "confidence": confidence})
            hits.append(MemoryHit(str(item["kind"]), text, score, meta))
        hits.sort(key=lambda hit: (-hit.score, -float(hit.meta.get("importance", 0.0))))
        return hits[:limit]

    # ---------------------------------------------------------------- facts
    def remember_fact(
        self, fact: str, key: str = "", tags: str = "", importance: float = 0.7,
        confidence: float = 0.9, source_turn: str = "", source_session: str = "",
        sensitivity: str = "normal", confirmed: bool = True,
    ) -> str:
        """Save/update a profile fact with provenance and confidence; key updates resolve contradictions."""
        fact = (fact or "").strip()
        if not fact:
            return ""
        now = time.time()
        fid = str(uuid.uuid4())
        with self._lock, self._connect() as c:
            existing = c.execute("SELECT id, created_at FROM facts WHERE key = ?", (key,)).fetchone() if key else None
            if existing is None and key == "owner_name":
                existing = c.execute(
                    "SELECT id, created_at FROM facts WHERE key='name' AND tags LIKE '%profile%' "
                    "ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                if existing:
                    c.execute("UPDATE facts SET key=? WHERE id=?", (key, existing["id"]))
            if existing:
                fid = existing["id"]
                c.execute(
                    "UPDATE facts SET value=?, tags=?, importance=?, confidence=?, source_turn=?, source_session=?, "
                    "sensitivity=?, confirmed=?, updated_at=? WHERE id=?",
                    (fact, tags, max(0.0, min(1.0, importance)), max(0.0, min(1.0, confidence)),
                     source_turn, source_session, sensitivity, int(bool(confirmed)), now, fid),
                )
            else:
                c.execute(
                    "INSERT INTO facts (id,key,value,tags,importance,created_at,updated_at,confidence,source_turn,"
                    "source_session,sensitivity,confirmed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fid, key or fact[:40].lower(), fact, tags, max(0.0, min(1.0, importance)), now, now,
                     max(0.0, min(1.0, confidence)), source_turn, source_session, sensitivity, int(bool(confirmed))),
                )
            self._index(c, fid, "fact", fact)
        return fid

    def stage_pending_fact(
        self, fact: str, key: str = "", tags: str = "", source_turn: str = "",
        source_session: str = "", replace_id: str = "",
    ) -> str:
        fact = (fact or "").strip()
        if not fact:
            return ""
        ident = str(uuid.uuid4())
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM pending_facts WHERE created_at < ?", (time.time() - 300,))
            c.execute(
                "INSERT INTO pending_facts (id,value,key,tags,source_turn,source_session,replace_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ident, fact, key, tags, source_turn, source_session, replace_id, time.time()),
            )
        return ident

    def pending_facts(self, limit: int = 5) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM pending_facts WHERE created_at < ?", (now - 300,))
            rows = c.execute("SELECT * FROM pending_facts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def resolve_pending_fact(self, pending_id: str, confirmed: bool) -> dict[str, Any]:
        with self._lock, self._connect() as c:
            row = c.execute("SELECT * FROM pending_facts WHERE id = ?", (pending_id,)).fetchone()
            if row is None or float(row["created_at"] or 0) < time.time() - 300:
                c.execute("DELETE FROM pending_facts WHERE id = ?", (pending_id,))
                return {"ok": False, "message": "Sensitive memory confirmation expired or not found."}
            item = dict(row)
            c.execute("DELETE FROM pending_facts WHERE id = ?", (pending_id,))
            if not confirmed:
                return {"ok": True, "action": "cancelled", "id": pending_id,
                        "message": "Sensitive memory save cancel kiya; kuch store nahi hua."}
            replace_id = str(item.get("replace_id") or "")
            if replace_id:
                c.execute("DELETE FROM facts WHERE id = ?", (replace_id,))
                self._delete_index(c, replace_id)
            now = time.time()
            fid = str(uuid.uuid4())
            key = str(item.get("key") or "")
            existing = c.execute("SELECT id FROM facts WHERE key = ?", (key,)).fetchone() if key else None
            if existing:
                fid = existing["id"]
                c.execute("UPDATE facts SET value=?, tags=?, importance=0.8, confidence=0.95, source_turn=?, "
                          "source_session=?, sensitivity='sensitive', confirmed=1, updated_at=? WHERE id=?",
                          (item["value"], item["tags"], item["source_turn"], item["source_session"], now, fid))
            else:
                c.execute("INSERT INTO facts (id,key,value,tags,importance,created_at,updated_at,confidence,source_turn,"
                          "source_session,sensitivity,confirmed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (fid, key or item["value"][:40].lower(), item["value"], item["tags"], 0.8, now, now,
                           0.95, item["source_turn"], item["source_session"], "sensitive", 1))
            self._index(c, fid, "fact", str(item["value"]))
        return {"ok": True, "action": "confirmed", "id": fid,
                "message": f"Sensitive memory save user confirmation ke baad hua (id {fid[:8]})."}

    def facts(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as c:
            rows = c.execute(
                "SELECT * FROM facts ORDER BY importance DESC, updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- episodes
    def add_episode(
        self, role: str, content: str, session: str = "default", importance: float = 0.4,
        turn_id: str = "", metadata: dict[str, Any] | None = None,
    ) -> str:
        content = (content or "").strip()
        if not content:
            return ""
        eid = str(uuid.uuid4())
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT INTO episodes (id,role,content,session,importance,created_at,turn_id,metadata,confidence) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (eid, role, content[:20000], session, max(0.0, min(1.0, importance)), time.time(), turn_id,
                 json.dumps(metadata or {}, ensure_ascii=False, default=str), 0.85),
            )
            self._index(c, eid, "episode", content[:8000])
        return eid

    def add_turn(self, user_text: str, assistant_text: str, session: str = "default") -> str:
        turn_id = str(uuid.uuid4())
        self.add_episode("user", user_text, session=session, importance=0.5, turn_id=turn_id)
        self.add_episode("assistant", assistant_text, session=session, importance=0.5, turn_id=turn_id)
        _enqueue_extraction(self, user_text, turn_id, session)
        return turn_id

    def relevant_context(
        self, query: str, token_budget: int = 700, limit: int = 12,
        session: str | None = None, recent_limit: int = 6,
    ) -> str:
        """Return relevant facts/episodes/notes plus a short continuity tail under a token budget."""
        budget = max(1, int(token_budget))
        lines: list[str] = []
        seen: set[str] = set()
        hits = self.search(query, limit=max(limit, 1))
        for hit in hits:
            if hit.score < 0.17:
                continue
            ident = str(hit.meta.get("id") or "")
            signature = (ident or f"{hit.kind}:{hit.text[:80]}").lower()
            if signature in seen:
                continue
            seen.add(signature)
            line = f"[{hit.kind}; score={hit.score:.2f}] {hit.text[:420]}"
            if _token_count("\n".join(lines + [line])) <= budget:
                lines.append(line)

        if recent_limit > 0:
            for row in reversed(self.recent(limit=recent_limit, session=session)):
                content = str(row.get("content") or "").strip()
                signature = content[:100].casefold()
                if not content or signature in seen:
                    continue
                role = "Nikhil" if row.get("role") == "user" else "Jarvis"
                line = f"[recent {role}] {content[:260]}"
                if _token_count("\n".join(lines + [line])) <= budget:
                    lines.append(line)
                    seen.add(signature)
        return "\n".join(lines) if lines else "(no relevant saved memory)"

    def recent(self, limit: int = 12, session: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connect() as c:
            if session:
                rows = c.execute(
                    "SELECT role, content, created_at, turn_id, importance FROM episodes WHERE session=? ORDER BY created_at DESC LIMIT ?",
                    (session, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT role, content, created_at, turn_id, importance FROM episodes ORDER BY created_at DESC LIMIT ?", (limit,)
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
    def index_note(
        self, name: str, path: str, content: str, tags: str = "", source_turn: str = "",
        importance: float = 0.6, confidence: float = 0.8,
    ) -> str:
        nid = str(uuid.uuid4())
        now = time.time()
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT INTO notes (id,name,path,content,tags,created_at,updated_at,importance,confidence,source_turn) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (nid, name, path, (content or "")[:60000], tags, now, now,
                 max(0.0, min(1.0, importance)), max(0.0, min(1.0, confidence)), source_turn),
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

    def finish_task(
        self, task_id: str, status: str, result: str = "", plan: Iterable | None = None,
        artifacts: Iterable | None = None, requirements: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connect() as c:
            c.execute(
                "UPDATE tasks SET status=?, result=?, plan=?, artifacts=?, requirements=?, updated_at=? WHERE id=?",
                (
                    status,
                    (result or "")[:20000],
                    json.dumps(list(plan or []), ensure_ascii=False, default=str)[:20000],
                    json.dumps(list(artifacts or []), ensure_ascii=False, default=str)[:8000],
                    json.dumps(requirements or {}, ensure_ascii=False, default=str)[:30000],
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
        if isinstance(keep_last, bool) or not isinstance(keep_last, int) or keep_last < 1:
            raise ValueError("keep_last must be an integer greater than or equal to 1")
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
            placeholders = ",".join("?" * len(rows))
            c.execute(f"DELETE FROM episodes WHERE id IN ({placeholders})", [r["id"] for r in rows])
            return len(rows)


# --------------------------------------------------------------------------
# Background, batched profile extraction (regex/rules only; no network/model call).
_EXTRACTION_QUEUE: queue.Queue | None = None
_EXTRACTION_THREAD: threading.Thread | None = None
_EXTRACTION_LOCK = threading.Lock()


def _token_count(text: str) -> int:
    return len(re.findall(r"[\w\u0900-\u097F]+|[^\w\s]", text or ""))


def _extract_profile_facts(text: str) -> list[dict[str, Any]]:
    raw = " ".join((text or "").strip().split())
    if not raw or len(raw) > 800:
        return []
    low = raw.casefold()
    # Only stable, explicit profile statements are extracted. Sensitive details are never
    # auto-promoted; they need the separate confirmation flow in MemoryManager.
    if re.search(r"\b(password|passcode|secret|aadhaar|pan card|bank account|credit card|diagnos|medical|medicine|allerg|religion|politic|sexual|precise address|my salary)\b", low):
        return []
    patterns = (
        ("other_friend_name", r"\bmy\s+friend(?:['’]s)?\s+name\s+is\s+([^,.!?;]{2,72})", "Friend name"),
        ("other_friend_name", r"\b(?:mere|mera)\s+(?:dost|friend)\s+ka\s+naam\s+(.+?)\s+(?:hai|h|he)\b", "Friend name"),
        ("owner_name", r"\bmy name is\s+([^,.!?;]{2,72})", "Name"),
        ("owner_name", r"\bmera naam\s+(.+?)\s+(?:hai|h|he)\b", "Name"),
        ("location", r"\b(?:i live in|i reside in|i am from|i'm from|i moved to)\s+([^,.!?;]{2,72})", "Location"),
        ("location", r"\bmain\s+([\w .-]{2,50}?)\s+me(?:in)?\s+rehta(?:\s+hu(?:n)?)?\b", "Location"),
        ("project", r"\bmy project(?: name)? is\s+([^,.!?;]{2,72})", "Project"),
        ("workplace", r"\bi work at\s+([^,.!?;]{2,72})", "Workplace"),
        ("occupation", r"\bi work as\s+([^,.!?;]{2,72})", "Occupation"),
        ("response_style", r"\bi prefer (concise|brief|detailed|bullet[- ]point|short)\s+(?:answers?|replies|responses?)\b", "Response style"),
        ("language", r"\bi prefer (hindi|english|hinglish)\s+(?:replies|answers|responses)\b", "Language"),
    )
    facts: list[dict[str, Any]] = []
    for key, pattern, label in patterns:
        match = re.search(pattern, raw, re.I)
        if not match:
            continue
        value = re.sub(r"\s+", " ", match.group(1)).strip(" .,!?:;-_")
        if len(value) < 2:
            continue
        tags = f"other,friend,{key}" if key.startswith("other_") else f"profile,owner,{key}"
        facts.append({"key": key, "value": f"{label}: {value}", "tags": tags,
                      "importance": 0.78, "confidence": 0.90, "sensitivity": "normal"})
    return facts[:2]


def _apply_extraction_batch(store: MemoryStore, jobs: list[tuple[str, str, str]]) -> None:
    prepared: list[tuple[dict[str, Any], str, str]] = []
    for text, turn_id, session in jobs:
        for fact in _extract_profile_facts(text):
            prepared.append((fact, turn_id, session))
    if not prepared:
        return
    now = time.time()
    with store._lock, store._connect() as conn:
        for fact, turn_id, session in prepared:
            existing = conn.execute("SELECT id FROM facts WHERE key=?", (fact["key"],)).fetchone()
            if existing is None and fact["key"] == "owner_name":
                existing = conn.execute(
                    "SELECT id FROM facts WHERE key='name' AND tags LIKE '%profile%' "
                    "ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                if existing:
                    conn.execute("UPDATE facts SET key=? WHERE id=?", (fact["key"], existing["id"]))
            if existing:
                ident = str(existing["id"])
                conn.execute("UPDATE facts SET value=?, tags=?, importance=?, confidence=?, source_turn=?, "
                             "source_session=?, sensitivity='normal', confirmed=1, updated_at=? WHERE id=?",
                             (fact["value"], fact["tags"], fact["importance"], fact["confidence"],
                              turn_id, session, now, ident))
            else:
                ident = str(uuid.uuid4())
                conn.execute("INSERT INTO facts (id,key,value,tags,importance,created_at,updated_at,confidence,"
                             "source_turn,source_session,sensitivity,confirmed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (ident, fact["key"], fact["value"], fact["tags"], fact["importance"], now, now,
                              fact["confidence"], turn_id, session, "normal", 1))
            store._index(conn, ident, "fact", fact["value"])


def _ensure_extraction_worker() -> queue.Queue:
    global _EXTRACTION_QUEUE, _EXTRACTION_THREAD
    with _EXTRACTION_LOCK:
        if _EXTRACTION_QUEUE is None:
            _EXTRACTION_QUEUE = queue.Queue()
        if _EXTRACTION_THREAD is None or not _EXTRACTION_THREAD.is_alive():
            work_queue = _EXTRACTION_QUEUE

            def worker() -> None:
                while True:
                    first = work_queue.get()
                    batch = [first]
                    deadline = time.monotonic() + 0.025
                    while len(batch) < 24:
                        wait = max(0.0, deadline - time.monotonic())
                        if wait <= 0:
                            break
                        try:
                            batch.append(work_queue.get(timeout=wait))
                        except queue.Empty:
                            break
                    grouped: dict[MemoryStore, list[tuple[str, str, str]]] = {}
                    for store, text, turn_id, session in batch:
                        grouped.setdefault(store, []).append((text, turn_id, session))
                    for store, jobs in grouped.items():
                        try:
                            _apply_extraction_batch(store, jobs)
                        except Exception:
                            # Extraction is opportunistic and must never break a user turn.
                            pass
                    for _ in batch:
                        work_queue.task_done()

            _EXTRACTION_THREAD = threading.Thread(target=worker, name="jarvis-memory-extractor", daemon=True)
            _EXTRACTION_THREAD.start()
        return _EXTRACTION_QUEUE


def _enqueue_extraction(store: MemoryStore, user_text: str, turn_id: str, session: str) -> None:
    if not _extract_profile_facts(user_text):
        return
    _ensure_extraction_worker().put((store, user_text, turn_id, session))


def flush_memory_extraction(timeout: float = 5.0) -> bool:
    """Tests/admin diagnostics: wait for queued memory extraction; request handling never waits."""
    work_queue = _EXTRACTION_QUEUE
    if work_queue is None:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    while work_queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.005)
    return work_queue.unfinished_tasks == 0


# --------------------------------------------------------------------------
_default: MemoryStore | None = None
_stores: dict[tuple[str, str], MemoryStore] = {}
_stores_lock = threading.RLock()
_current_memory_user: contextvars.ContextVar[str] = contextvars.ContextVar(
    "jarvis_memory_user", default="nikhil_mehra")


@contextmanager
def use_memory_user(user_id: str):
    token = _current_memory_user.set(str(user_id or "nikhil_mehra"))
    try:
        yield
    finally:
        _current_memory_user.reset(token)


def get_memory(user_id: str | None = None, **kwargs: Any) -> MemoryStore:
    """Return the shared owner store or an isolated SQLite store for another user.

    Telegram guests receive per-user databases; their episodes/facts can neither appear in
    the owner's recall nor leak across guest accounts. User identifiers never become paths.
    """
    global _default
    uid = str(user_id or _current_memory_user.get() or "nikhil_mehra")
    db_path = kwargs.pop("db_path", None)
    if db_path is None:
        if uid == "nikhil_mehra":
            db_path = MEMORY_DB
        else:
            digest = hashlib.sha256(uid.encode("utf-8", errors="ignore")).hexdigest()[:24]
            db_path = WORKSPACE_DIR / ".private-memory" / f"{digest}.sqlite3"
    resolved_path = str(Path(db_path).expanduser().resolve())
    key = (uid, resolved_path)
    with _stores_lock:
        if key not in _stores:
            _stores[key] = MemoryStore(db_path=resolved_path, user_id=uid, **kwargs)
        store = _stores[key]
        if uid == "nikhil_mehra" and resolved_path == str(Path(MEMORY_DB).expanduser().resolve()):
            _default = store
        return store


def owner_profile_block(user_id: str | None = None) -> str:
    """User-scoped facts for the supervisor prompt; owner data never enters guest context."""
    uid = str(user_id or _current_memory_user.get() or "nikhil_mehra")
    mem = get_memory(user_id=uid)
    facts = mem.facts(limit=15)
    label = OWNER_NAME if uid == "nikhil_mehra" else "guest"
    if not facts:
        return f"(Profile: {label}. Abhi tak koi extra fact memory me nahi hai.)"
    return "Profile: " + label + "\nKnown facts:\n" + "\n".join(f"- {f['value'][:200]}" for f in facts)


if __name__ == "__main__":  # pragma: no cover
    m = get_memory()
    m.remember_fact(f"{OWNER_NAME} Kota, Rajasthan me rehta hai", key="location", tags="owner,personal")
    m.add_turn("bhai notes khol do", "हो गया boss ✅ notes khul gaye")
    print("stats:", m.stats())
    print("search 'notes':", [h.as_dict() for h in m.search("notes")])
    print(m.context_block(4))
    print("notes dir:", NOTES_DIR)
