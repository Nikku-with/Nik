from __future__ import annotations

import uuid

import pytest


def test_tool_audit_skips_mutating_and_code_execution_tools():
    from scripts.audit_tools import NO_LIVE

    required = {
        "clear_cache_tool", "code_engine", "create_project", "undo_file_operation",
        "install_package", "python_repl", "run_python", "run_script_file",
        "session_note", "set_pending_steps", "set_preference", "set_workspace",
    }
    assert required <= NO_LIVE


@pytest.mark.asyncio
async def test_tool_audit_redacts_invoked_output_from_report():
    from scripts.audit_tools import OK, check_tool

    class PrivateOutputTool:
        name = "private_read"
        description = "Returns private user content for a synthetic audit test."
        args_schema = None

        async def ainvoke(self, _args):
            return "private memory phrase"

    status, detail = await check_tool(PrivateOutputTool(), live_dangerous=False)
    assert status == OK
    assert "private memory phrase" not in detail
    assert "content redacted" in detail


def test_documented_role_model_env_is_honored(monkeypatch):
    from jarvis.config import settings as S

    for name in ("NIKHILAI_FAST_MODEL", "JARVIS_FAST_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FAST_MODEL", "gemini-audit-fast-model")
    monkeypatch.setenv("NIKHILAI_FAST_FALLBACKS", "")
    monkeypatch.setenv("JARVIS_FAST_FALLBACKS", "")
    monkeypatch.setenv("FAST_FALLBACKS", "")

    chain = S.model_chain_for("fast", "gemini")
    assert chain[0] == "gemini-audit-fast-model"


def test_auto_provider_routes_never_pair_another_provider_model(monkeypatch):
    from jarvis.llm import roles

    def fake_get(key, default=None):
        return {"provider": "auto", "model": None, "temperature": 0}.get(key, default)

    monkeypatch.setattr(roles.R, "get", fake_get)
    monkeypatch.setattr(roles.S, "BRAIN_PROVIDER_CONFIG", "auto")
    monkeypatch.setattr(roles.S, "_auto_provider", lambda: "gemini")
    monkeypatch.setattr(roles.P, "is_available", lambda provider: provider in {"gemini", "groq"})
    monkeypatch.setattr(roles.S, "explicit_model_for", lambda role: "")
    monkeypatch.setattr(
        roles.S, "model_chain_for",
        lambda role, provider: ["gemini-3.8-flash"] if provider == "gemini" else [],
    )
    monkeypatch.setattr(
        roles.S, "provider_model_defaults",
        lambda role, provider: {
            "gemini": ["gemini-3.8-flash"],
            "groq": ["openai/gpt-oss-20b"],
        }.get(provider, []),
    )
    monkeypatch.setattr(roles.S, "MAX_MODEL_TARGETS", 4)

    route = roles.resolve("brain", allow_discovery=False)
    assert route.primary is not None
    assert (route.primary.provider, route.primary.model) == ("gemini", "gemini-3.8-flash")
    assert route.temperature == 0.0
    assert all(t.provider != "groq" or t.model != "gemini-3.8-flash" for t in route.targets)
    assert any(t.provider == "groq" and t.model == "openai/gpt-oss-20b" for t in route.targets)


def test_demo_model_serializes_non_string_tool_results():
    from langchain_core.messages import ToolMessage

    from jarvis.llm.demo import DemoChatModel

    message = ToolMessage(
        content=[{"type": "text", "text": "result payload"}],
        name="sample_tool",
        tool_call_id="audit-call",
    )
    answer = DemoChatModel()._summarize("question", [message])
    assert "result payload" in answer


def test_discovery_summary_handles_provider_with_no_live_models(monkeypatch):
    from jarvis.llm import discovery

    monkeypatch.setattr(discovery.P, "PROVIDERS", {"gemini": object()})
    monkeypatch.setattr(discovery.P, "is_available", lambda _name: True)
    monkeypatch.setattr(discovery, "list_models", lambda _name: [])

    assert "1 provider key set" in discovery.summary()


def test_model_discovery_does_not_assume_unknown_tool_support():
    from jarvis.llm.discovery import _parse

    rows = _parse({"data": [
        {"id": "with-tools", "supported_parameters": ["tools", "tool_choice"]},
        {"id": "without-tools", "supported_parameters": ["temperature"]},
        {"id": "unknown-tools"},
    ]}, "openrouter")
    assert [row["tools"] for row in rows] == [True, False, None]


