from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis.cli import app
from jarvis.runtime.progress import ProgressEvent


def test_trace_report_measures_spans_without_exposing_request_or_output_text():
    events = [
        ProgressEvent("start", text="PRIVATE REQUEST: synthetic secret phrase"),
        ProgressEvent("plan", text="PRIVATE PLAN", detail="do not expose this plan"),
        ProgressEvent("model_start", text="prompt contains secret", tool="brain/chat"),
        ProgressEvent("model_end", text="model output contains secret", tool="brain/chat", status="ok", ms=34),
        ProgressEvent("tool_start", text="private path /tmp/secret", tool="system_info", status="running"),
        ProgressEvent("tool_end", text="private tool output", tool="system_info", status="ok", ms=12),
        ProgressEvent("thinking", text="hidden reasoning must not appear"),
    ]
    result = SimpleNamespace(status="chat", seconds=0.110)

    report = app._build_trace_report(events, result)
    joined = "\n".join(report)

    assert report[0] == "Last run: status=chat wall=110ms"
    assert "models=1/34ms tools=1/12ms" in report[1]
    assert "brain/chat" in joined and "system_info" in joined
    assert "PRIVATE" not in joined
    assert "secret" not in joined
    assert "hidden reasoning" not in joined


@pytest.mark.asyncio
async def test_trace_command_shows_summary_and_only_the_requested_recent_events(monkeypatch):
    monkeypatch.setattr(app, "_LAST_TRACE", [
        "Last run: status=done wall=50ms",
        "Spans: models=1/20ms tools=1/0ms (operation sums may overlap)",
        "12:00:00 start",
        "12:00:00 model_end brain/chat status=ok 20ms",
        "12:00:00 done status=done",
    ])
    output: list[str] = []
    monkeypatch.setattr(app.ui, "cprint_safe", lambda text, *_args, **_kwargs: output.append(str(text)))

    keep_running = await app.handle_command("/trace 1", "trace-test")

    assert keep_running is True
    assert output == app._LAST_TRACE[:2] + app._LAST_TRACE[-1:]


@pytest.mark.asyncio
async def test_cli_captures_a_sanitized_trace_for_a_completed_request(monkeypatch):
    from contextlib import nullcontext

    from jarvis.automation import scheduler
    from jarvis.cli import pro_ui

    monkeypatch.setattr(scheduler, "RUNNER", None)
    monkeypatch.setattr(scheduler, "register_delivery_handler", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "start_background", lambda: None)
    monkeypatch.setattr(app, "_header_meta", lambda: {})
    monkeypatch.setattr(pro_ui, "live_run", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(app.ui, "answer_panel", lambda *_args, **_kwargs: None)

    async def fake_run_agent(_text, *, session_id, on_progress):
        on_progress(ProgressEvent("start", text="PRIVATE REQUEST CONTENT"))
        on_progress(ProgressEvent("model_end", text="PRIVATE MODEL OUTPUT", tool="brain/chat",
                                  status="ok", ms=4))
        on_progress(ProgressEvent("done", text="PRIVATE RESULT", status="chat"))
        return SimpleNamespace(answer="ok", status="chat", seconds=0.01, workers=["chat"],
                               errors=[], execution_required=False, verified_execution=False,
                               reasoning=[], artifacts=[])

    monkeypatch.setattr(app, "run_agent", fake_run_agent)
    exit_code = await app.run_cli(once="hello", show_reasoning=False)

    assert exit_code == 0
    assert app._LAST_TRACE[0] == "Last run: status=chat wall=10ms"
    assert "brain/chat" in "\n".join(app._LAST_TRACE)
    assert "PRIVATE" not in "\n".join(app._LAST_TRACE)


@pytest.mark.asyncio
async def test_trace_command_rejects_invalid_limits_without_touching_the_saved_trace(monkeypatch):
    monkeypatch.setattr(app, "_LAST_TRACE", ["Last run: status=done wall=1ms", "Spans: models=0/0ms tools=0/0ms"])
    output: list[str] = []
    monkeypatch.setattr(app.ui, "cprint", lambda text, *_args, **_kwargs: output.append(str(text)))

    await app.handle_command("/trace 41", "trace-test")

    assert output and "Usage: /trace" in output[0]
    assert app._LAST_TRACE[0].startswith("Last run:")
