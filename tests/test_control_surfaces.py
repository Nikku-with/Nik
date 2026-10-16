from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    from jarvis.config import runtime as R

    monkeypatch.setattr(R, "RUNTIME_SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(R, "_CACHE", {"mode": "text"})
    monkeypatch.setattr(R, "_LOADED", True)
    monkeypatch.setattr(R, "_FILE_SIGNATURE", R._file_signature())
    monkeypatch.setattr(R, "_save", lambda: True)
    monkeypatch.delenv("JARVIS_EXEC_APPROVAL", raising=False)
    monkeypatch.delenv("EXEC_APPROVAL", raising=False)
    return R


def test_gate_setting_is_saved_and_status_reports_environment_override(isolated_runtime, monkeypatch):
    from jarvis.runtime.controls import gate_mode_details, save_gate_mode
    from jarvis.security.gate import approval_mode

    ok, message = save_gate_mode("all")
    assert ok and "effective mode is all" in message
    assert approval_mode() == "all"

    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    info = gate_mode_details()
    assert info["mode"] == "risky"
    assert info["source"] == "environment override"
    assert isolated_runtime.snapshot()["exec_approval"] == "risky"

    ok, message = save_gate_mode("all")
    assert not ok and "forces Gate mode 'risky'" in message
    assert isolated_runtime.get("exec_approval") == "all"  # dormant preference is not misreported as effective


def test_gate_mode_rejects_unknown_values_without_writing(isolated_runtime, monkeypatch):
    from jarvis.runtime.controls import save_gate_mode

    writes = []
    monkeypatch.setattr(isolated_runtime, "_save", lambda: writes.append(True) or True)
    ok, message = save_gate_mode("sometimes")

    assert not ok and "Usage" in message
    assert writes == []
    assert isolated_runtime.get("exec_approval") is None


def test_model_and_provider_preferences_report_route_and_missing_key_without_network(monkeypatch, isolated_runtime):
    import jarvis.llm as llm
    from jarvis.llm import roles
    from jarvis.runtime import controls

    reset_calls = []
    monkeypatch.setattr(llm, "reset_cache", lambda: reset_calls.append(True))
    monkeypatch.setattr(roles, "resolve", lambda role, allow_discovery=False: SimpleNamespace(
        role=role, targets=[], reason="configured provider 'groq' has no key", describe=lambda: "unavailable",
    ))
    ok, message = controls.save_model_preference("gemini-3.8-flash")
    assert ok and "preference saved" in message and "no brain route" in message
    assert "connectivity was not tested" in message.lower()
    assert isolated_runtime.get("model") == "gemini-3.8-flash"
    assert reset_calls == [True]

    monkeypatch.setattr(llm, "health", lambda: {
        "active": "demo", "available": ["gemini"], "roles": {"brain": "(unavailable: missing groq key)"},
    })
    ok, message = controls.save_provider_preference("groq")
    assert ok and "key is unavailable" in message and "no provider switch" in message
    assert isolated_runtime.get("provider") == "groq"
    assert reset_calls == [True, True]

    ok, message = controls.save_provider_preference("not-a-provider")
    assert not ok and "allowed values" in message
    assert isolated_runtime.get("provider") == "groq"


def test_cli_mode_gate_and_environment_override_messages(isolated_runtime, monkeypatch):
    from jarvis.cli import app as cli_app
    from jarvis.security.gate import approval_mode

    messages = []
    monkeypatch.setattr(cli_app.ui, "cprint_safe", lambda text, *args, **kwargs: messages.append(str(text)))
    monkeypatch.setattr(cli_app.ui, "cprint", lambda text, *args, **kwargs: messages.append(str(text)))

    assert asyncio.run(cli_app.handle_command("/mode voice", "cli")) is True
    assert isolated_runtime.get("mode") == "voice"
    mode_output = " ".join(messages)
    assert "current interface remains CLI / TEXT" in mode_output
    assert "Microphone/LiveKit was not started" in mode_output

    isolated_runtime._CACHE["exec_approval"] = "risky"
    assert asyncio.run(cli_app.handle_command("/gate all", "cli")) is True
    assert approval_mode() == "all"
    assert "effective mode is all" in messages[-1]

    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    assert asyncio.run(cli_app.handle_command("/gate all", "cli")) is True
    assert approval_mode() == "risky"
    assert "environment configuration currently forces" in messages[-1].lower()


def test_cli_mode_save_failure_does_not_claim_success(isolated_runtime, monkeypatch):
    from jarvis.cli import app as cli_app

    messages = []
    monkeypatch.setattr(cli_app.ui, "cprint_safe", lambda text, *args, **kwargs: messages.append(str(text)))
    monkeypatch.setattr(isolated_runtime, "_save", lambda: False)

    asyncio.run(cli_app.handle_command("/text", "cli"))

    assert isolated_runtime.get("mode") == "text"
    assert "save nahi hui" in messages[-1]
    assert "✅" not in messages[-1]


def test_cli_model_provider_and_generic_settings_use_truthful_control_helpers(isolated_runtime, monkeypatch):
    from jarvis.cli import app as cli_app
    from jarvis.runtime import controls

    messages = []
    monkeypatch.setattr(cli_app.ui, "cprint_safe", lambda text, *args, **kwargs: messages.append(str(text)))
    monkeypatch.setattr(controls, "model_provider_status", lambda: {
        "provider_preference": "auto", "model_preference": "(provider default)",
        "active_provider": "demo", "brain_route": "(no key)", "available_providers": [],
        "connectivity": "not tested",
    })
    monkeypatch.setattr(controls, "save_model_preference", lambda _value: (False, "route unavailable; preference unchanged"))
    monkeypatch.setattr(controls, "save_provider_preference", lambda _value: (True, "preference saved; connectivity not tested"))

    asyncio.run(cli_app.handle_command("/model", "cli"))
    assert "Brain model preference" in messages[-1]
    assert "Connectivity: not tested" in messages[-1]
    asyncio.run(cli_app.handle_command("/model model-id", "cli"))
    assert messages[-1].startswith("⚠️") and "route unavailable" in messages[-1]
    asyncio.run(cli_app.handle_command("/provider", "cli"))
    assert "Provider preference" in messages[-1]
    asyncio.run(cli_app.handle_command("/provider groq", "cli"))
    assert messages[-1].startswith("✅") and "connectivity not tested" in messages[-1]
    asyncio.run(cli_app.handle_command("/settings exec_approval all", "cli"))
    assert messages[-1].startswith("✅") or "environment" in messages[-1].lower()


def _telegram_handler(application, command: str):
    from telegram.ext import CommandHandler

    return next(
        handler
        for group in application.handlers.values()
        for handler in group
        if isinstance(handler, CommandHandler) and command in handler.commands
    )


class _Message:
    def __init__(self):
        self.responses: list[str] = []

    async def reply_text(self, text, **_kwargs):
        self.responses.append(str(text))


def _telegram_update(user_id: int, chat_id: int, message: _Message):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message,
        message=message,
    )