def test_incompatible_explicit_model_fails_closed(monkeypatch):
    from jarvis.llm import roles

    monkeypatch.setattr(
        roles.R, "get",
        lambda key, default=None: {"provider": "auto", "model": None, "temperature": None}.get(key, default),
    )
    monkeypatch.setattr(roles.S, "BRAIN_PROVIDER_CONFIG", "groq")
    monkeypatch.setattr(roles.P, "is_available", lambda provider: provider == "groq")
    monkeypatch.setattr(roles.S, "explicit_model_for", lambda role: "gemini-3.8-flash")
    monkeypatch.setattr(roles.S, "model_chain_for", lambda role, provider: ["gemini-3.8-flash"])

    route = roles.resolve("brain", allow_discovery=False)
    assert route.targets == []
    assert "incompatible with configured provider 'groq'" in route.reason


def test_missing_explicit_provider_key_does_not_fall_back_to_another_key(monkeypatch):
    from jarvis.llm import core, roles

    monkeypatch.setattr(core.Runtime, "get", lambda key, default=None: "auto" if key == "provider" else default)
    monkeypatch.setattr(core.S, "BRAIN_PROVIDER_CONFIG", "groq")
    monkeypatch.setattr(core.P, "is_available", lambda provider: provider == "gemini")
    monkeypatch.setattr(core.P, "available", lambda: ["gemini"])
    monkeypatch.setattr(core, "is_offline", lambda: False)
    monkeypatch.setattr(core, "PROVIDER", "gemini")
    monkeypatch.setattr(roles.S, "explicit_model_for", lambda role: "")
    monkeypatch.setattr(roles.S, "model_chain_for", lambda role, provider: ["openai/gpt-oss-20b"])

    with pytest.raises(RuntimeError, match="configured provider 'groq' has no key"):
        core.get_chat_model("brain")


def test_status_marks_missing_explicit_provider_as_unavailable_not_demo(monkeypatch):
    from types import SimpleNamespace

    import jarvis.llm as llm
    from jarvis.runtime import status

    monkeypatch.setattr(status.S, "is_offline", lambda: False)
    monkeypatch.setattr(status.S, "BRAIN_PROVIDER_CONFIG", "groq")
    monkeypatch.setattr(
        status.R, "get",
        lambda key, default=None: {"provider": "auto"}.get(key, default),
    )
    monkeypatch.setattr(llm, "health", lambda: {"active": "demo", "available": ["gemini"], "roles": {}})
    monkeypatch.setattr(
        llm, "resolve_role",
        lambda role, allow_discovery=False: SimpleNamespace(
            role=role, targets=[], reason="configured provider 'groq' has no key", describe=lambda: "(no model)"
        ),
    )

    result = status.agent_status(compact=True, surface="audit")
    assert result["status"] == "MODEL UNAVAILABLE"
    assert result["offline"] is False
    assert "no provider switch" in result["provider_status"]


def test_code_execution_is_not_a_read_permission():
    from jarvis.security.permissions import check, tool_level

    assert tool_level("code_engine") == "EXECUTE"
    assert tool_level("run_script") == "EXECUTE"
    assert tool_level("install_package") == "DANGEROUS"
    assert tool_level("list_skills") == "READ"
    assert tool_level("read_skill") == "READ"
    assert tool_level("process_stop") == "DANGEROUS"
    assert not check("code_engine", "telegram_guest").allowed
    assert not check("code_engine", "api").allowed
    assert check("code_engine", "voice_owner").allowed
    assert not check("unknown_tool", "unrecognized-surface").allowed


