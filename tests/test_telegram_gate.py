from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jarvis.security import gate


class FakeMessage:
    def __init__(self, chat_id=999):
        self.chat_id = chat_id
        self.chat = SimpleNamespace(id=chat_id)
        self.texts = []
        self.markups = []

    async def edit_text(self, text, **kwargs):
        self.texts.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return self


class FakeQuery:
    def __init__(self, data, user_id=999, chat_id=999, message=None):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = message or FakeMessage(chat_id)
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


@pytest.mark.asyncio
async def test_telegram_approval_buttons_are_short_owner_only_and_edit_result(monkeypatch):
    pytest.importorskip("telegram")
    from jarvis.automation import scheduler as sch
    from jarvis.config import settings as S
    from jarvis.telegram.bot import build_application

    monkeypatch.setattr(sch, "configure_agent_runner", lambda: None)
    monkeypatch.setattr(sch, "start_background", lambda: False)
    monkeypatch.setattr(sch, "stop_background", lambda: None)
    monkeypatch.setattr(S, "TELEGRAM_BOT_TOKEN", "123456789:" + "x" * 35)
    monkeypatch.setattr(S, "TELEGRAM_OWNER_ID", "999")
    monkeypatch.setattr(S, "TELEGRAM_ALLOWED_USERS", [])
    monkeypatch.setattr(S, "TELEGRAM_ALLOWED_CHAT_IDS", [])
    app = build_application()
    await app.post_init(app)
    broker = gate.ApprovalBroker(owner_id="999", ttl_s=1)
    monkeypatch.setattr(gate, "BROKER", broker)
    sent = []
    approval_message = FakeMessage()

    async def fake_send_message(_bot, **kwargs):
        sent.append(kwargs)
        return approval_message

    monkeypatch.setattr(type(app.bot), "send_message", fake_send_message)
    callback_handler = next(handler for handler in app.handlers[0]
                            if handler.__class__.__name__ == "CallbackQueryHandler")
    on_button = callback_handler.callback
    emitted = asyncio.Event()
    captured = []
    registered_prompt = gate._surface_handlers["telegram_owner"]

    async def prompt(request):
        captured.append(request)
        emitted.set()
        return await registered_prompt(request)

    gate.register_approval_handler("telegram_owner", prompt)
    try:
        long_command = "rm -rf <temporary> " + ("safe-arg " * 650)
        approval_task = asyncio.create_task(
            broker.ask(long_command, "/workspace", "recursive delete", "telegram_owner")
        )
        await asyncio.wait_for(emitted.wait(), 0.5)
        assert len(sent) > 1
        assert "&lt;temporary&gt;" in sent[0]["text"]
        assert long_command[-80:] in "".join(item["text"] for item in sent)
        markup = sent[-1]["reply_markup"]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        assert len(callbacks) == 2
        assert all(len(callback.encode()) < 64 for callback in callbacks)
        assert all("rm -rf" not in callback for callback in callbacks)

        # A guest click must not consume the owner request.
        wrong = FakeQuery(callbacks[0], user_id=123, chat_id=999, message=approval_message)
        await on_button(SimpleNamespace(callback_query=wrong), None)
        assert wrong.answers and not (awaitable_done(approval_task))

        owner_query = FakeQuery(callbacks[0], user_id=999, chat_id=999, message=approval_message)
        await on_button(SimpleNamespace(callback_query=owner_query), None)
        assert (await approval_task).allowed
        long_result = "fake execution result " + ("complete-output " * 500)
        await gate.report_approval_completion(
            captured[0].id, "telegram_owner", "ran", long_result,
        )
        assert any("approved" in text.lower() for text in approval_message.texts)
        assert long_result[-80:] in "".join(approval_message.texts)
        assert all(len(text) <= 3900 for text in approval_message.texts)
        assert owner_query.answers

        emitted.clear()
        captured.clear()
        cancel_message = FakeMessage()
        original_send = type(app.bot).send_message

        async def send_cancel(_bot, **kwargs):
            sent.append(kwargs)
            return cancel_message

        monkeypatch.setattr(type(app.bot), "send_message", send_cancel)
        cancel_task = asyncio.create_task(
            broker.ask("rm -rf /tmp/cancel", "/tmp", "recursive delete", "telegram_owner")
        )
        await asyncio.wait_for(emitted.wait(), 0.5)
        cancel_data = [button.callback_data for row in sent[-1]["reply_markup"].inline_keyboard for button in row]
        cancel_query = FakeQuery(cancel_data[1], user_id=999, chat_id=999, message=cancel_message)
        await on_button(SimpleNamespace(callback_query=cancel_query), None)
        assert not (await cancel_task).allowed
        assert any("Cancelled" in text for text in cancel_message.texts)
        monkeypatch.setattr(type(app.bot), "send_message", original_send)
    finally:
        await app.post_shutdown(app)
        gate.register_approval_handler("telegram_owner", None)
        gate.register_approval_completion_handler("telegram_owner", None)


