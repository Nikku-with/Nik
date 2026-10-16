from __future__ import annotations


def test_doctor_reports_exact_voice_dependencies_and_missing_keys(monkeypatch):
    from jarvis.cli import doctor
    from jarvis.config import settings as S
    from jarvis.voice import livekit_agent as voice

    for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GEMINI_API_KEY"):
        monkeypatch.setattr(S, name, "")
    monkeypatch.setattr(voice, "_module_available", lambda _name: False)
    checks = doctor.collect_checks()
    rows = {label: (status, detail) for status, label, detail in checks}

    assert rows["Voice dependency: livekit-agents[google]"][0] == "warn"
    assert "pip install livekit-agents[google]" in rows["Voice dependency: livekit-agents[google]"][1]
    assert "missing GEMINI_API_KEY" in rows["Gemini Live voice key"][1]
    assert rows["Voice local console readiness"][0] == "warn"
    assert "LIVEKIT_URL" in rows["Voice LiveKit Cloud readiness"][1]


def test_doctor_table_keeps_full_dependency_and_configuration_details():
    from io import StringIO

    from rich.console import Console

    from jarvis.cli import ui

    output = StringIO()
    previous = ui.console
    ui.console = Console(file=output, width=140, force_terminal=False)
    detail = (
        "missing — pip install livekit-agents[google]; pip install livekit-plugins-google; "
        "pip install livekit-plugins-silero; missing GEMINI_API_KEY; LIVEKIT_URL; "
        "LIVEKIT_API_KEY; LIVEKIT_API_SECRET"
    )
    try:
        ui.doctor_table([("warn", "Voice LiveKit Cloud readiness", detail)])
    finally:
        ui.console = previous

    rendered = output.getvalue()
    for expected in ("livekit-agents[google]", "livekit-plugins-silero", "GEMINI_API_KEY",
                     "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        assert expected in rendered