@pytest.mark.asyncio
async def test_code_engine_checks_surface_before_generating_or_running(monkeypatch):
    from jarvis.security.permissions import use_surface
    from jarvis.tools import code_engine as code_module

    called = False

    async def forbidden_generation(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("permission-denied request reached code generation")

    monkeypatch.setattr(code_module, "generate_code", forbidden_generation)
    with use_surface("telegram_guest"):
        result = await code_module.code_engine_run("make and run a script")

    assert result["ok"] is False
    assert "Permission blocked" in result["report"]
    assert called is False


@pytest.mark.asyncio
async def test_short_greeting_bypasses_graph_and_planner(monkeypatch):
    from jarvis.runtime import agent

    class StateStub:
        def add_task(self, *args, **kwargs):
            return None

    monkeypatch.setattr(agent, "get_state", lambda: StateStub())
    monkeypatch.setattr(agent, "_post_run", lambda *args, **kwargs: None)

    async def forbidden(*args, **kwargs):
        raise AssertionError("a trivial greeting should not start graph/app routing")

    monkeypatch.setattr(agent, "_direct_app_request", forbidden)
    result = await agent.run_agent("hello", session_id="fast-greeting-test", surface="cli_owner")
    assert result.status == "chat"
    assert result.answer


@pytest.mark.asyncio
async def test_answer_only_graph_skips_planner_critic_and_text_voice_polish(monkeypatch):
    from jarvis.graph import critic, routing, synthesis

    class MemoryStub:
        def start_task(self, *args, **kwargs):
            return "task-audit"

    class StateStub:
        def set_pending_steps(self, _steps):
            return None

    monkeypatch.setattr(routing, "get_memory", lambda: MemoryStub())
    monkeypatch.setattr(routing, "get_state", lambda: StateStub())
    monkeypatch.setattr(routing, "skills_prompt_block", lambda _request: "")

    def forbidden(*args, **kwargs):
        raise AssertionError("answer-only text should not invoke planner/critic/voice polish")

    monkeypatch.setattr(routing.dspy_mods, "plan", forbidden)
    plan = await routing.plan_node({"request": "what is two plus two?", "workers": ["answer"]})
    assert plan["plan"] == []
    assert "skipped" in plan["events"][0]["detail"]

    monkeypatch.setattr(critic.dspy_mods, "critic", forbidden)
    verdict = await critic.critic_node({
        "request": "what is two plus two?", "workers": ["answer"], "answer": "4", "errors": [],
    })
    assert verdict["critique"]["skipped"] == "answer-only"

    monkeypatch.setattr(synthesis, "is_offline", lambda: False)
    monkeypatch.setattr(synthesis, "PROVIDER", "gemini")
    monkeypatch.setattr(synthesis.dspy_mods, "voice_polish", forbidden)
    response = await synthesis.respond_node({"answer": "4", "surface": "api", "status": "done"})
    assert response["spoken"]
    assert response["status"] == "done"


@pytest.mark.asyncio
async def test_memory_persists_across_surfaces_and_isolated_by_user(tmp_path, monkeypatch):
    from jarvis.memory import session_state, store
    from jarvis.runtime import agent

    monkeypatch.setattr(store, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(session_state, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(agent, "_post_run", lambda *args, **kwargs: None)

    suffix = uuid.uuid4().hex
    owner_id = f"audit-owner-{suffix}"
    guest_id = f"audit-guest-{suffix}"
    marker = f"cross-mode-memory-{suffix}"

    saved = await agent.run_agent(
        f"yaad rakh {marker}", session_id="cli-audit", surface="cli_owner", memory_user_id=owner_id,
    )
    assert marker in saved.answer

    recalled = await agent.run_agent(
        "dikhao meri memory", session_id="voice-audit", surface="voice_owner", memory_user_id=owner_id,
    )
    assert marker in recalled.answer

    guest_view = await agent.run_agent(
        "dikhao meri memory", session_id="telegram-audit", surface="telegram_guest", memory_user_id=guest_id,
    )
    assert marker not in guest_view.answer


@pytest.mark.asyncio
async def test_recall_includes_recent_turn_for_a_follow_up_message(tmp_path, monkeypatch):
    from jarvis.graph import routing
    from jarvis.memory.store import MemoryStore

    memory = MemoryStore(tmp_path / "recall.sqlite3", user_id="audit-owner")
    memory.add_turn("My project name is NikhilAI", "Understood; I will use that project name.",
                    session="same-conversation")
    monkeypatch.setattr(routing, "get_memory", lambda: memory)
    monkeypatch.setattr(routing, "get_state", lambda: type("State", (), {"context_block": lambda self, **kw: ""})())

    context = await routing.recall_node({"request": "what project name did I just tell you?",
                                       "session_id": "same-conversation"})
    assert "My project name is NikhilAI" in context["memory_context"]
    assert "Understood; I will use that project name." in context["memory_context"]


@pytest.mark.asyncio
async def test_graph_persist_marks_turn_before_task_finalize_error(monkeypatch):
    from jarvis.graph import synthesis

    class MemoryStub:
        def __init__(self):
            self.turn_writes = 0

        def add_turn(self, *_args, **_kwargs):
            self.turn_writes += 1

        def finish_task(self, *_args, **_kwargs):
            raise OSError("simulated task-row failure")

    class StateStub:
        def finish_task(self, **_kwargs):
            return None

        def set_pending_steps(self, _steps):
            return None

    memory = MemoryStub()
    monkeypatch.setattr(synthesis, "get_memory", lambda: memory)
    monkeypatch.setattr(synthesis, "get_state", lambda: StateStub())
    result = await synthesis.persist_node({
        "request": "remember this turn", "answer": "saved", "session_id": "audit",
        "task_id": "task-1", "status": "done", "errors": [],
    })

    assert memory.turn_writes == 1
    assert result["memory_persisted"] is True
    assert any("task finalization" in error for error in result["errors"])


def test_runtime_setting_write_failure_is_reported_and_rolled_back(monkeypatch):
    from jarvis.config import runtime as R

    previous = R.get("mode", "text")
    attempted = "voice" if previous != "voice" else "text"
    monkeypatch.setattr(R, "_save", lambda: False)
    applied, errors = R.set_many(mode=attempted)

    assert applied == {}
    assert errors and "rolled back" in errors[-1]
    assert R.get("mode", "text") == previous


def test_runtime_settings_reload_when_another_process_updates_the_file(tmp_path, monkeypatch):
    from jarvis.config import runtime as R

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(R, "RUNTIME_SETTINGS_FILE", settings_file)
    monkeypatch.setattr(R, "_CACHE", {})
    monkeypatch.setattr(R, "_LOADED", False)
    monkeypatch.setattr(R, "_FILE_SIGNATURE", None)

    assert R.get("mode", "text") == "text"
    settings_file.write_text('{"mode": "voice"}', encoding="utf-8")
    assert R.get("mode", "text") == "voice"


@pytest.mark.asyncio
async def test_cli_oneshot_returns_nonzero_for_agent_failure(monkeypatch):
    from jarvis.automation import scheduler
    from jarvis.cli import app as cli_app
    from jarvis.runtime.agent import AgentResult

    monkeypatch.setattr(scheduler, "start_background", lambda: None)
    monkeypatch.setattr(scheduler, "RUNNER", scheduler.RUNNER)
    monkeypatch.setattr(cli_app, "_header_meta", lambda: {"interface": "CLI / TEXT"})
    monkeypatch.setattr(cli_app.ui, "answer_panel", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_app.ui, "error_panel", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_app.ui, "reasoning_panel", lambda *args, **kwargs: None)

    async def failed_result(*_args, **_kwargs):
        return AgentResult("open chrome", "could not open", errors=["window tool failed"], status="failed")

    monkeypatch.setattr(cli_app, "run_agent", failed_result)
    exit_code = await cli_app.run_cli(once="open chrome", show_reasoning=False)
    assert exit_code == 1


def test_api_auth_is_optional_but_token_compare_handles_non_ascii(monkeypatch, capsys):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    import jarvis.api.server as server
    from jarvis.config import settings as settings_module

    monkeypatch.setattr(server, "API_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "API_AUTH_TOKEN", "audit-only-token")
    client = TestClient(server.app)

    assert client.get("/openapi.json").status_code == 401
    assert client.post("/auth", json={"token": "wrong"}).status_code == 401
    non_ascii = client.post("/auth", json={"token": "tökèn-不是-token"})
    assert non_ascii.status_code == 401
    unlocked = client.post("/auth", json={"token": "audit-only-token"})
    assert unlocked.status_code == 200
    assert "httponly" in unlocked.headers.get("set-cookie", "").lower()
    assert client.get("/openapi.json").status_code == 200
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/openapi.json").status_code == 401

    # Without an explicitly configured token, the API stays open as requested; a public bind warns.
    monkeypatch.setattr(server, "API_AUTH_TOKEN", "")
    monkeypatch.setattr(server, "API_HOST", "0.0.0.0")
    monkeypatch.setattr(settings_module, "API_AUTH_TOKEN", "")
    monkeypatch.delenv("NIKHILAI_API_BIND_WARNING_SHOWN", raising=False)
    exposed = TestClient(server.app)
    assert exposed.get("/openapi.json").status_code == 200
    assert "non-loopback API bind without API_AUTH_TOKEN" in capsys.readouterr().err


def test_api_bind_detection_handles_loopback_ipv4_and_ipv6():
    pytest.importorskip("fastapi")
    from starlette.requests import Request

    import jarvis.api.server as server

    def request_for(host):
        return Request({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": "/", "raw_path": b"/",
            "query_string": b"", "headers": [], "client": ("127.0.0.1", 12345),
            "server": (host, 8000),
        })

    assert not server._network_exposed(request_for("127.0.0.1"))
    assert not server._host_is_network_exposed("::ffff:127.0.0.1")
    assert server._network_exposed(request_for("0.0.0.0"))
    assert server._network_exposed(request_for("::"))
    assert server._network_exposed(request_for("public.example"))


@pytest.mark.asyncio
async def test_telegram_mode_voice_persists_preference_and_reports_save_failure(monkeypatch):
    pytest.importorskip("telegram")
    from types import SimpleNamespace

    from telegram.ext import CommandHandler

    from jarvis.config import runtime as runtime_settings
    from jarvis.security import gate
    from jarvis.telegram import bot

    monkeypatch.setattr(gate, "_surface_handlers", dict(gate._surface_handlers))
    monkeypatch.setattr(gate, "_completion_handlers", dict(gate._completion_handlers))
    owner_id = 734_112
    monkeypatch.setattr(bot.S, "TELEGRAM_BOT_TOKEN", "123456789:" + "x" * 35)
    monkeypatch.setattr(bot.S, "TELEGRAM_OWNER_ID", str(owner_id))
    monkeypatch.setattr(bot.S, "TELEGRAM_ALLOWED_CHAT_IDS", [])
    monkeypatch.setattr(bot.S, "TELEGRAM_ALLOWED_USERS", [])
    monkeypatch.setattr(runtime_settings, "_CACHE", {"mode": "text"})
    monkeypatch.setattr(runtime_settings, "_LOADED", True)
    monkeypatch.setattr(runtime_settings, "_save", lambda: True)

    application = bot.build_application()
    handler = next(
        h for group in application.handlers.values() for h in group
        if isinstance(h, CommandHandler) and "mode" in h.commands
    )

    class MessageStub:
        def __init__(self):
            self.responses = []

        async def reply_text(self, text, **kwargs):
            self.responses.append(str(text))

    message = MessageStub()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=owner_id),
        effective_chat=SimpleNamespace(id=owner_id),
        effective_message=message,
        message=message,
    )
    await handler.callback(update, SimpleNamespace(args=["voice"]))
    assert runtime_settings.get("mode") == "voice"
    assert "successfully saved" in message.responses[-1]
    assert "mic/LiveKit connection start nahi hua" in message.responses[-1]

    monkeypatch.setattr(runtime_settings, "_save", lambda: False)
    await handler.callback(update, SimpleNamespace(args=["text"]))
    assert runtime_settings.get("mode") == "voice"
    assert "save nahi hui" in message.responses[-1]


@pytest.mark.asyncio
async def test_app_open_focuses_existing_window_without_launch(monkeypatch):
    from jarvis.tools import app_finder

    app = {"name": "Google Chrome", "norm": "google chrome", "path": "/usr/bin/chrome",
           "kind": "path", "extra": "", "score": 100}
    monkeypatch.setattr(app_finder, "find_apps", lambda query, limit=8: [app])

    async def already_focused(_name):
        return True

    monkeypatch.setattr(app_finder, "focus_window", already_focused)
    monkeypatch.setattr(app_finder, "launch_app", lambda _app: pytest.fail("duplicate app launch"))
    result = await app_finder.open_any_app.ainvoke({"query": "chrome"})

    assert "Naya instance nahi khola" in result


@pytest.mark.asyncio
async def test_system_open_app_reuses_existing_window_without_launch(monkeypatch):
    from jarvis.tools import app_finder, system_tools

    app = {"name": "Google Chrome", "norm": "google chrome", "path": "/usr/bin/chrome",
           "kind": "path", "extra": "", "score": 100}
    monkeypatch.setattr(app_finder._APP_MEMORY, "lookup", lambda _name: None)
    monkeypatch.setattr(app_finder, "find_apps", lambda _query, limit=8: [app])

    async def already_open(_title):
        return True

    monkeypatch.setattr(app_finder, "focus_window", already_open)
    monkeypatch.setattr(
        app_finder, "launch_app", lambda _target: pytest.fail("existing app window must not be launched")
    )
    result = await system_tools.open_app.ainvoke({"app_title": "chrome"})

    assert "pehle se open" in result
    assert "Naya instance nahi khola" in result


@pytest.mark.asyncio
async def test_app_open_skips_launch_when_existing_window_cannot_be_focused(monkeypatch):
    from jarvis.tools import app_finder

    app = {"name": "Google Chrome", "norm": "google chrome", "path": "/usr/bin/chrome",
           "kind": "path", "extra": "", "score": 100}
    monkeypatch.setattr(app_finder, "find_apps", lambda query, limit=8: [app])

    async def not_focused(_name):
        return False

    async def exists(_name):
        return True

    monkeypatch.setattr(app_finder, "focus_window", not_focused)
    monkeypatch.setattr(app_finder, "window_exists", exists)
    monkeypatch.setattr(app_finder, "launch_app", lambda _app: pytest.fail("duplicate app launch"))
    result = await app_finder.open_any_app.ainvoke({"query": "chrome"})

    assert "Duplicate launch se bachne" in result
