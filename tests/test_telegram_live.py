from __future__ import annotations

import json
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest


class _BalanceParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.invalid = False

    def handle_starttag(self, tag, attrs):
        if tag in {"b", "i", "code", "pre", "a"}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in {"b", "i", "code", "pre", "a"}:
            if not self.stack or self.stack[-1] != tag:
                self.invalid = True
            else:
                self.stack.pop()


def test_markdown_headings_and_link_urls_are_escaped_exactly_once():
    from jarvis.telegram.md import md_to_html

    rendered = md_to_html("# Heading & details\n[Doc](https://example.com/?a=1&b=2)")

    assert "<b>Heading &amp; details</b>" in rendered
    assert '<a href="https://example.com/?a=1&amp;b=2">Doc</a>' in rendered
    assert "&amp;amp;" not in rendered


@pytest.mark.asyncio
async def test_markdown_html_chunks_are_bounded_escaped_and_balanced():
    from jarvis.telegram.md import html_chunks

    source = ("**bold & <unsafe>** `code` [link](https://example.com)\n" * 900)
    chunks = html_chunks(source, limit=1200)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 1200
        parser = _BalanceParser()
        parser.feed(chunk)
        assert not parser.invalid
        assert not parser.stack
    assert all("<unsafe>" not in chunk for chunk in chunks)
    assert any("&lt;unsafe&gt;" in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_live_progress_splits_long_final_answer_without_html_truncation():
    from jarvis.telegram.live import LiveProgress

    class FakeMessage:
        def __init__(self):
            self.sent: list[tuple[str, dict]] = []
            self.edited: list[tuple[str, dict]] = []

        async def reply_text(self, text, **kwargs):
            self.sent.append((text, kwargs))
            return self

        async def edit_text(self, text, **kwargs):
            self.edited.append((text, kwargs))

    message = FakeMessage()
    progress = LiveProgress(message, enabled=True, min_interval=999)
    await progress.start()
    answer = ("<tag> & **bold** sentence. " * 1200)
    await progress.finish(answer, footer="\n— done")
    assert len(message.edited) == 1
    all_chunks = [message.edited[0][0], *(text for text, _ in message.sent)]
    assert len(all_chunks) > 1
    assert all(len(chunk) <= 3900 for chunk in all_chunks)
    assert all(kwargs.get("parse_mode") == "HTML" for _, kwargs in message.sent)
    assert "&lt;tag&gt;" in "".join(all_chunks)
    await progress._stop_flusher()


@pytest.mark.asyncio
async def test_telegram_action_buttons_are_opaque_one_use_owner_private_and_split(monkeypatch):
    pytest.importorskip("telegram")
    from jarvis.telegram.tools import consume_button_action, tg_send_buttons, use_telegram_context

    class FakeBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)
            return SimpleNamespace(message_id=len(self.messages))

    bot = FakeBot()
    action = "show private status and send a long report"
    payload = json.dumps([{"label": "Run it", "action": action}])
    long_text = "<script>private & safe</script> " * 500
    with use_telegram_context(bot, 999, 999, "owner"):
        result = await tg_send_buttons.ainvoke({"text": long_text, "buttons_json": payload})
    assert "1 safe message chunk" in result or "safe message chunk(s)" in result
    assert len(bot.messages) > 1
    assert all(len(item["text"]) <= 3500 for item in bot.messages)
    assert all(item["parse_mode"] == "HTML" for item in bot.messages)
    assert all("<script>" not in item["text"] for item in bot.messages)
    assert "&lt;script&gt;" in "".join(item["text"] for item in bot.messages)
    assert all(item.get("reply_markup") is None for item in bot.messages[:-1])

    markup = bot.messages[-1]["reply_markup"]
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert len(callbacks) == 1
    callback = callbacks[0]
    assert len(callback.encode()) < 64
    assert action not in callback
    assert consume_button_action(callback.removeprefix("tgact:"), user_id=123, chat_id=999) is None
    assert consume_button_action(callback.removeprefix("tgact:"), user_id=999, chat_id=998) is None
    assert consume_button_action(callback.removeprefix("tgact:"), user_id=999, chat_id=999) == action
    assert consume_button_action(callback.removeprefix("tgact:"), user_id=999, chat_id=999) is None

    before = len(bot.messages)
    with use_telegram_context(bot, 999, 123, "guest"):
        denied = await tg_send_buttons.ainvoke({"text": "no", "buttons_json": payload})
    assert "sirf owner" in denied.lower()
    with use_telegram_context(bot, 999, 999, "owner"):
        malformed = await tg_send_buttons.ainvoke({"text": "no", "buttons_json": "not-json"})
    assert "valid JSON" in malformed
    assert len(bot.messages) == before


