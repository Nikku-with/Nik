from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _api_memory_write_endpoint():
    pytest.importorskip("fastapi")
    from jarvis.api import server

    return next(
        route.endpoint for route in server.app.routes
        if getattr(route, "path", "") == "/memory" and "POST" in getattr(route, "methods", set())
    )


def _api_memory_read_endpoint():
    pytest.importorskip("fastapi")
    from jarvis.api import server

    return next(
        route.endpoint for route in server.app.routes
        if getattr(route, "path", "") == "/memory" and "GET" in getattr(route, "methods", set())
    )


def _json(response):
    return json.loads(response.body.decode("utf-8"))


@pytest.mark.asyncio
async def test_api_sensitive_memory_requires_explicit_id_bound_confirm_or_cancel(tmp_path, monkeypatch):
    from jarvis.memory import manager as manager_module
    from jarvis.memory.manager import MemoryManager
    from jarvis.memory.store import MemoryStore

    memory = MemoryManager(MemoryStore(tmp_path / "api-memory.sqlite3", user_id="owner"))
    monkeypatch.setattr(manager_module, "get_manager", lambda *_args, **_kwargs: memory)
    endpoint = _api_memory_write_endpoint()

    staged_response = await endpoint({"action": "remember", "text": "My diagnosis is synthetic-condition"})
    staged = _json(staged_response)
    assert staged["action"] == "pending_confirmation"
    assert staged["id"]
    assert memory.list_memories() == []
    read_response = await _api_memory_read_endpoint()()
    assert _json(read_response)["pending"][0]["id"] == staged["id"]

    missing_id = await endpoint({"action": "confirm"})
    assert missing_id.status_code == 400
    assert memory.pending()
    assert memory.list_memories() == []

    wrong_id = await endpoint({"action": "confirm", "id": "not-the-pending-id"})
    assert wrong_id.status_code == 409
    assert memory.pending()
    assert memory.list_memories() == []

    saved_response = await endpoint({"action": "confirm", "id": staged["id"]})
    saved = _json(saved_response)
    assert saved["ok"] is True and saved["action"] == "confirmed"
    facts = memory.list_memories()
    assert len(facts) == 1
    assert facts[0]["sensitivity"] == "sensitive" and facts[0]["confirmed"] is True
    replay = await endpoint({"action": "confirm", "id": staged["id"]})
    assert replay.status_code == 409
    assert len(memory.list_memories()) == 1

    second = memory.remember("My medication is synthetic-medication")
    cancelled_response = await endpoint({"action": "cancel", "id": second["id"]})
    cancelled = _json(cancelled_response)
    assert cancelled["ok"] is True and cancelled["action"] == "cancelled"
    assert len(memory.list_memories()) == 1
    assert not memory.pending()


@pytest.mark.asyncio
async def test_api_sensitive_update_leaves_old_fact_until_specific_confirmation(tmp_path, monkeypatch):
    from jarvis.memory import manager as manager_module
    from jarvis.memory.manager import MemoryManager
    from jarvis.memory.store import MemoryStore

    memory = MemoryManager(MemoryStore(tmp_path / "api-update-consent.sqlite3", user_id="owner"))
    original = memory.remember("My favorite color is blue", kind="preference", key="color")
    missing_id = memory.update("", "My medical diagnosis is synthetic-condition")
    assert missing_id["ok"] is False and not memory.pending()
    monkeypatch.setattr(manager_module, "get_manager", lambda *_args, **_kwargs: memory)
    endpoint = _api_memory_write_endpoint()

    response = await endpoint({
        "action": "update", "id": original["id"], "text": "My medical diagnosis is synthetic-condition",
    })
    staged = _json(response)
    assert staged["action"] == "pending_confirmation"
    assert any(row["id"] == original["id"] and row["text"] == "My favorite color is blue"
               for row in memory.list_memories())
    assert not any("synthetic-condition" in row["text"] for row in memory.list_memories())

    result = _json(await endpoint({"action": "confirm", "id": staged["id"]}))
    assert result["action"] == "confirmed"
    assert not any(row["id"] == original["id"] for row in memory.list_memories())
    updated = memory.list_memories()
    assert len(updated) == 1 and updated[0]["sensitivity"] == "sensitive"


@pytest.mark.asyncio
async def test_api_forget_accepts_a_natural_description_and_keeps_other_facts(tmp_path, monkeypatch):
    from jarvis.memory import manager as manager_module
    from jarvis.memory.manager import MemoryManager
    from jarvis.memory.store import MemoryStore

    memory = MemoryManager(MemoryStore(tmp_path / "api-forget.sqlite3", user_id="owner"))
    memory.remember("I prefer masala chai every evening", kind="preference", key="chai")
    memory.remember("I prefer filter coffee on weekends", kind="preference", key="coffee")
    monkeypatch.setattr(manager_module, "get_manager", lambda *_args, **_kwargs: memory)
    endpoint = _api_memory_write_endpoint()

    response = await endpoint({
        "action": "forget",
        "text": "Please forget my preference for masala chai",
    })
    result = _json(response)

    assert result["ok"] is True and result["deleted"] == 1
    values = [row["text"].lower() for row in memory.list_memories()]
    assert not any("masala chai" in value for value in values)
    assert any("filter coffee" in value for value in values)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "telegram_owner"])
