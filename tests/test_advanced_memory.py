from __future__ import annotations

import re

import pytest

from jarvis.memory.manager import MemoryManager
from jarvis.memory.store import MemoryStore, flush_memory_extraction


def test_profile_facts_episodes_and_notes_keep_provenance_separate(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", user_id="owner")
    turn_id = store.add_turn("My name is Nikhil Mehra", "Got it.", session="cli")
    assert flush_memory_extraction()

    facts = store.facts()
    assert len(facts) == 1
    fact = facts[0]
    assert fact["key"] == "owner_name"
    assert fact["value"] == "Name: Nikhil Mehra"
    assert fact["confidence"] >= 0.85
    assert fact["importance"] >= 0.7
    assert fact["source_turn"] == turn_id
    assert fact["source_session"] == "cli"
    assert fact["created_at"] and fact["updated_at"]

    with store._connect() as conn:
        episodes = conn.execute("SELECT role, content, turn_id, confidence, importance FROM episodes ORDER BY created_at").fetchall()
    assert [row["role"] for row in episodes] == ["user", "assistant"]
    assert {row["turn_id"] for row in episodes} == {turn_id}
    assert all(row["confidence"] > 0 and row["importance"] > 0 for row in episodes)

    note_id = store.index_note("design", "notes/design.md", "A separate note body", source_turn=turn_id)
    note = next(row for row in store.notes() if row["id"] == note_id)
    assert note["name"] == "design"
    with store._connect() as conn:
        note_row = conn.execute("SELECT source_turn, created_at, updated_at, importance, confidence FROM notes WHERE id=?",
                                (note_id,)).fetchone()
    assert note_row["source_turn"] == turn_id
    assert note_row["created_at"] and note_row["updated_at"]
    assert note_row["importance"] > 0 and note_row["confidence"] > 0


def test_short_memory_value_does_not_overwrite_unrelated_fact(tmp_path):
    store = MemoryStore(tmp_path / "short-overwrite.sqlite3", user_id="owner")
    manager = MemoryManager(store)

    original = "The quarterly release deadline for Northstar is Friday"
    created = manager.remember(original, kind="fact", key="release_deadline")
    short = manager.remember("Friday", kind="fact", key="weekday")
    duplicate = manager.remember(original, kind="fact", key="release_deadline")

    assert created["action"] == "created"
    assert short["action"] == "created"
    assert duplicate["action"] == "duplicate"
    values = {row["key"]: row["value"] for row in store.facts()}
    assert values == {"release_deadline": original, "weekday": "Friday"}


def test_batched_extraction_updates_contradicting_profile_fact(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", user_id="owner")
    store.add_turn("My project is NikhilAI", "Noted.", session="cli")
    assert flush_memory_extraction()
    first = next(row for row in store.facts() if row["key"] == "project")

    store.add_turn("Actually, my project is JarvisAI", "Updated.", session="telegram")
    assert flush_memory_extraction()
    projects = [row for row in store.facts() if row["key"] == "project"]
    assert len(projects) == 1
    assert projects[0]["id"] == first["id"]
    assert projects[0]["value"] == "Project: JarvisAI"
    assert projects[0]["source_session"] == "telegram"
    assert projects[0]["source_turn"] != first["source_turn"]


def test_other_person_name_is_not_stored_over_owner_name(tmp_path):
    store = MemoryStore(tmp_path / "subject-name.sqlite3", user_id="owner")
    manager = MemoryManager(store)
    store.remember_fact("Name: Nikhil Mehra", key="name", tags="profile,name")

    friend = manager.remember("Mere dost ka naam Ravi hai")

    facts = {row["key"]: row["value"] for row in store.facts()}
    assert friend["action"] == "created"
    assert facts["name"] == "Name: Nikhil Mehra"
    assert facts["other_friend_name"] == "Mere dost ka naam Ravi hai"


def test_batched_name_extraction_namespaces_owner_and_friend(tmp_path):
    store = MemoryStore(tmp_path / "subject-extraction.sqlite3", user_id="owner")
    store.add_turn("My name is Nikhil Mehra", "Noted.", session="cli")
    assert flush_memory_extraction()

    store.add_turn("Mere dost ka naam Ravi hai", "I will remember your friend's name.", session="telegram")
    assert flush_memory_extraction()

    facts = {row["key"]: row["value"] for row in store.facts()}
    assert facts["owner_name"] == "Name: Nikhil Mehra"
    assert facts["other_friend_name"] == "Friend name: Ravi"


def test_trivial_turns_remain_episodes_not_profile_facts(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", user_id="owner")
    for index, text in enumerate(("hey", "thanks", "nice", "haha", "good night", "cool", "okay")):
        store.add_turn(text, f"reply {index}", session="cli")
    assert flush_memory_extraction()
    assert store.facts() == []
    assert store.stats()["episodes"] == 14


def test_compaction_rejects_zero_and_preserves_episode_history(tmp_path):
    store = MemoryStore(tmp_path / "compact-zero.sqlite3", user_id="owner")
    for index in range(3):
        store.add_turn(f"Important detail {index}", f"Acknowledged {index}", session="cli")
    before = store.stats()["episodes"]

    with pytest.raises(ValueError, match="keep_last"):
        store.compact(keep_last=0)

    assert store.stats()["episodes"] == before
    assert all(store.search(f"Important detail {index}") for index in range(3))


@pytest.mark.asyncio
async def test_compaction_tool_returns_validation_error_without_mutating_memory(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "compact-tool-zero.sqlite3", user_id="owner")
    store.add_turn("Keep this user history", "Keep this reply", session="cli")
    before = store.stats()["episodes"]

    from jarvis.memory import memory_tools

    monkeypatch.setattr(memory_tools, "get_memory", lambda: store)
    result = await memory_tools.memory_compact.ainvoke({"keep_last": 0})

    assert "keep_last" in result
    assert store.stats()["episodes"] == before
    assert any("Keep this user history" in hit.text for hit in store.search("Keep this user history"))


def test_memory_connections_close_after_each_context(tmp_path):
    import sqlite3

    store = MemoryStore(tmp_path / "connections.sqlite3", user_id="owner")
    with store._connect() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_search_index_replaces_rows_through_an_indexed_reference_map(tmp_path):
    store = MemoryStore(tmp_path / "index-map.sqlite3", user_id="owner")
    ident = store.remember_fact("Old entry text", key="index_test")
    store.remember_fact("Updated entry text", key="index_test")

    with store._connect() as conn:
        rows = conn.execute(
            "SELECT rowid, text FROM search_index WHERE ref_id=? AND kind='fact'", (ident,)
        ).fetchall()
        mapping = conn.execute(
            "SELECT fts_rowid FROM search_refs WHERE ref_id=? AND kind='fact'", (ident,)
        ).fetchone()
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT fts_rowid FROM search_refs WHERE ref_id=? AND kind='fact'",
            (ident,),
        ).fetchone()["detail"]

    assert len(rows) == 1
    assert rows[0]["text"] == "Updated entry text"
    assert mapping["fts_rowid"] == rows[0]["rowid"]
    assert "SEARCH" in plan.upper()


def test_legacy_fts_rows_are_mapped_once_for_indexed_deletion(tmp_path):
    import sqlite3

    path = tmp_path / "legacy-index.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE VIRTUAL TABLE search_index USING fts5(ref_id UNINDEXED, kind UNINDEXED, text)"
    )
    cursor = legacy.execute(
        "INSERT INTO search_index (ref_id, kind, text) VALUES (?,?,?)",
        ("legacy-ref", "fact", "legacy indexed text"),
    )
    legacy_rowid = cursor.lastrowid
    legacy.commit()
    legacy.close()

    store = MemoryStore(path, user_id="owner")
    with store._connect() as conn:
        mapped = conn.execute(
            "SELECT fts_rowid FROM search_refs WHERE ref_id=?", ("legacy-ref",)
        ).fetchall()
        store._delete_index(conn, "legacy-ref")
    with store._connect() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) AS count FROM search_index WHERE rowid=?", (legacy_rowid,)
        ).fetchone()["count"]

    assert [row["fts_rowid"] for row in mapped] == [legacy_rowid]
    assert remaining == 0