def awaitable_done(task):
    return task.done()


@pytest.mark.asyncio
async def test_telegram_command_button_shim_exposes_effective_message(monkeypatch):
    pytest.importorskip("telegram")
    from jarvis.config import settings as S
    from jarvis.telegram.bot import build_application

    monkeypatch.setattr(S, "TELEGRAM_BOT_TOKEN", "123456789:" + "x" * 35)
    monkeypatch.setattr(S, "TELEGRAM_OWNER_ID", "999")
    monkeypatch.setattr(S, "TELEGRAM_ALLOWED_USERS", [])
    monkeypatch.setattr(S, "TELEGRAM_ALLOWED_CHAT_IDS", [])
    app = build_application()
    callback_handler = next(
        handler for handler in app.handlers[0]
        if handler.__class__.__name__ == "CallbackQueryHandler"
    )
    query = FakeQuery("cmd:settings", user_id=999, chat_id=999)

    await callback_handler.callback(SimpleNamespace(callback_query=query), SimpleNamespace(args=[]))

    assert query.message.texts
    assert any("Settings" in text for text in query.message.texts)


@pytest.mark.asyncio
async def test_telegram_stop_and_restart_use_single_owner_authorized_paths(monkeypatch):
    pytest.importorskip("telegram")
    from jarvis.config import settings as S
    from jarvis.security.permissions import current_surface
    from jarvis.telegram.bot import build_application
    from jarvis.tools import system_ops

    monkeypatch.setattr(S, "TELEGRAM_BOT_TOKEN", "123456789:" + "x" * 35)
    monkeypatch.setattr(S, "TELEGRAM_OWNER_ID", "999")
    monkeypatch.setattr(S, "TELEGRAM_ALLOWED_USERS", [])
    monkeypatch.setattr(S, "TELEGRAM_ALLOWED_CHAT_IDS", [])
    app = build_application()
    calls = []

    async def stop_once():
        calls.append(("stop", current_surface()))
        return {"ok": True, "message": "stopped fake process"}

    async def restart_once():
        calls.append(("restart", current_surface()))
        return {"ok": False, "message": "restart disabled"}

    monkeypatch.setattr(system_ops, "clear_processes_authorized", stop_once)
    monkeypatch.setattr(system_ops, "restart_self", restart_once)
    stop_handler = next(handler for handler in app.handlers[0] if "stop" in getattr(handler, "commands", set()))
    restart_handler = next(handler for handler in app.handlers[0] if "restart" in getattr(handler, "commands", set()))

    class Message(FakeMessage):
        async def reply_text(self, text, **kwargs):
            self.texts.append(text)
            return self

    message = Message()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999), effective_chat=SimpleNamespace(id=999),
        effective_message=message, message=message,
    )
    await stop_handler.callback(update, SimpleNamespace(args=[]))
    await restart_handler.callback(update, SimpleNamespace(args=[]))

    assert calls == [("stop", "telegram_owner"), ("restart", "telegram_owner")]
    assert any("stopped fake process" in text for text in message.texts)
    assert any("restart disabled" in text for text in message.texts)


@pytest.mark.asyncio
async def test_telegram_scheduler_delivers_to_configured_owner(monkeypatch, tmp_path):
    pytest.importorskip("telegram")
    from jarvis.automation import scheduler as sch
    from jarvis.config import settings as S
    from jarvis.telegram.bot import build_application

    monkeypatch.setattr(S, "TELEGRAM_BOT_TOKEN", "123456789:" + "x" * 35)
    monkeypatch.setattr(S, "TELEGRAM_OWNER_ID", "999")
    monkeypatch.setattr(S, "TELEGRAM_ALLOWED_USERS", [])
    monkeypatch.setattr(S, "TELEGRAM_ALLOWED_CHAT_IDS", [])
    app = build_application()
    sent = []

    async def fake_send_message(_bot, **kwargs):
        sent.append(kwargs)
        return FakeMessage()

    monkeypatch.setattr(type(app.bot), "send_message", fake_send_message)
    store = sch.AutomationStore(tmp_path / "automations.json")
    monkeypatch.setattr(sch, "_STORE", store)
    monkeypatch.setattr(sch, "configure_agent_runner", lambda: None)
    monkeypatch.setattr(sch, "start_background", lambda: False)
    monkeypatch.setattr(sch, "stop_background", lambda: None)
    await app.post_init(app)
    monkeypatch.setattr(sch, "RUNNER", lambda task, auto_id, run_id, surface: {
        "answer": f"done: {task}", "status": "done", "errors": [],
    })
    created = store.create("send report", "every 2h", channel="telegram_owner")
    try:
        result = await asyncio.to_thread(sch.run_now, created["automation"]["id"])
        assert result["ok"] is True
        assert sent and sent[0]["chat_id"] == 999
        assert "send report" in sent[0]["text"]
        assert "done: send report" in sent[0]["text"]
    finally:
        await app.post_shutdown(app)
