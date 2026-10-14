"""
tests/test_telegram.py — Telegram excellence (spec section 4)
==============================================================
  pytest tests/test_telegram.py        (CI style)
  python tests/test_telegram.py        (repo style runner)

Fake Update/Context/Bot objects — koi network nahi. Covers: authorize matrix,
callback routing, Gate Yes/No buttons (owner approve, guest/wrong-user deny,
single-use, expiry), tg_send_buttons (send/wait/timeout, token kabhi leak nahi),
send retry (429/5xx/timeout), pending approvals, app wiring.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JARVIS_OFFLINE_MODE", "1")

from jarvis.config import settings as S  # noqa: E402
from jarvis.security import gate as G  # noqa: E402
from jarvis.telegram import bot as T  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _patch_settings(**kw):
    before = {k: getattr(S, k) for k in kw}
    for k, v in kw.items():
        setattr(S, k, v)
    return lambda: [setattr(S, k, v) for k, v in before.items()]


class FakeBot:
    """send_message capture karta hai (network nahi)."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, **kw})
        return {"message_id": len(self.sent)}


class FakeFailBot(FakeBot):
    """Pehle N sends par transient error, phir success."""

    def __init__(self, exc_factory, fails: int):
        super().__init__()
        self._exc_factory = exc_factory
        self._fails = fails
        self.calls = 0

    async def send_message(self, chat_id, text, **kw):
        self.calls += 1
        if self.calls <= self._fails:
            raise self._exc_factory()
        return await super().send_message(chat_id, text, **kw)


class RetryAfter(Exception):
    def __init__(self):
        super().__init__("flood")
        self.retry_after = 0


class TimedOut(Exception):
    pass


try:
    import pytest  # type: ignore

    @pytest.fixture(autouse=True)
    def _auto_isolation():
        G.reset_broker_for_tests()
        restore = _patch_settings(
            TELEGRAM_OWNER_ID="111", TELEGRAM_ALLOWED_CHAT_IDS=[], TELEGRAM_ALLOWED_USERS=[], TELEGRAM_BOT_TOKEN="DUMMY"
        )
        yield
        restore()
        G.reset_broker_for_tests()
except Exception:  # pragma: no cover
    pass


# ---------------------------------------------------------------------------
# 1) authorize matrix
# ---------------------------------------------------------------------------
def test_authorize_owner_guest_blocked():
    restore = _patch_settings(
        TELEGRAM_OWNER_ID="111", TELEGRAM_ALLOWED_CHAT_IDS=["222"], TELEGRAM_ALLOWED_USERS=["333"]
    )
    try:
        assert T.authorize("111", "999") == (True, "owner")
        assert T.authorize(111, 999) == (True, "owner"), "int ids bhi"
        assert T.authorize("333", "999") == (True, "guest")
        assert T.authorize("444", "222") == (True, "guest")
        assert T.authorize("555", "666") == (False, "blocked")
    finally:
        restore()


def test_authorize_first_user_owner_when_unconfigured():
    restore = _patch_settings(TELEGRAM_OWNER_ID="", TELEGRAM_ALLOWED_CHAT_IDS=[], TELEGRAM_ALLOWED_USERS=[])
    try:
        assert T.authorize("777", "888") == (True, "owner")
    finally:
        restore()


def test_surface_for():
    assert T.surface_for("owner") == "telegram_owner"
    assert T.surface_for("guest") == "telegram_guest"


# ---------------------------------------------------------------------------
# 2) callback parsing + routing
# ---------------------------------------------------------------------------
def test_parse_callback():
    assert T.parse_callback("gate:abc-123:yes") == ("gate", {"id": "abc-123", "approved": "yes"})
    assert T.parse_callback("gate:abc-123:no")[0] == "gate"
    assert T.parse_callback("tgb:deadbeef:2") == ("tap", {"id": "deadbeef", "index": "2"})
    assert T.parse_callback("cmd:status") == ("cmd", {"name": "status"})
    assert T.parse_callback("gate:broken")[0] == "unknown"
    assert T.parse_callback("gate:x:maybe")[0] == "unknown"
    assert T.parse_callback("tgb:x:notanint")[0] == "unknown"
    assert T.parse_callback("")[0] == "unknown"
    assert T.parse_callback(None)[0] == "unknown"