def test_like_fallback_escapes_percent_and_underscore_metacharacters(tmp_path):
    store = MemoryStore(tmp_path / "like-escape.sqlite3", user_id="owner")
    store.remember_fact("alphaX beta unrelated", key="wildcard-near-match")
    store.remember_fact("alpha_%beta exact literal", key="wildcard-literal")
    store._fts = False

    hits = store.search("alpha_%beta")

    assert [hit.text for hit in hits] == ["alpha_%beta exact literal"]


def test_recall_survives_50_turns_restart_and_cross_surface(tmp_path):
    path = tmp_path / "continuity.sqlite3"
    store = MemoryStore(path, user_id="owner")
    store.add_turn("My project is ContinuityProject", "I will remember the project.", session="cli")
    assert flush_memory_extraction()
    for index in range(50):
        store.add_turn(f"Small turn {index}: the marker is turn-{index}", f"Acknowledged {index}.",
                       session="telegram" if index % 2 else "cli")
    assert flush_memory_extraction()

    restarted = MemoryStore(path, user_id="owner")
    hits = restarted.search("What is my project name?", limit=8)
    assert any("ContinuityProject" in hit.text for hit in hits)
    context = restarted.relevant_context("Tell me the project name", token_budget=120,
                                         session="web-session", recent_limit=4)
    assert "ContinuityProject" in context
    assert len(re.findall(r"\w+|[^\w\s]", context)) <= 120


