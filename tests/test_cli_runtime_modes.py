from __future__ import annotations

import sys

import pytest


@pytest.mark.parametrize(
    "line",
    [
        "help me write a proposal",
        "test my code",
        "web scrape weather data",
    ],
)
def test_command_words_with_unknown_arguments_remain_agent_requests(line):
    from jarvis.cli.routing import resolve_command

    result = resolve_command(line.split())

    assert result.kind == "request"
    assert result.args == line.split()


def test_serve_invalid_port_returns_friendly_error_without_starting_server(monkeypatch, capsys):
    from types import SimpleNamespace

    from jarvis import __main__ as jarvis_main

    started = []
    monkeypatch.setitem(
        sys.modules, "uvicorn",
        SimpleNamespace(run=lambda *args, **kwargs: started.append((args, kwargs))),
    )

    result = jarvis_main.cmd_serve(["127.0.0.1", "not-a-port"])

    assert result == 2
    assert started == []
    assert "port" in capsys.readouterr().out.lower()


def test_known_help_test_and_web_arguments_still_route_as_commands():
    from jarvis.cli.routing import resolve_command

    assert resolve_command(["help"]).kind == "help"
    assert resolve_command(["test"]).kind == "test"
    assert resolve_command(["web"]).kind == "serve"
    assert resolve_command(["web", "127.0.0.1", "8080"]).kind == "serve"


@pytest.mark.asyncio
async def test_cli_run_requires_gate_before_invoking_a_tool(monkeypatch):
    from jarvis.cli import app as cli_app
    from jarvis.security import gate

    gate_calls = []
    tool_calls = []

    async def authorize(*args, **kwargs):
        gate_calls.append((args, kwargs))
        return gate.ApprovalResult(allowed=kwargs.get("allow", False), reason="test decision")

    async def invoke(name, args):
        tool_calls.append((name, args))
        return "fake tool output"

    monkeypatch.setattr(gate, "authorize_command", authorize)
    monkeypatch.setattr("jarvis.cli.pro_ui.tool_schema", lambda _name: {})
    monkeypatch.setattr("jarvis.cli.pro_ui.run_tool_async", invoke)

    await cli_app.handle_command("/run system_info", "test-session")
    assert len(gate_calls) == 1
    assert gate_calls[0][1]["force"] is True
    assert gate_calls[0][1]["surface"] == "cli_owner"
    assert tool_calls == []

    async def approve(*args, **kwargs):
        gate_calls.append((args, kwargs))
        return gate.ApprovalResult(allowed=True, reason="owner approved")

    monkeypatch.setattr(gate, "authorize_command", approve)
    await cli_app.handle_command("/run system_info", "test-session")
    assert tool_calls == [("system_info", {})]


@pytest.mark.asyncio
async def test_cli_starts_voice_and_telegram_in_child_processes(monkeypatch):
    from jarvis.automation import scheduler
    from jarvis.cli import app as cli_app
    from jarvis.telegram import bot as telegram_bot
    from jarvis.voice import livekit_agent

    monkeypatch.setattr(scheduler, "register_delivery_handler", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "start_background", lambda: False)
    monkeypatch.setattr(cli_app.ui, "banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_app, "show_status", lambda: None)
    monkeypatch.setattr(cli_app, "agent_status", lambda **_kwargs: {
        "provider": "demo", "model": "fake-model", "status": "OFFLINE / DEMO",
    })

    inputs = iter(["console", "telegram", "/quit"])

    async def fake_to_thread(_func, *_args, **_kwargs):
        return next(inputs)

    monkeypatch.setattr(cli_app.asyncio, "to_thread", fake_to_thread)

    launched = []
    monkeypatch.setattr("subprocess.call", lambda argv: launched.append(list(argv)) or 0)
    monkeypatch.setattr(telegram_bot, "run_bot", lambda: pytest.fail("run_bot must not run in the REPL loop"))
    monkeypatch.setattr(
        livekit_agent,
        "main",
        lambda _args: pytest.fail("voice main must not run in the REPL loop"),
    )

    assert await cli_app.run_cli() == 0

    assert launched == [
        [sys.executable, "-m", "jarvis", "console"],
        [sys.executable, "-m", "jarvis", "telegram"],
    ]


@pytest.mark.asyncio
async def test_cli_menu_tasks_are_retained_and_exceptions_are_logged(caplog):
    from jarvis.cli import app as cli_app

    async def fail():
        raise RuntimeError("fake menu failure")

    task = cli_app._spawn_cli_task(fail())
    assert task in cli_app._CLI_TASKS

    with pytest.raises(RuntimeError, match="fake menu failure"):
        await task

    assert task not in cli_app._CLI_TASKS
    assert "CLI menu action failed" in caplog.text
    assert "fake menu failure" in caplog.text