def test_gate_keyboard_fits_limit():
    import uuid

    kb = T.make_gate_keyboard(str(uuid.uuid4()))
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert len(datas) == 2
    assert all(len(d) <= T.CB_MAX_LEN for d in datas), datas
    assert datas[0].endswith(":yes") and datas[1].endswith(":no")


def test_gate_tap_decision_pure():
    ok, reply = T.gate_tap_decision("owner", "gate:req1:yes")
    assert ok is True and "Approved" in reply
    ok, reply = T.gate_tap_decision("owner", "gate:req1:no")
    assert ok is False and "Denied" in reply
    ok, reply = T.gate_tap_decision("guest", "gate:req1:yes")
    assert ok is None and "sirf owner" in reply, "guest ka tap ignore"
    ok, _ = T.gate_tap_decision("owner", "garbage")
    assert ok is None


# ---------------------------------------------------------------------------
# 3) Gate button flow — owner approve / guest deny / single-use / expiry
# ---------------------------------------------------------------------------
def _owner_env():
    return _patch_settings(TELEGRAM_OWNER_ID="111", TELEGRAM_ALLOWED_CHAT_IDS=[], TELEGRAM_ALLOWED_USERS=["333"])


def _pending_request(command="rm -rf /tmp/tg-probe"):
    """Broker me pending request daalo (asker block karta hai), id wapas do."""
    broker = G.reset_broker_for_tests()
    box: dict[str, str] = {}

    def waiter(req):
        box["id"] = req.id
        time.sleep(30)
        return "no"

    broker.register_asker("zz", waiter)
    t = threading.Thread(target=lambda: broker.request(command, "t", surface="zz", timeout_s=30), daemon=True)
    t.start()
    for _ in range(100):
        if "id" in box:
            break
        time.sleep(0.05)
    assert "id" in box
    return broker, box["id"], t


def test_owner_yes_resolves_owner_no_denies():
    restore = _owner_env()
    try:
        broker, rid, t = _pending_request()
        reply = T.handle_gate_callback(f"gate:{rid}:yes", "111", "111")
        assert "Approved" in reply, reply
        t.join(timeout=5)
        assert broker.get(rid).status == "approved"
        assert broker.get(rid).decided_by == "telegram:111"

        broker, rid2, t2 = _pending_request("rm -rf /tmp/tg-probe-2")
        reply = T.handle_gate_callback(f"gate:{rid2}:no", "111", "111")
        assert "Denied" in reply, reply
        t2.join(timeout=5)
        assert broker.get(rid2).status == "denied"
    finally:
        restore()


def test_guest_tap_ignored_blocked_user_rejected():
    restore = _owner_env()
    try:
        broker, rid, t = _pending_request()
        reply = T.handle_gate_callback(f"gate:{rid}:yes", "333", "333")
        assert "sirf owner" in reply, reply
        assert broker.get(rid).status == "pending", "guest tap se resolve NAHI hona chahiye"
        reply = T.handle_gate_callback(f"gate:{rid}:yes", "999", "999")
        assert "allowed users me nahi" in reply, reply
        assert broker.get(rid).status == "pending"
        # cleanup: owner deny
        T.handle_gate_callback(f"gate:{rid}:no", "111", "111")
        t.join(timeout=5)
    finally:
        restore()


def test_gate_button_single_use_and_expired():
    restore = _owner_env()
    try:
        broker, rid, t = _pending_request()
        assert "Approved" in T.handle_gate_callback(f"gate:{rid}:yes", "111", "111")
        t.join(timeout=5)
        # dobara tap → already decided
        assert "expire" in T.handle_gate_callback(f"gate:{rid}:yes", "111", "111")
        # unknown id → expire message
        assert "expire" in T.handle_gate_callback("gate:does-not-exist:no", "111", "111")
    finally:
        restore()