def _telegram_application(monkeypatch, *, owner_id: int = 734_112, allowed_users=None):
    pytest.importorskip("telegram")
    from jarvis.telegram import bot

    monkeypatch.setattr(bot.S, "TELEGRAM_BOT_TOKEN", "123456789:" + "x" * 35)
    monkeypatch.setattr(bot.S, "TELEGRAM_OWNER_ID", str(owner_id))
    monkeypatch.setattr(bot.S, "TELEGRAM_ALLOWED_CHAT_IDS", [])
    monkeypatch.setattr(bot.S, "TELEGRAM_ALLOWED_USERS", [str(value) for value in (allowed_users or [])])
    return bot.build_application()


@pytest.mark.asyncio
async def test_telegram_mode_view_is_available_but_change_is_owner_only(isolated_runtime, monkeypatch):
    app = _telegram_application(monkeypatch, allowed_users=[456])
    handler = _telegram_handler(app, "mode")
    guest_message = _Message()

    await handler.callback(_telegram_update(456, 456, guest_message), SimpleNamespace(args=[]))
    assert "Default interface preference: text" in guest_message.responses[-1]
    assert "Current interface: Telegram text" in guest_message.responses[-1]

    await handler.callback(_telegram_update(456, 456, guest_message), SimpleNamespace(args=["voice"]))
    assert isolated_runtime.get("mode") == "text"
    assert "sirf owner" in guest_message.responses[-1]

    owner_message = _Message()
    await handler.callback(_telegram_update(734_112, 734_112, owner_message), SimpleNamespace(args=["voice"]))
    assert isolated_runtime.get("mode") == "voice"
    assert "preference voice successfully saved" in owner_message.responses[-1]
    assert "mic/LiveKit connection start nahi hua" in owner_message.responses[-1]


@pytest.mark.asyncio
async def test_telegram_gate_command_respects_owner_and_environment_override(isolated_runtime, monkeypatch):
    from jarvis.runtime.controls import gate_mode_details
    from jarvis.security.gate import approval_mode

    app = _telegram_application(monkeypatch, allowed_users=[456])
    handler = _telegram_handler(app, "gate")
    guest_message = _Message()

    await handler.callback(_telegram_update(456, 456, guest_message), SimpleNamespace(args=["all"]))
    assert "sirf owner" in guest_message.responses[-1]
    assert isolated_runtime.get("exec_approval") is None

    owner_message = _Message()
    await handler.callback(_telegram_update(734_112, 734_112, owner_message), SimpleNamespace(args=["all"]))
    assert approval_mode() == "all"
    assert "effective mode is all" in owner_message.responses[-1]

    monkeypatch.setenv("EXEC_APPROVAL", "risky")
    await handler.callback(_telegram_update(734_112, 734_112, owner_message), SimpleNamespace(args=["all"]))
    assert gate_mode_details()["mode"] == "risky"
    assert "environment configuration currently forces" in owner_message.responses[-1].lower()