async def test_chat_confirmation_accepts_prompted_punctuation_and_never_guesses_with_multiple_items(
    tmp_path, monkeypatch, surface,
):
    from jarvis.memory import manager as manager_module
    from jarvis.memory.manager import MemoryManager
    from jarvis.memory.store import MemoryStore
    from jarvis.runtime import agent

    memory = MemoryManager(MemoryStore(tmp_path / "chat-consent.sqlite3", user_id="owner"))
    monkeypatch.setattr(manager_module, "get_manager", lambda *_args, **_kwargs: memory)

    class StateStub:
        def add_task(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(agent, "get_state", lambda: StateStub())
    monkeypatch.setattr(agent, "_post_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent, "_emit_final_status", lambda *_args, **_kwargs: None)

    pending = memory.remember("My diagnosis is synthetic-condition")
    result = await agent.run_agent(
        "haan, save karo", session_id=f"consent-{surface}", surface=surface, memory_user_id="owner",
    )
    assert result.status == "memory"
    assert not memory.pending()
    assert any(row["id"] == pending["id"] or "synthetic-condition" in row["text"]
               for row in memory.list_memories())

    memory.remember("My medication is synthetic-medication")
    cancelled = await agent.run_agent(
        "nahi.", session_id=f"consent-cancel-{surface}", surface=surface, memory_user_id="owner",
    )
    assert cancelled.status == "memory"
    assert not memory.pending()
    assert not any("synthetic-medication" in row["text"] for row in memory.list_memories())

    second_pending = memory.remember("My medication is synthetic-medication")
    third_pending = memory.remember("My allergy is synthetic-allergy")
    ambiguous = await agent.run_agent(
        "haan, save karo", session_id=f"consent-ambiguous-{surface}", surface=surface, memory_user_id="owner",
    )
    assert ambiguous.status == "needs_user"
    assert len(memory.pending()) == 2
    assert len(memory.list_memories()) == 1
    assert second_pending["id"] in ambiguous.answer
    assert third_pending["id"] in ambiguous.answer
    assert "synthetic-medication" not in ambiguous.answer
    assert "synthetic-allergy" not in ambiguous.answer
    if surface == "api":
        assert "GET /memory" in ambiguous.answer
    else:
        assert "/memory pending" in ambiguous.answer


@pytest.mark.asyncio
async def test_telegram_owner_can_list_confirm_and_cancel_exact_pending_memory(tmp_path, monkeypatch):
    pytest.importorskip("telegram")
    from jarvis.config import settings as settings_module
    from jarvis.memory import manager as manager_module
    from jarvis.memory.manager import MemoryManager
    from jarvis.memory.store import MemoryStore
    from jarvis.telegram import bot as telegram_bot

    monkeypatch.setattr(settings_module, "TELEGRAM_BOT_TOKEN", "123456789:" + "x" * 35)
    monkeypatch.setattr(settings_module, "TELEGRAM_OWNER_ID", "999")
    monkeypatch.setattr(settings_module, "TELEGRAM_ALLOWED_USERS", [])
    monkeypatch.setattr(settings_module, "TELEGRAM_ALLOWED_CHAT_IDS", [])

    memory = MemoryManager(MemoryStore(tmp_path / "telegram-memory.sqlite3", user_id="nikhil_mehra"))
    monkeypatch.setattr(manager_module, "get_manager", lambda *_args, **_kwargs: memory)
    application = telegram_bot.build_application()

    def command(name):
        return next(
            handler.callback for group in application.handlers.values() for handler in group
            if name in getattr(handler, "commands", set())
        )

    class FakeMessage:
        def __init__(self):
            self.responses = []

        async def reply_text(self, text, **_kwargs):
            self.responses.append(str(text))

    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_chat=SimpleNamespace(id=999),
        effective_message=message,
        message=message,
    )

    await command("remember")(update, SimpleNamespace(args=["My", "diagnosis", "is", "synthetic-condition"]))
    pending_id = memory.pending()[0]["id"]
    assert pending_id in "\n".join(message.responses)
    assert f"/memory confirm {pending_id}" in "\n".join(message.responses)

    await command("memory")(update, SimpleNamespace(args=["pending"]))
    assert pending_id in message.responses[-1]
    assert "synthetic-condition" not in message.responses[-1]

    await command("memory")(update, SimpleNamespace(args=["confirm", pending_id]))
    assert not memory.pending()
    assert any("synthetic-condition" in row["text"] for row in memory.list_memories())

    await command("remember")(update, SimpleNamespace(args=["My", "medication", "is", "synthetic-medication"]))
    second_id = memory.pending()[0]["id"]
    await command("memory")(update, SimpleNamespace(args=["cancel", second_id]))
    assert not memory.pending()
    assert not any("synthetic-medication" in row["text"] for row in memory.list_memories())

    memory.remember("I prefer masala chai every evening", kind="preference", key="chai")
    await command("forget")(
        update, SimpleNamespace(args=["Please", "forget", "my", "preference", "for", "masala", "chai"]),
    )
    assert not any("masala chai" in row["text"].lower() for row in memory.list_memories())

    guest_pending = memory.remember("My allergy is synthetic-allergy")
    monkeypatch.setattr(settings_module, "TELEGRAM_ALLOWED_USERS", ["123"])
    guest_message = FakeMessage()
    guest_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=guest_message,
        message=guest_message,
    )
    await command("memory")(guest_update, SimpleNamespace(args=["confirm", guest_pending["id"]]))
    assert "sirf owner" in guest_message.responses[-1].lower()
    assert guest_pending["id"] == memory.pending()[0]["id"]
    assert not any("synthetic-allergy" in row["text"] for row in memory.list_memories())
