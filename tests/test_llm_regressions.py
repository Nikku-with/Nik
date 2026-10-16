from __future__ import annotations

import sys
from types import SimpleNamespace


def test_google_genai_api_key_is_used_by_the_gemini_provider(monkeypatch):
    from jarvis.llm import providers

    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "fake-google-genai-key")

    assert providers.api_key("gemini") == "fake-google-genai-key"
    assert "gemini" in providers.available()


def test_ollama_client_receives_the_configured_http_timeout(monkeypatch):
    from jarvis.llm import providers

    captured = {}

    class FakeChatOllama:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = SimpleNamespace(ChatOllama=FakeChatOllama)
    monkeypatch.setitem(sys.modules, "langchain_ollama", fake_module)

    providers.build_model("ollama", "qwen2.5:7b", timeout=37, retries=2)

    assert captured["client_kwargs"]["timeout"] == 37


def test_demo_note_intent_emits_the_registered_save_note_tool_call():
    from jarvis.llm.demo import DemoChatModel

    save_note = SimpleNamespace(name="save_note")
    model = DemoChatModel().bind_tools([save_note])

    response = model.invoke("note save karo: grocery list - milk and eggs")

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0]["name"] == "save_note"
    assert response.tool_calls[0]["args"]["name"]
    assert "milk and eggs" in response.tool_calls[0]["args"]["content"]


def test_explicit_provider_does_not_add_other_provider_fallbacks(monkeypatch):
    from jarvis.llm import roles

    def fake_get(key, default=None):
        return {"provider": "auto", "model": None, "temperature": None}.get(key, default)

    monkeypatch.setattr(roles.R, "get", fake_get)
    monkeypatch.setattr(roles.S, "BRAIN_PROVIDER_CONFIG", "gemini")
    monkeypatch.setattr(roles.P, "is_available", lambda provider: provider in {"gemini", "groq"})
    monkeypatch.setattr(roles.S, "explicit_model_for", lambda _role: "")
    monkeypatch.setattr(
        roles.S, "model_chain_for",
        lambda _role, provider: ["gemini-3.8-flash"] if provider == "gemini" else [],
    )
    monkeypatch.setattr(
        roles.S, "provider_model_defaults",
        lambda _role, provider: {
            "gemini": ["gemini-3.8-flash"],
            "groq": ["openai/gpt-oss-20b"],
        }.get(provider, []),
    )
    monkeypatch.setattr(roles.S, "MAX_MODEL_TARGETS", 4)

    route = roles.resolve("brain", allow_discovery=False)

    assert route.targets
    assert {target.provider for target in route.targets} == {"gemini"}
