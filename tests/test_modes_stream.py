"""
tests/test_modes_stream.py — uniform modes + visible thinking (spec sections 6, 7)
==================================================================================
  pytest tests/test_modes_stream.py
  python tests/test_modes_stream.py

No LLM / no network (offline demo graph). Covers: mode<->surface map,
mode override in run_agent, stream_agent gate wrap, progress events inside
jarvis_stream, and /stream SSE end-to-end (thinking feed reaches web).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JARVIS_OFFLINE_MODE", "1")

from jarvis.config import runtime as R  # noqa: E402
from jarvis.graph import build as GB  # noqa: E402
from jarvis.runtime import agent as AG  # noqa: E402
from jarvis.runtime import modes as M  # noqa: E402
from jarvis.security import gate as G  # noqa: E402

try:
    import pytest  # type: ignore

    @pytest.fixture(autouse=True)
    def _auto_isolation():
        G.reset_broker_for_tests()
        yield
        G.reset_broker_for_tests()
except Exception:  # pragma: no cover
    pass


# ---------------------------------------------------------------------------
# 1) mode <-> surface map
# ---------------------------------------------------------------------------
def test_surface_for_mode_matrix():
    assert M.surface_for_mode("text") == "cli_owner"
    assert M.surface_for_mode("voice") == "voice_owner"
    assert M.surface_for_mode("telegram") == "telegram_owner"
    assert M.surface_for_mode("telegram", "guest") == "telegram_guest"
    assert M.surface_for_mode("telegram", "owner") == "telegram_owner"
    # guests sirf telegram par — voice/text par role irrelevant
    assert M.surface_for_mode("voice", "guest") == "voice_owner"
    assert M.surface_for_mode("text", "guest") == "cli_owner"


def test_normalize_mode_unknown_to_text():
    assert M.normalize_mode(None) == "text"
    assert M.normalize_mode("") == "text"
    assert M.normalize_mode("VOICE") == "voice"
    assert M.normalize_mode("radio") == "text"
    assert M.surface_for_mode("radio") == "cli_owner"


def test_current_and_set_mode_roundtrip():
    before = R.get("mode", "text")
    try:
        applied, errors = M.set_mode("voice")
        assert not errors and applied.get("mode") == "voice"
        assert M.current_mode() == "voice"
        assert "voice" in M.describe_mode().lower()
        assert "voice" in M.mode_entry_hint().lower()
        assert "jarvis" in M.mode_entry_hint("telegram")
        assert "jarvis" in M.mode_entry_hint("text")
    finally:
        R.set_many(mode=before)


# ---------------------------------------------------------------------------
# 2) run_agent mode override (fast-path — no LLM)
# ---------------------------------------------------------------------------
def test_run_agent_mode_override():
    async def body():
        r1 = await AG.run_agent("time kya hai", session_id="t-mode-1", mode="voice")
        assert r1.surface == "voice_owner", r1.surface
        assert r1.status == "time" and "🕒" in r1.answer
        r2 = await AG.run_agent("time kya hai", session_id="t-mode-2", mode="telegram")
        assert r2.surface == "telegram_owner", r2.surface
        # mode nahi diya → surface param jaisa hai waisa
        r3 = await AG.run_agent("time kya hai", session_id="t-mode-3", surface="api")
        assert r3.surface == "api", r3.surface

    asyncio.run(body())


# ---------------------------------------------------------------------------
# 3) stream_agent gate wrap (fake stream se surface assert)
# ---------------------------------------------------------------------------
def test_stream_agent_runs_under_surface_gate():
    async def body():
        seen: list[str] = []

        async def fake_stream(text, session_id="default", thread_id=None):
            seen.append(G.current_surface())
            yield {"node": "probe", "events": []}

        real = GB.jarvis_stream
        GB.jarvis_stream = fake_stream  # type: ignore[assignment]
        try:
            chunks = [c async for c in AG.stream_agent("hi", session_id="t-s", surface="api")]
        finally:
            GB.jarvis_stream = real
        assert chunks and chunks[0]["node"] == "probe"
        assert seen == ["api"], f"stream ambient surface par chala: {seen}"

    asyncio.run(body())


# ---------------------------------------------------------------------------
# 4) jarvis_stream: node updates + thinking feed (real offline graph)
# ---------------------------------------------------------------------------
def test_jarvis_stream_has_progress_and_final():
    async def body():
        nodes: list[str] = []
        progress_lines: list[str] = []
        final = None
        async for chunk in GB.jarvis_stream("workspace me notes file dhoondh ke padho", session_id="t-stream-1"):
            nodes.append(chunk.get("node", "?"))
            if chunk.get("node") == "progress":
                progress_lines.extend(chunk.get("lines", []))
            if chunk.get("node") == "final":
                final = chunk
        assert "respond" in nodes, nodes
        assert final is not None and final.get("answer"), "final chunk missing"
        assert progress_lines, f"thinking feed khaali — nodes: {nodes}"
        assert any("🧠" in line or "📋" in line or "🛠️" in line or "✅" in line for line in progress_lines), (
            progress_lines[:6]
        )

    asyncio.run(body())


# ---------------------------------------------------------------------------
# 5) /stream SSE end-to-end (thinking feed web tak pahunchta hai)
# ---------------------------------------------------------------------------
def test_sse_stream_end_to_end():
    from jarvis.api.server import app

    route = next(r for r in app.routes if getattr(r, "path", "") == "/stream")
    endpoint = route.endpoint

    async def body():
        resp = await endpoint(message="workspace me notes file dhoondho", session_id="t-sse-1")
        assert resp.media_type == "text/event-stream"
        raw = b""
        async for piece in resp.body_iterator:
            raw += piece if isinstance(piece, bytes) else str(piece).encode()
        text = raw.decode("utf-8", "replace")
        assert "event: start" in text and "event: end" in text, text[:400]
        assert "event: node" in text
        assert '"node": "final"' in text, "final answer SSE par nahi aaya"
        assert '"node": "progress"' in text, "thinking feed SSE par nahi aaya"
        # final chunk ka answer khaali nahi hona chahiye (respond-node bug ka regression)
        import json as _json

        finals = [
            _json.loads(line[6:])
            for line in text.splitlines()
            if line.startswith("data: ") and '"node": "final"' in line
        ]
        assert finals and all(str(f.get("answer", "")).strip() for f in finals), finals

    asyncio.run(body())


# ---------------------------------------------------------------------------
# script runner (repo style)
# ---------------------------------------------------------------------------
def _run_all() -> int:
    import traceback

    fns = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    passed, failed = 0, []
    for name, fn in fns:
        G.reset_broker_for_tests()
        try:
            fn()
            print(f"  ✅ {name}")
            passed += 1
        except Exception as exc:
            print(f"  ❌ {name} — {exc}")
            traceback.print_exc(limit=4)
            failed.append(name)
    G.reset_broker_for_tests()
    print("=" * 66)
    print(
        f"  RESULT: {passed}/{passed + len(failed)} checks passed  "
        f"({'ALL GOOD 🚀' if not failed else 'kuch fail hue ⚠️'})"
    )
    print("=" * 66)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