# ---------------------------------------------------------------------------
# 4) owner asker — buttons bhejta hai, tap ka wait karta hai
# ---------------------------------------------------------------------------
def test_make_owner_asker_end_to_end():
    async def body():
        restore = _owner_env()
        try:
            broker = G.reset_broker_for_tests()
            bot = FakeBot()
            loop = asyncio.get_running_loop()
            asker = T.make_owner_asker(bot, "111", loop)
            broker.register_asker("telegram_owner", asker)
            box: dict = {}

            def req_in_thread():
                box["res"] = broker.request(
                    "rm -rf /tmp/tg-asker", "test wajah", surface="telegram_owner", timeout_s=15
                )

            t = threading.Thread(target=req_in_thread, daemon=True)
            t.start()
            for _ in range(200):
                if bot.sent:
                    break
                await asyncio.sleep(0.05)
            assert bot.sent, "asker ne approval message bheja hi nahi"
            msg = bot.sent[0]
            assert msg["chat_id"] == "111" and "Approval chahiye" in msg["text"]
            assert "test wajah" in msg["text"], "wajah message me dikhni chahiye"
            kb = msg["reply_markup"]
            datas = [b.callback_data for row in kb.inline_keyboard for b in row]
            yes_cbs = [d for d in datas if d.endswith(":yes")]
            assert len(yes_cbs) == 1, datas
            # owner taps YES
            reply = T.handle_gate_callback(yes_cbs[0], "111", "111")
            assert "Approved" in reply
            t.join(timeout=10)
            assert box["res"].approved, box["res"]
        finally:
            restore()

    asyncio.run(body())


def test_guest_asker_denies_immediately():
    restore = _owner_env()
    try:
        G.reset_broker_for_tests()
        req = G.ApprovalRequest(id="probe", command="rm -rf /x", why_flagged="t", cwd="", surface="telegram_guest")
        assert T._guest_deny_asker(req) == "no"
    finally:
        restore()


# ---------------------------------------------------------------------------
# 5) agent-question taps (tg_send_buttons wait mode)
# ---------------------------------------------------------------------------
def test_tap_waiter_resolve_and_guards():
    async def body():
        restore = _owner_env()
        try:
            tap_id = "tap123abc"
            ev = T.register_tap_waiter(tap_id, [{"label": "A", "value": "A"}, {"label": "B", "value": "B"}])
            # blocked user ka tap → reject, waiter jaagta nahi
            assert "allowed users" in T.handle_tap_callback(f"tgb:{tap_id}:0", "999", "999")
            assert not ev.is_set()
            # galat index → expire-ish (resolve fail)
            assert T.resolve_tap(tap_id, 9, "owner:111") is False
            assert not ev.is_set()
            # guest ka valid tap → accept (sawaal ka jawab koi allowed user de sakta hai)
            reply = T.handle_tap_callback(f"tgb:{tap_id}:1", "333", "333")
            assert "agent ko bhej diya" in reply, reply
            await asyncio.wait_for(ev.wait(), timeout=5)
            box = T.tap_result(tap_id)
            assert box and box["value"] == "B" and box["by"] == "guest:333"
            T.cancel_tap_waiter(tap_id)
            assert T.tap_result(tap_id) is None
            # unknown tap
            assert "expire" in T.handle_tap_callback("tgb:nope:0", "111", "111")
        finally:
            restore()

    asyncio.run(body())


# ---------------------------------------------------------------------------
# 6) send retry — 429 / 5xx / timeout, phir haar
# ---------------------------------------------------------------------------
def test_send_retry_then_success():
    async def body():
        bot = FakeFailBot(RetryAfter, fails=2)
        out = await T.send_with_retry(lambda: bot.send_message("111", "hi"), base_delay=0.01, what="test")
        assert out["message_id"] == 1 and bot.calls == 3
        assert len(bot.sent) == 1

    asyncio.run(body())