def test_context_budget_is_hard_and_hybrid_ranking_prefers_relevant_fact(tmp_path):
    store = MemoryStore(tmp_path / "budget.sqlite3", user_id="owner")
    store.remember_fact("Project: MercuryLaunch is the active release", key="project",
                        importance=0.95, confidence=0.97)
    for index in range(30):
        store.remember_fact(f"Unrelated archive item {index}: {'background detail ' * 20}",
                            key=f"archive_{index}", importance=0.5)
    context = store.relevant_context("What is the active release project?", token_budget=55,
                                     limit=16, recent_limit=0)
    assert "MercuryLaunch" in context
    assert len(re.findall(r"\w+|[^\w\s]", context)) <= 55


def test_sensitive_memory_waits_for_confirmation_and_deletion_cleans_fts(tmp_path):
    store = MemoryStore(tmp_path / "sensitive.sqlite3", user_id="owner")
    manager = MemoryManager(store)

    pending = manager.remember("My diagnosis is diabetes")
    assert pending["action"] == "pending_confirmation"
    assert not store.facts()
    assert manager.pending()
    cancelled = manager.confirm_pending(confirmed=False)
    assert cancelled["action"] == "cancelled"
    assert not store.facts()

    pending = manager.remember("My allergy is peanuts")
    saved = manager.confirm_pending(confirmed=True, pending_id=pending["id"])
    assert saved["action"] == "confirmed"
    row = next(item for item in store.facts() if item["id"] == saved["id"])
    assert row["sensitivity"] == "sensitive" and row["confirmed"] == 1

    remembered = manager.remember("My project note is AtlasPlan", key="project")
    ident = remembered["id"]
    edited = manager.update(ident[:8], "Project: NovaPlan")
    assert edited["ok"]
    assert any("NovaPlan" in hit.text for hit in store.search("NovaPlan"))
    assert not any("AtlasPlan" in hit.text for hit in store.search("AtlasPlan"))
    forgotten = manager.forget(ident[:8])
    assert forgotten["ok"] and forgotten["deleted"] == 1
    assert not any("NovaPlan" in hit.text for hit in store.search("NovaPlan"))