@pytest.mark.asyncio
async def test_telegram_model_and_provider_commands_use_status_and_save_feedback(isolated_runtime, monkeypatch):
    app = _telegram_application(monkeypatch)
    from jarvis.runtime import controls

    monkeypatch.setattr(controls, "model_provider_status", lambda: {
        "provider_preference": "auto", "model_preference": "(provider default)",
        "active_provider": "demo", "brain_route": "(no key)", "available_providers": [],
        "connectivity": "not tested",
    })
    preference_calls = []

    def save_model(value):
        preference_calls.append(("model", value))
        return False, "route unavailable; preference unchanged"

    def save_provider(value):
        preference_calls.append(("provider", value))
        return False, "key unavailable; no provider switch"

    monkeypatch.setattr(controls, "save_model_preference", save_model)
    monkeypatch.setattr(controls, "save_provider_preference", save_provider)
    owner = _telegram_update(734_112, 734_112, _Message())

    model_handler = _telegram_handler(app, "model")
    await model_handler.callback(owner, SimpleNamespace(args=[]))
    assert "Brain model preference" in owner.message.responses[-1]
    assert "Provider connectivity: not tested" in owner.message.responses[-1]
    await model_handler.callback(owner, SimpleNamespace(args=["bad-model"]))
    assert owner.message.responses[-1].startswith("⚠️")
    assert "route unavailable" in owner.message.responses[-1]

    provider_handler = _telegram_handler(app, "provider")
    await provider_handler.callback(owner, SimpleNamespace(args=["groq"]))
    assert owner.message.responses[-1].startswith("⚠️")
    assert "no provider switch" in owner.message.responses[-1]

    settings_handler = _telegram_handler(app, "settings")
    await settings_handler.callback(owner, SimpleNamespace(args=["model", "settings-model"]))
    await settings_handler.callback(owner, SimpleNamespace(args=["provider", "settings-provider"]))
    assert preference_calls[-2:] == [
        ("model", "settings-model"), ("provider", "settings-provider"),
    ]


@pytest.mark.asyncio
async def test_telegram_settings_escapes_dynamic_values_in_html(isolated_runtime, monkeypatch):
    app = _telegram_application(monkeypatch)
    monkeypatch.setattr(isolated_runtime, "set_many", lambda **kwargs: (kwargs, []))

    class CapturingMessage:
        def __init__(self):
            self.responses = []

        async def reply_text(self, text, **kwargs):
            self.responses.append((text, kwargs))

    message = CapturingMessage()
    update = _telegram_update(734_112, 734_112, message)
    handler = _telegram_handler(app, "settings")

    await handler.callback(update, SimpleNamespace(args=["owner_name", "Nikhil_Mehra"]))

    text, options = message.responses[-1]
    assert options.get("parse_mode") == "HTML"
    assert "<code>owner_name</code>" in text
    assert "Nikhil_Mehra" in text


@pytest.mark.asyncio
async def test_telegram_provider_denial_is_single(isolated_runtime, monkeypatch):
    app = _telegram_application(monkeypatch)
    provider_handler = _telegram_handler(app, "provider")
    guest_message = _Message()

    await provider_handler.callback(_telegram_update(456, 456, guest_message), SimpleNamespace(args=["groq"]))
    assert len(guest_message.responses) == 1
    assert "not in the allowed" in guest_message.responses[0] or "allowed users" in guest_message.responses[0]


def test_cli_and_telegram_status_surfaces_show_effective_gate_source(monkeypatch):
    from jarvis.cli import app as cli_app
    from jarvis.runtime import status

    state = {
        "status": "DEMO", "assistant": "NikhilAI", "owner": "Nikhil", "provider": "demo",
        "configured_provider": "auto", "model": "(no active model)", "fast_model": "(no active model)",
        "mode": "voice", "interface": "audit", "workspace": "/tmp/work", "offline": True,
        "connection_tested": False, "time": "2026-10-14", "gate_mode": "risky",
        "gate_mode_source": "environment override", "memory": "0 facts", "tool_count": 1,
        "browser": "not installed", "livekit_status": "not ready", "telegram_status": "not configured",
        "automations": "0/0 enabled", "provider_status": "no provider key",
    }
    monkeypatch.setattr(status, "agent_status", lambda **_kwargs: dict(state))
    text = status.agent_status_text(compact=True, surface="Telegram")
    assert "Dangerous-Command Gate: `risky` (environment override)" in text

    monkeypatch.setattr(cli_app, "agent_status", lambda **_kwargs: dict(state))
    cli_status = cli_app._status_dict()
    assert cli_status["gate"] == "risky (environment override)"