def test_send_retry_gives_up_and_raises():
    async def body():
        bot = FakeFailBot(TimedOut, fails=99)
        try:
            await T.send_with_retry(lambda: bot.send_message("111", "hi"), tries=3, base_delay=0.01, what="test")
        except TimedOut:
            pass
        else:
            raise AssertionError("haar ke baad raise hona chahiye tha")
        assert bot.calls == 3

    asyncio.run(body())


def test_send_no_retry_on_permanent_error():
    async def body():
        calls = {"n": 0}

        async def bad():
            calls["n"] += 1
            raise ValueError("bad request — retry bekaar")

        try:
            await T.send_with_retry(bad, tries=3, base_delay=0.01)
        except ValueError:
            pass
        else:
            raise AssertionError("ValueError raise hona chahiye")
        assert calls["n"] == 1, "permanent error par retry nahi"

    asyncio.run(body())


# ---------------------------------------------------------------------------
# 7) pending approvals list + app wiring
# ---------------------------------------------------------------------------
def test_format_pending_approvals():
    restore = _owner_env()
    try:
        G.reset_broker_for_tests()
        text, kb = T.format_pending_approvals()
        assert "Koi pending" in text and kb is None
        _, rid, t = _pending_request("rm -rf /tmp/tg-list")
        text, kb = T.format_pending_approvals()
        assert "tg-list" in text and kb is not None
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert f"gate:{rid}:yes" in datas and f"gate:{rid}:no" in datas
        T.handle_gate_callback(f"gate:{rid}:no", "111", "111")
        t.join(timeout=5)
    finally:
        restore()


def test_build_application_wiring():
    restore = _patch_settings(TELEGRAM_BOT_TOKEN="123:FAKE-TOKEN-FOR-TEST")
    try:
        app = T.build_application()
        handlers = app.handlers.get(0, [])
        from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

        cmds = sorted(h.commands for h in handlers if isinstance(h, CommandHandler))
        flat = [c for group in cmds for c in group]
        for must in ("start", "status", "mode", "approvals", "menu", "automation", "memory", "progress", "restart"):
            assert must in flat, f"/{must} handler missing: {flat}"
        assert any(isinstance(h, CallbackQueryHandler) for h in handlers), "button handler missing"
        assert any(isinstance(h, MessageHandler) for h in handlers), "free-text handler missing"
    finally:
        restore()


# ---------------------------------------------------------------------------
# 8) tg_send_buttons tool
# ---------------------------------------------------------------------------
def test_tg_tool_no_token_no_chat():
    from jarvis.tools.telegram_tools import tg_send_buttons

    restore = _patch_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_OWNER_ID="")
    try:
        out = asyncio.run(tg_send_buttons.ainvoke({"text": "hi?", "buttons": ["A"]}))
        assert "TELEGRAM_BOT_TOKEN" in out, out
    finally:
        restore()
    restore = _patch_settings(TELEGRAM_BOT_TOKEN="AAA", TELEGRAM_OWNER_ID="")
    try:
        out = asyncio.run(tg_send_buttons.ainvoke({"text": "hi?", "buttons": ["A"]}))
        assert "chat_id" in out, out
    finally:
        restore()
    out = asyncio.run(tg_send_buttons.ainvoke({"text": "", "buttons": ["A"]}))
    assert "khaali" in out
    out = asyncio.run(tg_send_buttons.ainvoke({"text": "hi?", "buttons": []}))
    assert "1 button" in out