def test_ambiguous_update_id_fails_before_staging_sensitive_replacement(tmp_path):
    store = MemoryStore(tmp_path / "ambiguous-update.sqlite3", user_id="owner")
    manager = MemoryManager(store)
    store.remember_fact("First project", key="first")
    store.remember_fact("Second project", key="second")
    with store._connect() as conn:
        conn.execute("UPDATE facts SET id=? WHERE key=?", ("abcdef12-0000-4000-8000-000000000001", "first"))
        conn.execute("UPDATE facts SET id=? WHERE key=?", ("abcdef12-0000-4000-8000-000000000002", "second"))

    rejected = manager.update("abcdef12", "My medical diagnosis is synthetic-condition")

    assert rejected["ok"] is False and "ambiguous" in rejected["message"]
    assert manager.pending() == []
    assert {row["value"] for row in store.facts()} == {"First project", "Second project"}


def test_confirm_pending_requires_an_exact_id_when_multiple_items_exist(tmp_path):
    store = MemoryStore(tmp_path / "pending-ambiguous.sqlite3", user_id="owner")
    manager = MemoryManager(store)
    first = manager.remember("My diagnosis is synthetic-condition")
    second = manager.remember("My allergy is synthetic-allergy")

    ambiguous = manager.confirm_pending(confirmed=True)

    assert ambiguous["action"] == "needs_user"
    assert ambiguous["ok"] is False
    assert len(manager.pending()) == 2
    assert manager.list_memories() == []
    confirmed = manager.confirm_pending(confirmed=True, pending_id=first["id"])
    assert confirmed["action"] == "confirmed"
    assert len(manager.pending()) == 1
    assert second["id"] == manager.pending()[0]["id"]


def test_exact_pending_id_resolves_items_older_than_the_default_listing_cap(tmp_path):
    store = MemoryStore(tmp_path / "pending-list-cap.sqlite3", user_id="owner")
    manager = MemoryManager(store)
    staged = [manager.remember(f"My diagnosis is synthetic-condition-{index}") for index in range(6)]

    assert len(store.pending_facts()) == 5
    resolved = manager.confirm_pending(confirmed=True, pending_id=staged[0]["id"])

    assert resolved["action"] == "confirmed"
    assert len(manager.pending()) == 5
    assert staged[0]["id"] not in {item["id"] for item in manager.pending()}


def test_sensitive_auto_extraction_is_not_promoted(tmp_path):
    store = MemoryStore(tmp_path / "sensitive-auto.sqlite3", user_id="owner")
    store.add_turn("My medical diagnosis is diabetes", "I will not save that automatically.", session="cli")
    assert flush_memory_extraction()
    assert store.facts() == []


@pytest.mark.parametrize(
    "description",
    [
        "Please forget my preference for masala chai",
        "Masala chai wali memory bhool jao",
    ],
)
def test_forget_by_natural_description_removes_only_the_matching_fact_and_fts(
    tmp_path, description,
):
    store = MemoryStore(tmp_path / "forget-description.sqlite3", user_id="owner")
    manager = MemoryManager(store)
    chai = manager.remember("I prefer masala chai every evening", kind="preference", key="chai")
    coffee = manager.remember("I prefer filter coffee on weekends", kind="preference", key="coffee")

    deleted = manager.forget(description)

    assert deleted["ok"] is True
    assert deleted["deleted"] == 1
    assert chai["id"] in deleted["ids"]
    assert coffee["id"] not in deleted["ids"]
    assert all("masala chai" not in row["value"].lower() for row in store.facts())
    assert any("filter coffee" in row["value"].lower() for row in store.facts())
    assert not any("masala chai" in hit.text.lower() for hit in store.search("masala chai"))
    broad = manager.forget("forget my memory")
    assert broad["ok"] is False
    assert any("filter coffee" in row["value"].lower() for row in store.facts())


def test_forget_description_matches_a_stored_key_when_value_omits_the_category(tmp_path):
    store = MemoryStore(tmp_path / "forget-key.sqlite3", user_id="owner")
    manager = MemoryManager(store)
    location = manager.remember("I live in Kota", kind="profile", key="location")

    deleted = manager.forget("Meri purani location wali memory hata do")

    assert deleted["ok"] is True and deleted["ids"] == [location["id"]]
    assert manager.list_memories() == []
