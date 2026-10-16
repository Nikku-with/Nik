from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

CASUAL_INPUTS = [
    "I'm feeling a bit tired today.",
    "What do you think about AI art?",
    "That was a good joke.",
    "I had a really long day.",
    "Tell me something interesting.",
    "Why is the sky blue?",
    "Explain why cats purr.",
    "Do you like cricket?",
    "I think chai is underrated.",
    "That made me laugh.",
    "How are you holding up?",
    "Can you tell me a fun fact?",
    "I'm nervous about tomorrow.",
    "No questions, just chat with me.",
    "Mat poocho, bas baat karo.",
    "What makes a good teammate?",
    "I'm bored.",
    "I need a little motivation.",
    "I love old Bollywood songs.",
    "Can you explain photosynthesis?",
    "Kya scene hai?",
    "Thanks for listening.",
    "I finally finished that book.",
    "What is your take on remote work?",
    "I'm excited about the weekend.",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", CASUAL_INPUTS)
async def test_casual_chat_uses_one_llm_call_without_tools_or_graph(text, monkeypatch):
    from types import SimpleNamespace

    import jarvis.llm as llm
    from jarvis.memory import manager as memory_manager
    from jarvis.memory import store as memory_store
    from jarvis.runtime import agent

    class StateStub:
        def add_task(self, *_args, **_kwargs):
            return None

    class MemoryStub:
        user_id = "nikhil_mehra"

        def recent(self, **_kwargs):
            return []

        def search(self, *_args, **_kwargs):
            return []

        def relevant_context(self, *_args, **_kwargs):
            return "(no relevant saved memory)"

    class FakeModel:
        calls = 0
        messages = None

        async def ainvoke(self, messages):
            self.calls += 1
            self.messages = messages
            return SimpleNamespace(content="Samajh gaya—seedhi baat, bina extra lecture ke.")

    model = FakeModel()
    monkeypatch.setattr(agent, "get_state", lambda: StateStub())
    monkeypatch.setattr(agent, "_post_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent, "_emit_final_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(memory_store, "get_memory", lambda **_kwargs: MemoryStub())
    monkeypatch.setattr(memory_manager, "get_memory", lambda **_kwargs: MemoryStub())
    monkeypatch.setattr(llm, "get_chat_model", lambda *_args, **_kwargs: model)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("chat must not inspect desktop tools or start task routing")

    monkeypatch.setattr(agent, "_direct_app_request", forbidden)
    result = await agent.run_agent(text, session_id="casual-chat-test", surface="cli_owner")

    assert result.status == "chat"
    assert result.tool_evidence == []
    assert result.events[0]["llm_calls"] == 1
    assert result.events[0]["tools"] == 0
    assert result.events[0]["planner"] == "skipped"
    assert result.events[0]["critic"] == "skipped"
    assert model.calls == 1
    prompt_text = "\n".join(str(getattr(message, "content", "")) for message in model.messages)
    assert "LOCAL CLOCK" not in prompt_text
    assert "Asia/Kolkata" not in prompt_text
    assert "Kota" not in prompt_text
    assert "weather: " not in prompt_text.lower()


TASK_ROUTING_INPUTS = [
    "notepad open krke hello likho",
    "jarvis notepad open karke hello likho",
    "desktop pe ek folder banao",
    "mere desktop me notes.txt me meeting likh do",
    "notepad kholo",
    "chrome khol",
    "notepad open karo",
    "notepad kholke hello likho",
    "notes.txt open karo",
    "folder bana do desktop par",
    "mere desktop pe folder bana do",
    "notes me meeting likh do",
    "file me todo likh ke save karo",
    "hello likho in notepad",
    "notepad me hello type karo",
    "chrome chalao",
    "folder hatao",
    "file delete kar do",
    "report download karo",
    "python install kar do",
    "script run karo",
    "ye command chalao",
    "web par search karo",
    "internet pe dhundo",
    "file dhundo",
    "notes.txt dhoondo",
    "note bhejo",
    "message bhej do",
    "make a folder on desktop",
    "create a folder on desktop",
    "open Notepad and type hello",
    "Open Chrome",
    "Search the web for today's weather",
    "Download the report",
    "Install Python",
    "Run the test suite",
    "Write hello to notes.txt",
    "Save this as a file",
    "Delete the old file",
    "Find my folder on desktop",
    "Can you open Notepad?",
    "Could you create a folder?",
    "Please send this message",
    "Schedule a reminder",
    "Take a screenshot",
    "Click the save button",
    "Move file to desktop",
    "Rename notes.txt",
    "Read this file",
    "bhai file hata do",
    "notes.txt me meeting likh ke save kar do",
    "desktop se report download karo",
    "Chrome khol kar website search karo",
    "folder banao aur notes.txt likho",
    "please run this script",
    "find my notes.txt",
    "delete the folder",
    "notepad kholo aur mera naam likho",
    "khol ke file me likh do",
]

CHAT_ROUTING_INPUTS = [
    *CASUAL_INPUTS,
    "Hi Jarvis",
    "Good morning",
    "Thanks!",
    "Shukriya, that helped",
    "What do you think about AI?",
    "What do you think about open source?",
    "What's your opinion on remote work?",
    "Do you like cricket?",
    "How are you?",
    "I'm happy today",
    "I feel a bit anxious",
    "I am sad",
    "I'm proud of finishing that book",
    "That joke was funny",
    "I had a long day",
    "I'm bored",
    "I need some encouragement",
    "Why is the sky blue?",
    "What is photosynthesis?",
    "Who wrote Hamlet?",
    "Can you tell me a fun fact?",
    "Explain why cats purr",
    "Tell me something interesting",
    "How do solar panels work?",
    "What makes a good teammate?",
    "Is chai better than coffee?",
    "Kya haal hai?",
    "Kya scene hai?",
    "Bas mujhse baat karo",
    "Mat poocho, bas baat karo.",
    "Mujhe chai pasand hai",
    "Aaj mood thoda off hai",
    "Mujhe motivation chahiye",
    "Thanks for listening",
    "I love old Bollywood songs",
    "What is your take on remote work?",
    "What are you doing today?",
    "What's happening?",
    "What is the meaning of folder?",
    "What is a desktop environment?",
    "What is a file?",
    "How can I open Notepad?",
    "Tell me how to install Python",
    "How do I create a folder?",
    "What do you think about folders?",
    "Do you like Notepad?",
    "I was thinking about Chrome",
    "My day was exhausting",
    "Namaste",
    "Hello Jarvis",
    "You made me laugh",
    "Can you explain photosynthesis?",
    "Tell me why people enjoy cricket",
    "What should I do when I feel tired?",
    "How was your day?",
    "I'm feeling calm today",
    "Thanks for being here",
    "What does a file extension mean?",
]


def test_rule_first_classifier_defaults_to_task_for_english_and_hinglish_actions():
    from jarvis.runtime.agent import is_chat_request

    assert len(TASK_ROUTING_INPUTS) >= 40
    misrouted = [text for text in TASK_ROUTING_INPUTS if is_chat_request(text)]
    assert not misrouted, f"These action requests were misclassified as chat: {misrouted}"


def test_rule_first_classifier_keeps_clear_conversation_on_chat_path():
    from jarvis.runtime.agent import is_chat_request

    assert len(CHAT_ROUTING_INPUTS) >= 40
    misrouted = [text for text in CHAT_ROUTING_INPUTS if not is_chat_request(text)]
    assert not misrouted, f"These conversational inputs were misclassified as tasks: {misrouted}"


def test_explicit_time_answers_are_direct_and_default_context_is_time_free():
    from jarvis.config import TIMEZONE
    from jarvis.graph.state import evidence_digest
    from jarvis.prompts import chat_system_prompt, supervisor_prompt, voice_instructions
    from jarvis.runtime.agent import _is_time_question, _time_answer

    assert _is_time_question("What day is it today?")
    assert _is_time_question("Abhi kitne baje hain?")
    assert _is_time_question("Aaj ki date batao")
    assert not _is_time_question("I'm having a good day")
    assert not _is_time_question("What are you doing today?")

    now = datetime.now(ZoneInfo(TIMEZONE))
    date_answer = _time_answer("What is today's date?")
    time_answer = _time_answer("What time is it?")
    assert now.strftime("%d %B %Y") in date_answer
    assert now.strftime("%I:%M %p") in time_answer
    assert now.strftime("%d %B %Y") not in time_answer

    defaults = "\n".join((supervisor_prompt(), voice_instructions(), chat_system_prompt()))
    assert "LOCAL CLOCK" not in defaults
    assert "Kota" not in defaults
    assert "Weather:" not in defaults
    assert now.strftime("%d %B %Y") not in defaults

    assert "LOCAL CLOCK" not in evidence_digest({"request": "How are you today?", "findings": []})
    assert "LOCAL CLOCK" in evidence_digest({"request": "Remind me tomorrow at 8", "findings": []})


def _install_direct_chat_fakes(monkeypatch, *, recent_turns=(), response_text="Short, supportive reply."):
    from types import SimpleNamespace

    import jarvis.llm as llm
    from jarvis.memory import store as memory_store

    class MemoryStub:
        def recent(self, **_kwargs):
            return list(recent_turns)

        def relevant_context(self, *_args, **_kwargs):
            return "(no relevant saved memory)"

    class FakeModel:
        calls = 0
        messages = None

        async def ainvoke(self, messages):
            self.calls += 1
            self.messages = messages
            return SimpleNamespace(content=response_text)

    model = FakeModel()
    monkeypatch.setattr(memory_store, "get_memory", lambda: MemoryStub())
    monkeypatch.setattr(llm, "get_chat_model", lambda *_args, **_kwargs: model)
    return model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("assistant_replies", "follow_up_allowed"),
    [
        (["Would a short checklist help?"], False),
        (["Would a short checklist help?", "Let's keep it simple.", "That makes sense.",
          "We can take it one step at a time."], True),
        (["Let's keep it simple.", "Anything else you want to add?", "That makes sense.",
          "We can take it one step at a time."], False),
    ],
)
async def test_chat_fast_path_rate_limits_follow_up_curiosity(
    monkeypatch, assistant_replies, follow_up_allowed,
):
    from jarvis.runtime.agent import _chat_fast_path

    recent_turns = [{"role": "assistant", "content": text} for text in assistant_replies]
    model = _install_direct_chat_fakes(monkeypatch, recent_turns=recent_turns)
    result = await _chat_fast_path("I'm a little tired today.", "curiosity-test", "cli_owner", lambda *_a, **_k: None)

    prompt = str(model.messages[0].content)
    if follow_up_allowed:
        assert "Ek chhota relevant follow-up kabhi-kabhi pooch sakte ho." in prompt
    else:
        assert "Follow-up sawaal mat poochho; seedha jawab do." in prompt
    assert model.calls == 1
    assert result.status == "chat"


@pytest.mark.asyncio
async def test_chat_fast_path_uses_configured_language_and_matches_user_mood(monkeypatch):
    from jarvis.config import runtime
    from jarvis.runtime.agent import _chat_fast_path

    monkeypatch.setattr(
        runtime, "get",
        lambda key, default=None: {
            "language": "Hindi",
            "personality": "calm and encouraging",
        }.get(key, default),
    )
    model = _install_direct_chat_fakes(
        monkeypatch,
        response_text="Ghabrahat hona samajh mein aata hai. Hum aaram se ek-ek kadam lenge.",
    )
    result = await _chat_fast_path(
        "I'm nervous about tomorrow; please reassure me.",
        "language-mood-test", "cli_owner", lambda *_a, **_k: None,
    )

    prompt = str(model.messages[0].content)
    assert "Language: Hindi." in prompt
    assert "Personality: calm and encouraging." in prompt
    assert "Reply in the user's language and match their mood." in prompt
    assert "Ghabrahat hona samajh mein aata hai" in result.answer
    assert model.calls == 1


@pytest.mark.asyncio
async def test_mat_poocho_overrides_curiosity_and_gets_a_no_question_reply(monkeypatch):
    from jarvis.runtime.agent import _chat_fast_path

    model = _install_direct_chat_fakes(
        monkeypatch,
        response_text="Bilkul. Main yahin hoon aur aapki baat sun raha hoon.",
    )
    result = await _chat_fast_path(
        "Mat poocho, bas baat karo.", "no-follow-up-test", "cli_owner", lambda *_a, **_k: None,
    )

    prompt = str(model.messages[0].content)
    assert "Follow-up sawaal mat poochho; seedha jawab do." in prompt
    assert "Respect \"mat poocho\" / \"don't ask\" absolutely." in prompt
    assert "?" not in result.answer
    assert model.calls == 1
