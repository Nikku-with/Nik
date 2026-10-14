"""
tests/test_voice.py — LiveKit voice agent (spec section 5)
===========================================================
  pytest tests/test_voice.py        (CI style)
  python tests/test_voice.py        (repo style runner)

No mic / no network. Covers: voice instructions (operator rules + barge-in),
turn-taking/VAD session config (real livekit-agents API), session factory
success (stub plugins) + helpful errors (blocked imports), LIVEKIT_URL format,
voice Gate asker (Telegram bridge / deny), run_nikhilai wiring, last-spoken.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JARVIS_OFFLINE_MODE", "1")

import jarvis.voice.livekit_agent as VA  # noqa: E402
from jarvis.config import settings as S  # noqa: E402
from jarvis.security import gate as G  # noqa: E402


def _patch_settings(**kw):
    before = {k: getattr(S, k) for k in kw}
    for k, v in kw.items():
        setattr(S, k, v)
    return lambda: [setattr(S, k, v) for k, v in before.items()]


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
# 1) instructions — operator rules + barge-in
# ---------------------------------------------------------------------------
def test_voice_instructions():
    text = VA.voice_instructions()
    for rule in ("Rule 1", "Rule 2", "Rule 3", "Rule 4", "Rule 5", "Rule 6"):
        assert rule in text, f"{rule} missing"
    assert "OPERATOR" in text and "chatbot nahi" in text
    assert "agent_task" in text, "voice tools ka zikr hona chahiye"
    assert "Telegram" in text, "dangerous approvals ka rasta batana chahiye"
    assert "TURANT ruko" in text, "barge-in rule missing"


# ---------------------------------------------------------------------------
# 2) turn-taking / VAD config — sane values
# ---------------------------------------------------------------------------
def test_session_config_ranges():
    cfg = VA.session_config()
    assert cfg["vad_provider"] == "silero"
    assert cfg["model"] and cfg["voice"]
    th = cfg["turn_handling"]
    ep, intr = th["endpointing"], th["interruption"]
    assert 0 < ep["min_delay"] < ep["max_delay"] <= 10, ep
    assert ep["mode"] in ("fixed", "dynamic")
    assert intr["enabled"] is True, "barge-in on hona chahiye"
    assert intr["mode"] == "vad"
    assert intr["resume_false_interruption"] is True
    assert 0 < intr["min_duration"] <= 2


# ---------------------------------------------------------------------------
# 3) session factory — stub plugins par real AgentSession
# ---------------------------------------------------------------------------
def _install_stub_plugins():
    """Fake silero + google-realtime modules (real AgentSession ke saath)."""
    saved = {k: sys.modules.get(k) for k in ("livekit.plugins.silero", "livekit.plugins.google.realtime")}

    class FakeVAD:
        loaded = False

        @classmethod
        def load(cls, **kw):
            cls.loaded = True
            return cls()

    silero_mod = types.ModuleType("livekit.plugins.silero")
    silero_mod.VAD = FakeVAD

    class FakeRealtime:
        made: list[dict] = []

        def __init__(self, **kw):
            type(self).made.append(kw)

    rt_mod = types.ModuleType("livekit.plugins.google.realtime")
    rt_mod.RealtimeModel = FakeRealtime
    sys.modules["livekit.plugins.silero"] = silero_mod
    sys.modules["livekit.plugins.google.realtime"] = rt_mod

    def restore():
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return restore, FakeVAD, FakeRealtime


def _dummy_livekit_env():
    before = {k: os.environ.get(k) for k in ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")}
    os.environ["LIVEKIT_API_KEY"] = "test-key-for-voice-unit-test"
    os.environ["LIVEKIT_API_SECRET"] = "x" * 40
    return lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in before.items()]


def test_create_session_with_stub_plugins():
    restore_mod, FakeVAD, FakeRealtime = _install_stub_plugins()
    restore_env = _dummy_livekit_env()
    try:
        session = VA.create_agent_session()
        from livekit import agents

        assert isinstance(session, agents.AgentSession), type(session)
        assert FakeVAD.loaded, "silero VAD load hona chahiye"
        assert FakeRealtime.made, "RealtimeModel bana hi nahi"
        made = FakeRealtime.made[0]
        assert made.get("model") == VA.voice_model(), made
        assert made.get("voice"), made
    finally:
        restore_env()
        restore_mod()


def test_create_session_helpful_errors_when_blocked():
    saved = {k: sys.modules.get(k) for k in ("livekit.plugins.silero", "livekit.plugins.google.realtime")}
    sys.modules["livekit.plugins.silero"] = None  # type: ignore[assignment]
    try:
        try:
            VA.create_agent_session()
        except RuntimeError as exc:
            assert "livekit-plugins-silero" in str(exc), exc
        else:
            raise AssertionError("silero block par error aana chahiye")
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_voice_plugins_shape():
    plug = VA.voice_plugins()
    assert set(plug) == {"silero", "google"}
    assert all(isinstance(v, bool) for v in plug.values())


# ---------------------------------------------------------------------------
# 4) LIVEKIT_URL format
# ---------------------------------------------------------------------------
def test_livekit_url_ok():
    for url, ok in (
        ("wss://abc.livekit.cloud", True),
        ("ws://localhost:7880", True),
        ("https://abc.livekit.cloud", False),
        ("abc.livekit.cloud", False),
        ("", False),
    ):
        restore = _patch_settings(LIVEKIT_URL=url)
        try:
            good, hint = VA.livekit_url_ok()
            assert good is ok, f"{url!r}: {hint}"
            assert hint, "hint khaali nahi"
        finally:
            restore()


# ---------------------------------------------------------------------------
# 5) voice asker — Telegram bridge ya deny
# ---------------------------------------------------------------------------
def _req(command="rm -rf /tmp/voice-probe"):
    return G.ApprovalRequest(id="v-1", command=command, why_flagged="test", cwd="", surface="voice_owner", timeout_s=10)


def test_voice_asker_denies_without_telegram():
    restore = _patch_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_OWNER_ID="111")
    try:
        assert VA.voice_approval_channel() == "none"
        assert VA.make_voice_asker()(_req()) == "no"
    finally:
        restore()


def test_voice_asker_telegram_bridge_yes_and_timeout():
    from jarvis.telegram import bot as T

    restore = _patch_settings(
        TELEGRAM_BOT_TOKEN="DUMMY", TELEGRAM_OWNER_ID="111", TELEGRAM_ALLOWED_CHAT_IDS=[], TELEGRAM_ALLOWED_USERS=[]
    )
    sent: list[dict] = []
    orig_send = VA.send_voice_approval_via_telegram
    VA.send_voice_approval_via_telegram = lambda req: (sent.append({"id": req.id}), True)[1]
    try:
        assert VA.voice_approval_channel() == "telegram"
        broker = G.reset_broker_for_tests()
        asker = VA.make_voice_asker()
        broker.register_asker("voice_owner", asker)
        box: dict = {}

        def req_in_thread():
            box["res"] = broker.request("rm -rf /tmp/voice-bridge", "test wajah", surface="voice_owner", timeout_s=15)

        t = threading.Thread(target=req_in_thread, daemon=True)
        t.start()
        for _ in range(200):
            if sent:
                break
            time.sleep(0.05)
        assert sent, "Telegram bridge par approval gaya hi nahi"
        rid = sent[0]["id"]
        # owner Telegram par YES dabata hai → voice ka step approve
        assert "Approved" in T.handle_gate_callback(f"gate:{rid}:yes", "111", "111")
        t.join(timeout=10)
        assert box["res"].approved, box["res"]

        # timeout path: koi tap nahi → deny
        broker2 = G.reset_broker_for_tests()
        broker2.register_asker("voice_owner", asker)
        box2: dict = {}
        t2 = threading.Thread(
            target=lambda: box2.update(
                res=broker2.request("rm -rf /tmp/voice-timeout", "t", surface="voice_owner", timeout_s=2)
            ),
            daemon=True,
        )
        t2.start()
        t2.join(timeout=10)
        assert not box2["res"].approved, "timeout par approve NAHI hona chahiye"
    finally:
        VA.send_voice_approval_via_telegram = orig_send
        restore()


def test_voice_asker_send_failure_denies():
    restore = _patch_settings(TELEGRAM_BOT_TOKEN="DUMMY", TELEGRAM_OWNER_ID="111")
    orig_send = VA.send_voice_approval_via_telegram
    VA.send_voice_approval_via_telegram = lambda req: (_ for _ in ()).throw(RuntimeError("network down"))
    try:
        assert VA.make_voice_asker()(_req()) == "no"
    finally:
        VA.send_voice_approval_via_telegram = orig_send
        restore()


def test_send_voice_approval_payload():
    import json
    import urllib.request

    restore = _patch_settings(TELEGRAM_BOT_TOKEN="TOK123", TELEGRAM_OWNER_ID="111")
    posted: list[dict] = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"ok": True}).encode()

    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=20: (
        posted.append({"url": req.full_url, "body": json.loads(req.data.decode())}),
        FakeResp(),
    )[1]
    try:
        req = G.ApprovalRequest(
            id="rid-9", command="rm -rf /tmp/v9", why_flagged="tree delete", cwd="", surface="voice_owner", timeout_s=60
        )
        assert VA.send_voice_approval_via_telegram(req) is True
        assert posted and "botTOK123/sendMessage" in posted[0]["url"]
        body = posted[0]["body"]
        assert body["chat_id"] == "111" and "v9" in body["text"]
        btns = [b for row in body["reply_markup"]["inline_keyboard"] for b in row]
        assert {b["callback_data"] for b in btns} == {"gate:rid-9:yes", "gate:rid-9:no"}
    finally:
        urllib.request.urlopen = orig
        restore()


# ---------------------------------------------------------------------------
# 6) run_nikhilai wiring — surface + gate + last-spoken
# ---------------------------------------------------------------------------
def test_run_nikhilai_wiring():
    from types import SimpleNamespace

    seen_calls: list[dict] = []
    seen_gate: list[dict] = []

    async def fake_run_agent(query, session_id="", surface="", voice_output=False, **kw):
        seen_calls.append({"query": query, "session_id": session_id, "surface": surface, "voice_output": voice_output})
        return SimpleNamespace(
            spoken="**Hello** 👋 suno https://x.y",
            answer="Hello suno",
            workers=["desktop"],
            reasoning="r",
            artifacts=[],
            errors=[],
            seconds=0.1,
        )

    real_run = VA.run_agent
    real_gc = G.gate_context

    class Cap(real_gc):  # type: ignore[misc]
        def __init__(self, surface, asker=None):
            seen_gate.append({"surface": surface, "asker": asker})
            super().__init__(surface, asker)

    VA.run_agent = fake_run_agent  # type: ignore[assignment]
    G.gate_context = Cap  # type: ignore[assignment]
    try:
        out = asyncio.run(VA.run_nikhilai("time batao", session_id="voice-t1"))
        assert seen_calls and seen_calls[0]["surface"] == "voice_owner", seen_calls
        assert seen_calls[0]["session_id"] == "voice-t1"
        assert seen_calls[0]["voice_output"] is True
        outer = seen_gate[0]
        assert outer["surface"] == "voice_owner" and callable(outer["asker"]), seen_gate
        assert out["spoken"] and "**" not in out["spoken"] and "http" not in out["spoken"]
        assert VA.last_spoken() == out["spoken"], "repeat_last ke liye save hona chahiye"
    finally:
        VA.run_agent = real_run
        G.gate_context = real_gc
        try:
            from jarvis.memory.session_state import get_state

            get_state().data.pop("voice_last_spoken", None)
            get_state().save()
        except Exception:
            pass


def test_last_spoken_empty_by_default():
    try:
        from jarvis.memory.session_state import get_state

        get_state().data.pop("voice_last_spoken", None)
    except Exception:
        pass
    assert VA.last_spoken() == ""


# ---------------------------------------------------------------------------
# 7) status — additive keys, ready semantics same
# ---------------------------------------------------------------------------
def test_voice_status_additive():
    st = VA.voice_status()
    for key in (
        "ready",
        "library",
        "keys",
        "gemini",
        "model",
        "voice",
        "missing",
        "plugins",
        "url_ok",
        "approval_channel",
    ):
        assert key in st, f"status key missing: {key}"
    expect_ready = bool(st["library"] and all(st["keys"].values()) and st["gemini"])
    assert st["ready"] is expect_ready, "ready semantics badal gayi!"
    assert isinstance(st["missing"], list)
    text = VA.voice_status_text()
    assert "VAD" in text and "ealtime" in text and "approvals" in text


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