@pytest.mark.asyncio
async def test_telegram_message_includes_reply_and_temporary_media_context(monkeypatch):
    pytest.importorskip("telegram")
    from pathlib import Path

    from jarvis.config import settings as settings_module
    from jarvis.runtime.progress import ProgressEvent
    from jarvis.telegram import bot as telegram_bot

    monkeypatch.setattr(settings_module, "TELEGRAM_BOT_TOKEN", "123456789:" + "x" * 35)
    monkeypatch.setattr(settings_module, "TELEGRAM_OWNER_ID", "999")
    monkeypatch.setattr(settings_module, "TELEGRAM_ALLOWED_USERS", [])
    monkeypatch.setattr(settings_module, "TELEGRAM_ALLOWED_CHAT_IDS", [])
    application = telegram_bot.build_application()
    handler = next(
        item for item in application.handlers[0]
        if item.__class__.__name__ == "MessageHandler"
    )
    received = []

    async def fake_run_agent(text, chat_id, role, on_progress=None, user_id=None):
        received.append({"text": text, "chat_id": chat_id, "role": role, "user_id": user_id})
        media_line = next(line for line in text.splitlines() if "Temporary local path:" in line)
        path = media_line.split("Temporary local path:", 1)[1].split(". Use an available", 1)[0].strip()
        assert Path(path).read_bytes() == b"fake image bytes"
        if on_progress:
            on_progress(ProgressEvent(kind="thinking", text="media context attached"))
        return {"answer": "Processed only the attached bytes", "status": "done", "errors": []}

    monkeypatch.setattr(telegram_bot, "run_agent", fake_run_agent)

    class FakeMessage:
        def __init__(self):
            self.text = None
            self.caption = "Summarize this picture"
            self.photo = [SimpleNamespace(file_id="fake-file-id")]
            self.reply_to_message = SimpleNamespace(text="Quoted message to keep in context")
            self.responses = []

        async def reply_text(self, text, **kwargs):
            self.responses.append((text, kwargs))
            return self

        async def edit_text(self, text, **kwargs):
            self.responses.append((text, kwargs))

    class FakeRemoteFile:
        async def download_to_drive(self, custom_path):
            Path(custom_path).write_bytes(b"fake image bytes")
            return custom_path

    class FakeBot:
        async def get_file(self, file_id):
            assert file_id == "fake-file-id"
            return FakeRemoteFile()

    class FakeChat(SimpleNamespace):
        async def send_action(self, _action):
            return None

    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999), effective_chat=FakeChat(id=999),
        effective_message=message, message=message,
    )
    context = SimpleNamespace(bot=FakeBot(), args=[])
    await handler.callback(update, context)

    assert len(received) == 1
    assert received[0]["role"] == "owner" and received[0]["user_id"] == 999
    assert "Quoted message to keep in context" in received[0]["text"]
    assert "Summarize this picture" in received[0]["text"]
    assert "Temporary local path:" in received[0]["text"]
    media_line = next(line for line in received[0]["text"].splitlines() if "Temporary local path:" in line)
    downloaded_path = media_line.split("Temporary local path:", 1)[1].split(". Use an available", 1)[0].strip()
    assert not Path(downloaded_path).exists()
    assert any("Processed only the attached bytes" in text for text, _kwargs in message.responses)