def test_tg_tool_send_and_tap_and_timeout():
    import urllib.error

    from jarvis.tools import telegram_tools as TT

    restore = _patch_settings(TELEGRAM_BOT_TOKEN="SECRET-TOKEN-XYZ", TELEGRAM_OWNER_ID="111")
    posted: list[dict] = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json as _j

            return _j.dumps({"ok": True, "result": {"message_id": 7}}).encode()

    orig = TT.urllib.request.urlopen
    TT.urllib.request.urlopen = lambda req, timeout=20: (
        posted.append({"url": req.full_url, "body": json.loads(req.data.decode())}),
        FakeResp(),
    )[1]
    try:
        import json  # noqa: F401

        # --- fire-and-forget mode ---
        out = asyncio.run(
            TT.tg_send_buttons.ainvoke({"text": "kaun sa?", "buttons": ["A", "B", "C"], "wait_for_tap": False})
        )
        assert "message 7" in out and "wait nahi" in out, out
        assert posted and posted[0]["body"]["chat_id"] == "111"
        rows = posted[0]["body"]["reply_markup"]["inline_keyboard"]
        flat = [b for r in rows for b in r]
        assert [b["text"] for b in flat] == ["A", "B", "C"]
        assert all(b["callback_data"].startswith("tgb:") and len(b["callback_data"]) <= 64 for b in flat)

        # --- wait mode + simulated owner tap ---
        async def waiter():
            task = asyncio.create_task(
                TT.tg_send_buttons.ainvoke({"text": "pick one", "buttons": ["X", "Y"], "timeout_s": 10})
            )
            for _ in range(200):
                if len(posted) >= 2:
                    break
                await asyncio.sleep(0.05)
            cbs = [b["callback_data"] for r in posted[1]["body"]["reply_markup"]["inline_keyboard"] for b in r]
            tap_id = cbs[0].split(":")[1]
            # owner taps second button
            reply = T.handle_tap_callback(f"tgb:{tap_id}:1", "111", "111")
            assert "agent ko bhej diya" in reply
            return await asyncio.wait_for(task, timeout=10)

        restore2 = _owner_env()
        try:
            out = asyncio.run(waiter())
        finally:
            restore2()
        assert "Tap:" in out and "**Y**" in out and "owner:111" in out, out
    finally:
        TT.urllib.request.urlopen = orig
        restore()

    # --- timeout path (chhota timeout, koi tap nahi) ---
    restore = _patch_settings(TELEGRAM_BOT_TOKEN="SECRET-TOKEN-XYZ", TELEGRAM_OWNER_ID="111")
    TT.urllib.request.urlopen = lambda req, timeout=20: FakeResp()
    try:
        out = asyncio.run(TT.tg_send_buttons.ainvoke({"text": "koi hai?", "buttons": ["Haan"], "timeout_s": 5}))
        assert "koi tap nahi" in out, out
        assert "SECRET-TOKEN" not in out, "token leak!"
    finally:
        TT.urllib.request.urlopen = orig
        restore()

    # --- HTTP failure (token leak check) ---
    def boom(req, timeout=20):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    restore = _patch_settings(TELEGRAM_BOT_TOKEN="SECRET-TOKEN-XYZ", TELEGRAM_OWNER_ID="111")
    TT.urllib.request.urlopen = boom
    try:
        out = asyncio.run(TT.tg_send_buttons.ainvoke({"text": "x", "buttons": ["A"], "wait_for_tap": False}))
        assert "HTTP 401" in out, out
        assert "SECRET-TOKEN" not in out, "token leak!"
    finally:
        TT.urllib.request.urlopen = orig
        restore()


def test_tg_tool_permission_guest_blocked():
    from jarvis.security.permissions import check

    assert check("tg_send_buttons", "telegram_guest").allowed is False
    assert check("tg_send_buttons", "telegram_owner").allowed is True


# ---------------------------------------------------------------------------
# script runner (repo style)
# ---------------------------------------------------------------------------
def _run_all() -> int:
    import traceback

    restore_set = _patch_settings(
        TELEGRAM_OWNER_ID="111",
        TELEGRAM_ALLOWED_CHAT_IDS=[],
        TELEGRAM_ALLOWED_USERS=["333"],
        TELEGRAM_BOT_TOKEN="DUMMY",
    )
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
    restore_set()
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
