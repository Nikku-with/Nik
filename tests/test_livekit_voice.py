from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from jarvis.security import gate
from jarvis.voice import livekit_agent as voice


class FakeVoiceSession:
    def __init__(self):
        self.transcription_listener = None
        self.prompts = []
        self.interruptible = []

    def on(self, event_name, listener):
        assert event_name == "user_input_transcribed"
        self.transcription_listener = listener

    async def say(self, text, allow_interruptions=False):
        self.prompts.append(text)
        self.interruptible.append(allow_interruptions)
        if "Haan ya nahi" in text and self.transcription_listener:
            self.transcription_listener(SimpleNamespace(transcript="haan", is_final=True))


@pytest.mark.asyncio
async def test_sync_voice_bridge_runs_safely_inside_an_existing_event_loop():
    import asyncio

    from jarvis.voice.livekit_agent import _run_sync

    async def inside_loop():
        return _run_sync(asyncio.sleep(0, result="voice-started"))

    assert await inside_loop() == "voice-started"


async def test_spoken_confirmation_requires_clear_yes_no_or_cancels():
    session = FakeVoiceSession()
    queue: asyncio.Queue[str] = asyncio.Queue()
    request = SimpleNamespace(command="rm -rf /tmp/x", why_flagged="Recursive delete")
    task = asyncio.create_task(voice.spoken_confirmation(session, queue, request, timeout_s=0.2))
    await asyncio.sleep(0)
    queue.put_nowait("haan chalao")
    assert await task is True
    assert session.interruptible == [True]

    queue.put_nowait("previous stale yes")
    session2 = FakeVoiceSession()
    no_task = asyncio.create_task(voice.spoken_confirmation(session2, queue, request, timeout_s=0.2))
    await asyncio.sleep(0)
    queue.put_nowait("maybe")
    assert await no_task is False

    timeout = await voice.spoken_confirmation(FakeVoiceSession(), asyncio.Queue(), request, timeout_s=0.02)
    assert timeout is False


def test_livekit_fake_room_tool_turn_uses_shared_agent_and_spoken_gate(monkeypatch):
    captured: dict[str, object] = {}
    sessions: list[FakeVoiceSession] = []

    class FakeAgent:
        def __init__(self, instructions, tools):
            self.instructions = instructions
            self.tools = tools
            captured["agent"] = self

    class FakeRealtimeModel:
        def __init__(self, **kwargs):
            captured["llm_options"] = kwargs

    class FakeAgentSession(FakeVoiceSession):
        def __init__(self, llm=None):
            super().__init__()
            self.llm = llm
            sessions.append(self)

        async def start(self, room, agent):
            captured["room"] = room
            captured["agent"] = agent

        async def generate_reply(self, instructions):
            captured["greeting"] = instructions

    class FakeRunContext:
        pass

    class FakeWorkerOptions:
        def __init__(self, entrypoint_fnc):
            self.entrypoint_fnc = entrypoint_fnc

    agents_mod = types.ModuleType("livekit.agents")
    agents_mod.Agent = FakeAgent
    agents_mod.AgentSession = FakeAgentSession
    agents_mod.RunContext = FakeRunContext
    agents_mod.function_tool = lambda fn: fn
    agents_mod.WorkerOptions = FakeWorkerOptions

    async def fake_run_app(options):
        class FakeRoom:
            name = "fake-room-7"

        class FakeJobContext:
            room = FakeRoom()

            async def connect(self):
                captured["connected"] = True

        await options.entrypoint_fnc(FakeJobContext())
        agent = captured["agent"]
        # The second registered realtime tool is agent_task.
        captured["turn_result"] = await agent.tools[1](FakeRunContext(), "delete temp folder")
        return 0

    agents_mod.cli = SimpleNamespace(run_app=lambda options: asyncio.run(fake_run_app(options)))
    livekit_pkg = types.ModuleType("livekit")
    livekit_pkg.__path__ = []
    livekit_pkg.agents = agents_mod
    plugins_pkg = types.ModuleType("livekit.plugins")
    plugins_pkg.__path__ = []
    google_pkg = types.ModuleType("livekit.plugins.google")
    google_pkg.realtime = SimpleNamespace(RealtimeModel=FakeRealtimeModel)
    plugins_pkg.google = google_pkg

    monkeypatch.setitem(sys.modules, "livekit", livekit_pkg)
    monkeypatch.setitem(sys.modules, "livekit.agents", agents_mod)
    monkeypatch.setitem(sys.modules, "livekit.plugins", plugins_pkg)
    monkeypatch.setitem(sys.modules, "livekit.plugins.google", google_pkg)
    monkeypatch.setattr(gate, "BROKER", gate.ApprovalBroker(owner_id="", ttl_s=0.3))
    from jarvis.automation import scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "configure_agent_runner", lambda: None)
    monkeypatch.setattr(scheduler_module, "start_background", lambda: False)
    monkeypatch.setattr(scheduler_module, "stop_background", lambda: None)
    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    calls = []

    async def fake_shared_agent(request, session_id, surface, voice_output):
        calls.append((request, session_id, surface, voice_output))
        approval = await gate.authorize_command(
            "rm -rf /tmp/fake-voice", "/tmp", surface=surface,
        )
        return SimpleNamespace(
            answer="Approved command completed.", spoken="Approved command completed.",
            workers=["desktop"], reasoning=[], artifacts=[], errors=[] if approval.allowed else [approval.reason],
            status="done" if approval.allowed else "cancelled", verified_execution=approval.allowed,
            memory_persisted=True, blocked_tools=[], seconds=0.1,
        )

    monkeypatch.setattr(voice, "run_agent", fake_shared_agent)
    assert voice.build_livekit_agent_runner(["console"]) == 0
    assert captured["connected"] is True
    assert captured["room"].name == "fake-room-7"
    assert captured["llm_options"]["model"] == voice.voice_model()
    assert calls and calls[0][2] == "voice_owner"
    assert calls[0][1].startswith("voice:fake-room-7:")
    assert "completed" in str(captured["turn_result"])
    assert sessions and sessions[0].prompts
    assert sessions[0].interruptible == [True]


def test_spoken_confirmation_never_treats_unclear_words_as_yes():
    assert voice._yes_no("haan") is True
    assert voice._yes_no("nahi") is False
    assert voice._yes_no("maybe later") is None