@pytest.mark.asyncio
async def test_edited_telegram_message_replies_through_effective_message(monkeypatch):
    pytest.importorskip("telegram")

    from jarvis.config import settings as settings_module
    from jarvis.telegram import bot as telegram_bot

    monkeypatch.setattr(settings_module, "TELEGRAM_BOT_TOKEN", "123456789:" + "x" * 35)
    monkeypatch.setattr(settings_module, "TELEGRAM_OWNER_ID", "999")
    monkeypatch.setattr(settings_module, "TELEGRAM_ALLOWED_USERS", [])
    monkeypatch.setattr(settings_module, "TELEGRAM_ALLOWED_CHAT_IDS", [])
    application = telegram_bot.build_application()
    handler = next(
        item for item in application.handlers[0]
        if item.__class__.__name__ == "MessageHandler"
    )

    class FakeMessage:
        text = "edited request"
        caption = None
        reply_to_message = None

        def __init__(self):
            self.responses = []

        async def reply_text(self, text, **kwargs):
            self.responses.append((text, kwargs))
            return self

        async def edit_text(self, text, **kwargs):
            self.responses.append((text, kwargs))

    class FakeChat(SimpleNamespace):
        async def send_action(self, _action):
            return None

    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999), effective_chat=FakeChat(id=999),
        effective_message=message, message=None,
    )
    context = SimpleNamespace(bot=SimpleNamespace(), args=[])

    async def fake_run_agent(text, chat_id, role, on_progress=None, user_id=None):
        assert text == "User request: edited request"
        assert chat_id == 999 and role == "owner" and user_id == 999
        return {"answer": "Edited request handled"}

    monkeypatch.setattr(telegram_bot, "run_agent", fake_run_agent)

    await handler.callback(update, context)

    assert any("Edited request handled" in text for text, _kwargs in message.responses)


def test_telegram_run_bot_retries_bootstrap_and_never_prints_token(monkeypatch, capsys):
    from jarvis.config import settings as settings_module
    from jarvis.telegram import bot as telegram_bot

    secret = "123456789:" + "A" * 35
    calls = {}

    class FakeApplication:
        def run_polling(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setattr(settings_module, "TELEGRAM_BOT_TOKEN", secret)
    monkeypatch.setattr(settings_module, "TELEGRAM_OWNER_ID", "999")
    monkeypatch.setattr(telegram_bot, "config_ok", lambda: (True, "ok"))
    monkeypatch.setattr(telegram_bot, "build_application", lambda: FakeApplication())
    assert telegram_bot.run_bot(poll_interval=0.2) == 0
    output = capsys.readouterr().out
    assert secret not in output and secret[:10] not in output
    assert calls == {"poll_interval": 0.2, "bootstrap_retries": -1, "drop_pending_updates": True}


@pytest.mark.asyncio
async def test_telegram_forget_all_flag_is_not_in_the_search_query(monkeypatch):
    pytest.importorskip("telegram")

    from jarvis.config import settings as settings_module
    from jarvis.memory import manager as memory_manager
    from jarvis.telegram import bot as telegram_bot

    monkeypatch.setattr(settings_module, "TELEGRAM_BOT_TOKEN", "123456789:" + "x" * 35)
    monkeypatch.setattr(settings_module, "TELEGRAM_OWNER_ID", "999")
    monkeypatch.setattr(settings_module, "TELEGRAM_ALLOWED_USERS", [])
    monkeypatch.setattr(settings_module, "TELEGRAM_ALLOWED_CHAT_IDS", [])
    application = telegram_bot.build_application()
    handler = next(
        item for item in application.handlers[0]
        if item.__class__.__name__ == "CommandHandler" and "forget" in item.commands
    )

    class FakeManager:
        calls = []

        def forget(self, query, all_matches=False):
            self.calls.append((query, all_matches))
            return {"ok": True, "message": "matching memories deleted"}

    manager = FakeManager()
    monkeypatch.setattr(memory_manager, "get_manager", lambda **_kwargs: manager)

    class FakeMessage:
        def __init__(self):
            self.responses = []

        async def reply_text(self, text, **kwargs):
            self.responses.append((text, kwargs))

    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999), effective_chat=SimpleNamespace(id=999),
        effective_message=message, message=message,
    )

    await handler.callback(update, SimpleNamespace(args=["old address", "--all"]))

    assert manager.calls == [("old address", True)]
    assert any("matching memories deleted" in text for text, _kwargs in message.responses)


@pytest.mark.asyncio
async def test_telegram_action_send_failure_removes_server_side_callbacks():
    pytest.importorskip("telegram")
    from jarvis.telegram.tools import (
        _PENDING_ACTIONS,
        _PENDING_LOCK,
        tg_send_buttons,
        use_telegram_context,
    )

    class FailingBot:
        async def send_message(self, **_kwargs):
            raise RuntimeError("fake Telegram send failure")

    with _PENDING_LOCK:
        before = set(_PENDING_ACTIONS)
    with use_telegram_context(FailingBot(), 42, 42, "owner"):
        result = await tg_send_buttons.ainvoke({
            "text": "payload", "buttons_json": json.dumps([{"label": "Do", "action": "safe status"}]),
        })
    assert "send nahi hue" in result
    with _PENDING_LOCK:
        assert set(_PENDING_ACTIONS) == before
