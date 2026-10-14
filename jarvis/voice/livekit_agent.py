"""
Realtime voice mode — LiveKit Agents + Gemini Live
=================================================

Architecture (text mode ke saath **wahi brain** shared):

    MIC → LIVEKIT (WebRTC) → GEMINI LIVE (realtime STT+LLM+TTS)
                                   │  tool call: ask_nikhilai / agent_task
                                   ▼
                            LangGraph (same graph, same tools, same memory)
                                   ▼
                            spoken answer (emoji/markdown cleaned)

Chalane ke liye:
    1. `pip install "livekit-agents[google]" livekit-plugins-google livekit-plugins-silero`
    2. .env me LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET + GEMINI_API_KEY
    3. `python -m jarvis voice dev`   (ya `console` = terminal me test, browser ke bina)

CLI (LiveKit Cloud deploy) bhi support hai: `lk cloud auth && lk agent init i-agent`
"""

from __future__ import annotations

import sys
from typing import Any

from ..config import runtime as R
from ..config import settings as S
from ..runtime.agent import run_agent
from ..voice.speech import for_speech, summary_for_speech

SESSION = "voice"


# ---------------------------------------------------------------------------
# graph bridge — voice tools isi ko call karte hain
# ---------------------------------------------------------------------------
async def run_nikhilai(query: str, session_id: str = SESSION, spoken: bool = True) -> dict[str, Any]:
    """Voice se aayi request → LangGraph → {answer, spoken}.

    Dangerous steps par voice asker poochta hai (Telegram bridge, warna deny) —
    `gate_context` yahin wrap hota hai taaki har voice tool call cover ho.
    """
    from ..security.gate import gate_context

    with gate_context("voice_owner", asker=make_voice_asker()):
        result = await run_agent(query, session_id=session_id, surface="voice_owner", voice_output=spoken)
    # voice par chhota jawab natural lagta hai — 3 sentence se zyada mat bolo
    spoken_text = summary_for_speech(result.spoken or result.answer, max_sentences=3)
    try:
        from ..memory.session_state import get_state

        get_state().data["voice_last_spoken"] = for_speech(spoken_text)
        get_state().save()
    except Exception:
        pass
    return {
        "answer": result.answer,
        "spoken": for_speech(spoken_text),
        "workers": result.workers,
        "reasoning": result.reasoning,
        "artifacts": result.artifacts,
        "errors": result.errors,
        "seconds": result.seconds,
    }


# backwards-compatible alias (purana naam)
async def run_jarvis_graph(query: str, session_id: str = SESSION) -> dict[str, Any]:
    return await run_nikhilai(query, session_id=session_id)


# ---------------------------------------------------------------------------
# agent instructions
# ---------------------------------------------------------------------------
def voice_instructions() -> str:
    owner = R.get("owner_name", S.OWNER_NAME)
    name = R.get("assistant_name", S.ASSISTANT_NAME)
    personality = R.get("personality", S.ASSISTANT_PERSONALITY)
    language = R.get("language", S.LANGUAGE)
    return f"""Aap {name} ho — {owner} ke personal AI operator.
Personality: {personality}. Bhasha: {language} (Hindi + English mila-jula, natural).

Rule 1: Aap ek OPERATOR ho, chatbot nahi. Kaam karne se pehle "main kar sakta hoon" mat bolo —
        seedha tool call karo aur result batao.
Rule 2: Jo cheez aapko nahi pata (aaj ki news, price, mausam, current time, kisi company ka CEO) —
        kabhi andaaza mat lagao. `agent_task` tool se research karwao, ya `current_time` use karo.
Rule 3: Jawab CHHOTA aur bolne layak do (1-3 sentence). Emoji mat bolo, markdown mat padho —
        consumer device use karegi, isliye sirf natural sentences.
Rule 4: Lamba kaam (scraping, code, multi-site research) `agent_task` ko do aur bolo
        "kaam chal raha hai, result chat me bhej deta hoon" — mic par wait mat karao.
Rule 5: Kuch na samajh aaye to ek chhota sa sawaal poocho, phir kaam karo.
Rule 6: User beech me bole to TURANT ruko aur suno (barge-in) — apna sentence poora
        karne ki zid mat karo. Dangerous kaam (delete/format/kill) voice par kabhi
        khud approve mat karo — bolo "ye Telegram par bheja, wahan approve karo"."""


# ---------------------------------------------------------------------------
# LiveKit entrypoint
# ---------------------------------------------------------------------------
def build_livekit_agent_runner(args: list[str] | None = None):
    """LiveKit Agents ka entrypoint — voice session banata hai.

    `args` = LiveKit ka subcommand + options, jaise `["console"]`, `["dev"]`,
    `["console", "--list-devices"]`. Trim karke AAKHRI me LiveKit ko hi dete hain.

    ⚠️ Ye bug tha (user ne pakda): menu se voice chalao ya bina mode ke — hum `args` khaali
    chhod dete the. LiveKit ka CLI apna *subcommand* `sys.argv` se padhta hai, isliye usne
    `Usage: python -m jarvis [OPTIONS] COMMAND ...` print kar diya aur console start hi
    nahi hua. Ab khaali args par hamesha `["console"]` jaata hai.
    """
    try:
        from livekit import agents
        from livekit.agents import Agent, RunContext, function_tool
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "LiveKit agents installed nahi hain. Chalao:\n"
            '  pip install "livekit-agents[google]" livekit-plugins-google livekit-plugins-silero\n'
            f"(detail: {exc})"
        ) from exc

    @function_tool
    async def current_time(context: RunContext) -> str:
        """Current date aur time batao (kabhi andaaza mat lagao)."""
        from ..tools.system_tools import get_current_datetime

        return await get_current_datetime.ainvoke({})

    @function_tool
    async def agent_task(context: RunContext, request: str) -> str:
        """Pura NikhilAI agent chalao — research, files, code, browser, desktop, memory, automation.

        Jab user kuch aisa maange jo aapke paas turant nahi hai (live info, file kaam, code,
        system control, yaad rakhna) — ye tool call karo. Request Hinglish me poori likho.
        """
        res = await run_nikhilai(request)
        if res["errors"]:
            return f"Task me dikkat aayi: {str(res['errors'][0])[:150]}"
        return res["spoken"] or res["answer"][:600]

    @function_tool
    async def remember(context: RunContext, fact: str) -> str:
        """Owner ki koi baat yaad rakho (long-term memory me)."""
        from ..memory.manager import get_manager

        out = get_manager().remember(fact)
        return "yaad rakh liya" if out.get("ok") else "save nahi ho paya"

    @function_tool
    async def forget(context: RunContext, about: str) -> str:
        """Owner ki koi memory bhool jao (asli deletion)."""
        from ..memory.manager import get_manager

        out = get_manager().forget(about)
        return out["message"]

    @function_tool
    async def repeat_last(context: RunContext) -> str:
        """Aakhri jawab dobara bolo (user ne "phir se bolo" kaha ho)."""
        prev = last_spoken()
        return prev or "abhi kuch bola hi nahi — pehle kuch poochho"

    async def entrypoint(ctx: agents.JobContext) -> None:
        await ctx.connect()
        session = create_agent_session()
        agent = Agent(
            instructions=voice_instructions(), tools=[current_time, agent_task, remember, forget, repeat_last]
        )
        await session.start(room=ctx.room, agent=agent)

        greeting = f"Namaste {R.get('owner_name', S.OWNER_NAME)}! {R.get('assistant_name', S.ASSISTANT_NAME)} online hai. Bataiye?"
        await session.generate_reply(instructions=f"Nikhil ko warmly greet karo in ek line: {greeting}")

    effective = [a for a in (args or []) if str(a).strip()] or ["console"]
    saved = list(sys.argv)
    sys.argv = [(saved[0] if saved else "python -m jarvis"), *effective]
    try:
        return agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
    finally:
        sys.argv = saved


def voice_model() -> str:
    return str(R.get("voice_model", S.REALTIME_MODEL))


def voice_plugins() -> dict[str, bool]:
    """Voice plugins installed hain ya nahi (silero VAD + google realtime)."""
    out = {}
    try:
        from livekit.plugins import silero  # noqa: F401

        out["silero"] = True
    except Exception:
        out["silero"] = False
    try:
        from livekit.plugins.google import realtime  # noqa: F401

        out["google"] = True
    except Exception:
        out["google"] = False
    return out


def livekit_url_ok() -> tuple[bool, str]:
    """LIVEKIT_URL format check — wss:// (cloud) ya ws:// (local dev)."""
    url = (S.LIVEKIT_URL or "").strip()
    if not url:
        return False, "LIVEKIT_URL khaali hai"
    if url.startswith("wss://"):
        return True, "ok"
    if url.startswith("ws://"):
        return True, "ok (local/dev — production me wss:// use karo)"
    if url.startswith("https://"):
        return False, "LIVEKIT_URL me https:// nahi — wss:// chahiye (WebSocket, HTTP nahi)"
    return False, "LIVEKIT_URL wss://... hona chahiye"


def session_config() -> dict[str, Any]:
    """Turn-taking + VAD config (livekit-agents 1.x TurnHandlingOptions shape).

    * endpointing: user kab bolna band kare → 0.5s chuppi par turn khatam, 3s max wait
    * interruption: user beech me bole → VAD par turant ruko (barge-in), false alarm
      par apni baat resume karo
    """
    return {
        "vad_provider": "silero",
        "model": voice_model(),
        "voice": str(R.get("voice_name", S.VOICE_NAME)),
        "turn_handling": {
            "endpointing": {"mode": "fixed", "min_delay": 0.5, "max_delay": 3.0},
            "interruption": {
                "enabled": True,
                "mode": "vad",
                "min_duration": 0.5,
                "min_words": 0,
                "resume_false_interruption": True,
                "false_interruption_timeout": 1.5,
            },
        },
    }


def create_agent_session():
    """Configured AgentSession: silero VAD + google realtime + turn-taking.

    Raises RuntimeError with exact pip command agar plugin missing hai.
    """
    try:
        from livekit import agents
    except Exception as exc:
        raise RuntimeError(f'livekit-agents installed nahi: pip install "livekit-agents[google]" (detail: {exc})') from exc
    try:
        from livekit.plugins import silero
    except Exception as exc:
        raise RuntimeError(f"VAD missing hai: pip install livekit-plugins-silero (detail: {exc})") from exc
    try:
        from livekit.plugins.google import realtime
    except Exception as exc:
        raise RuntimeError(f"Google realtime missing hai: pip install livekit-plugins-google (detail: {exc})") from exc
    cfg = session_config()
    vad = silero.VAD.load()
    llm = realtime.RealtimeModel(model=cfg["model"], voice=cfg["voice"], api_key=S.GEMINI_API_KEY or None)
    import asyncio

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        # AgentSession ko current loop chahiye; asyncio.run() ke baad koi set
        # loop nahi rehta (test-order/sync-CLI flake) — ek fresh loop set karo.
        asyncio.set_event_loop(asyncio.new_event_loop())
    return agents.AgentSession(vad=vad, llm=llm, turn_handling=cfg["turn_handling"])


def voice_approval_channel() -> str:
    """ "telegram" agar owner ka Telegram configured hai (bridge), warna "none"."""
    token = (S.TELEGRAM_BOT_TOKEN or "").strip()
    owner = str(S.TELEGRAM_OWNER_ID or "").strip()
    return "telegram" if (token and owner) else "none"


def send_voice_approval_via_telegram(req) -> bool:
    """Voice ka dangerous step → owner ke Telegram par Yes/No buttons.

    Tap tabhi resolve hoga jab Telegram bot running ho (wahi `gate:` callbacks
    handle karta hai) — bot band ho to timeout par auto-deny (fail closed).
    """
    import json
    import urllib.request

    token = (S.TELEGRAM_BOT_TOKEN or "").strip()
    owner = str(S.TELEGRAM_OWNER_ID or "").strip()
    cmd = req.command or req.kind or "?"
    if len(cmd) > 250:
        cmd = cmd[:250] + "…"
    text = (
        "🎙️🔐 Voice se dangerous kaam aaya — approval chahiye:\n"
        f"Command: {cmd}\n"
        f"Wajah: {req.why_flagged or 'dangerous'}\n"
        f"(⏳ {req.timeout_s}s me jawab nahi to auto-cancel)"
    )
    payload = {
        "chat_id": owner,
        "text": text[:3500],
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "✅ Haan, chalao", "callback_data": f"gate:{req.id}:yes"},
                    {"text": "🚫 Nahi", "callback_data": f"gate:{req.id}:no"},
                ]
            ]
        },
    }
    data = json.dumps(payload).encode("utf-8")
    http_req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(http_req, timeout=20) as resp:
        return bool(json.loads(resp.read().decode("utf-8")).get("ok", False))


def make_voice_asker():
    """Voice surface asker: Telegram bridge par poochho, warna turant deny.

    Mic par "haan/naa" sunna realtime talk-loop todta aur galat approve kara
    sakta hai — isliye voice khud kabhi approve nahi karta.
    """

    def _ask(req) -> str:
        if voice_approval_channel() != "telegram":
            return "no"
        from ..security.gate import get_broker

        broker = get_broker()
        try:
            sent = send_voice_approval_via_telegram(req)
        except Exception:
            sent = False
        if not sent:
            return "no"
        ev = broker._events.get(req.id)
        if ev is not None:
            ev.wait(timeout=req.timeout_s)
            if req.status == "approved":
                return "yes"
        return "no"

    return _ask


def last_spoken() -> str:
    """Aakhri bola hua jawab (repeat_last tool ke liye)."""
    try:
        from ..memory.session_state import get_state

        return str(get_state().data.get("voice_last_spoken", "") or "")
    except Exception:
        return ""


def voice_status() -> dict[str, Any]:
    """Voice ki readiness — doctor/CLI/Telegram ke liye."""
    lib = S.has_livekit()
    keys = {"url": bool(S.LIVEKIT_URL), "api_key": bool(S.LIVEKIT_API_KEY), "secret": bool(S.LIVEKIT_API_SECRET)}
    gem = bool(S.GEMINI_API_KEY)
    ready = lib and all(keys.values()) and gem
    url_ok, url_hint = livekit_url_ok()
    plugins = voice_plugins()
    missing: list[str] = []
    if not lib:
        missing.append('pip install "livekit-agents[google]" livekit-plugins-google livekit-plugins-silero')
    if not all(keys.values()):
        missing.append("LIVEKIT_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET (cloud.livekit.io → Settings → Keys)")
    if not gem:
        missing.append("GEMINI_API_KEY (realtime brain, aistudio.google.com/apikey)")
    if S.LIVEKIT_URL and not url_ok:
        missing.append(f"LIVEKIT_URL format galat: {url_hint}")
    return {
        "ready": ready,
        "library": lib,
        "keys": keys,
        "gemini": gem,
        "model": voice_model(),
        "voice": str(R.get("voice_name", S.VOICE_NAME)),
        "missing": missing,
        "plugins": plugins,
        "url_ok": url_ok,
        "url_hint": url_hint,
        "approval_channel": voice_approval_channel(),
    }


def voice_status_text() -> str:
    st = voice_status()
    plug = st.get("plugins", {})
    vad_line = "✅ silero VAD" if plug.get("silero") else "❌ silero VAD (pip install livekit-plugins-silero)"
    rt_line = "✅ google realtime" if plug.get("google") else "❌ google realtime (pip install livekit-plugins-google)"
    lines = [
        "🎙️ *Voice mode (LiveKit + Gemini Live)*",
        f"• Ready: {'✅ haan' if st['ready'] else '❌ abhi nahi'}",
        f"• voice: `{st['voice']}`  •  realtime model: `{st['model']}`",
        f"• VAD/turn-taking: {vad_line}",
        f"• Realtime brain: {rt_line}",
        f"• Dangerous approvals: {'📲 Telegram buttons' if st.get('approval_channel') == 'telegram' else '🚫 voice par deny (Telegram set karo)'}",
    ]
    if st["missing"]:
        lines.append("• Chahiye:")
        lines += [f"   – {m}" for m in st["missing"]]
    else:
        lines.append(
            "• Chalane ke liye: `python -m jarvis voice console` (terminal test) ya `python -m jarvis voice dev`"
        )
    return "\n".join(lines)


VOICE_HELP = """
🎙️  Voice mode (LiveKit + Gemini Live) — console se kaise chalao
=================================================================
python -m jarvis voice console     → terminal me hi mic+speaker se baat karo (fastest test)
python -m jarvis voice dev         → dev mode (hot reload, LiveKit Cloud ke saath debugging)
python -m jarvis voice start       → production worker (cloud me deploy/job accept karega)
python -m jarvis voice test        → keys ke BINA pipeline test (spoken output + latency)
python -m jarvis voice keys        → kaunsi keys chahiye, kahan se milegi (exact steps)
python -m jarvis voice status      → abhi kya ready hai, kya missing hai

Zaroori keys (teen + ek):
  LIVEKIT_URL=wss://<project>.livekit.cloud        cloud.livekit.io → project → Settings → Keys
  LIVEKIT_API_KEY=APIxxxxxxxx
  LIVEKIT_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  GEMINI_API_KEY=AIza...                            aistudio.google.com/apikey (realtime brain)

Install:
  pip install "livekit-agents[google]" livekit-plugins-google livekit-plugins-silero

Note: `console` mode sirf aapke laptop par chalta hai (mic + speaker). `dev`/`start` ke liye
LiveKit Cloud project chahiye — bot room join karta hai, koi bhi device/site se baat kar sakte ho.
"""

KEYS_GUIDE = """
🔑 Voice ke liye keys — exact steps (5 minute ka kaam)
======================================================
1) LiveKit Cloud (free tier kaafi hai — transport + rooms):
   a) https://cloud.livekit.io par sign up (GitHub/Google se bhi ho jata hai)
   b) Naya project banao (naam: nikhilai)
   c) Project → Settings → Keys → "Create key" → naam: jarvis
   d) Wahan se teen cheezein copy karo:

      LIVEKIT_URL=wss://<aapka-project>.livekit.cloud
      LIVEKIT_API_KEY=APIxxxxxxxxxxxx
      LIVEKIT_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

2) Google AI Studio (realtime voice brain — Gemini Live):
   a) https://aistudio.google.com/apikey → "Create API key"
   b) GEMINI_API_KEY=AIza...

3) .env me daalo (workspace root me):
      LIVEKIT_URL=...
      LIVEKIT_API_KEY=...
      LIVEKIT_API_SECRET=...
      GEMINI_API_KEY=...

4) Verify: python -m jarvis voice status   → sab ✅ aana chahiye
5) Chalao: python -m jarvis voice console

Bahut ho gaya? `python -m jarvis voice test` bina keys bhi pipeline check kar deta hai.
"""


VOICE_MODES = ("console", "dev", "start", "connect", "download-files")


def start_livekit(args: list[str]) -> int:
    """LiveKit runner start karo (args passthrough ke saath) — test isko monkeypatch karte hain."""
    return build_livekit_agent_runner(args) or 0


def _run_sync(coro):
    """Coroutine ko sync tarike se chalao — kisi running loop ke andar bhi safe.

    (Ek hi thread me doosra loop nahi chala sakte, isliye loop na hone par seedha
    `asyncio.run`, warna ek alag thread me apna loop — dono case me result wapas.)
    """
    import asyncio
    import threading

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, object] = {}

    def worker() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            box["value"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            loop.close()

    t = threading.Thread(target=worker, name="voice-run-sync", daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("value")


def cmd_keys() -> int:
    print(KEYS_GUIDE)
    st = voice_status()
    print("Abhi ka status:")
    for k, v in st["keys"].items():
        print(f"  {'✅' if v else '❌'} {k}")
    print(f"  {'✅' if st['gemini'] else '❌'} GEMINI_API_KEY")
    print(f"  {'✅' if st['library'] else '❌'} livekit-agents library")
    return 0


def cmd_test(query: str = "") -> int:
    """Keys ke bina voice pipeline test — spoken text + latency + tool flow.

    Ye batata hai ki agar mic/LiveKit keys hote, to user ko kya sunai deta:
    emoji/markdown/URL strip hokar saaf bola hua text.
    """
    import time


    q = query or "aaj ki date batao aur ek chhota joke sunao"
    print("🎙️ Voice pipeline test (keys ki zaroorat nahi)")
    print(f"   User bolta (sample): {q!r}")
    t0 = time.time()
    res = _run_sync(run_nikhilai(q, session_id="voice-test", spoken=True))
    dt = time.time() - t0
    answer = str(res.get("answer") or "")
    spoken = str(res.get("spoken") or "")
    checks = []
    if spoken:
        checks.append(("spoken text mila", True))
    checks.append(("emoji strip", not any(ord(c) > 0x2190 for c in spoken)))
    checks.append(("markdown strip", "**" not in spoken and "`" not in spoken))
    checks.append(("URL strip", "http://" not in spoken and "https://" not in spoken))
    checks.append(("code block strip", "```" not in spoken))
    print()
    print(f"   Answer ({len(answer)} chars) → spoken ({len(spoken)} chars) in {dt:.1f}s")
    print(f"   🗣️  Jo bola jayega: {spoken[:300]!r}")
    print()
    for label, ok in checks:
        print(f"   {'✅' if ok else '❌'} {label}")
    if res.get("errors"):
        print(f"   ⚠️ errors: {res['errors'][:1]}")
    hard_fail = [c for c, ok in checks if not ok]
    print()
    if hard_fail:
        print(f"   ❌ FAIL — {', '.join(hard_fail)}")
        return 1
    print("   ✅ PASS — voice pipeline sahi chal rahi hai (keys lagane ke baad sunai degi)")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
    # `voice --help` = humari help; lekin `voice console --help` LiveKit ki hi help honi chahiye
    # (mode ke saath --help → passthrough, taaki LiveKit ke apne options dikhein)
    mode_like = bool(argv) and argv[0] not in ("-h", "--help") and argv[0] in VOICE_MODES
    if not mode_like and any(a in ("-h", "--help") for a in argv):
        print(VOICE_HELP)
        return 0
    if argv and argv[0] == "keys":
        return cmd_keys()
    if argv and argv[0] in ("test", "dry-run"):
        return cmd_test(" ".join(argv[1:]))
    if argv and argv[0] == "status":
        print(voice_status_text().replace("*", "").replace("`", ""))
        return 0

    st = voice_status()
    if not st["ready"]:
        print("❌ Voice mode abhi ready nahi hai:\n" + "\n".join(f"  • {m}" for m in st["missing"]))
        print("\n➡️  Exact steps: python -m jarvis voice keys")
        print("➡️  Bina keys test: python -m jarvis voice test")
        print("➡️  Poori help: python -m jarvis voice --help")
        return 1

    if argv and str(argv[0]).startswith("-"):
        argv = ["console", *argv]  # `voice --list-devices` → console ke options
    mode = argv[0] if argv else "console"  # bina mode = console (menu option 2 bhi yahi hai)
    if mode not in VOICE_MODES:
        print(f"❌ '{mode}' voice ka mode nahi hai — ye chalte hain: {', '.join(VOICE_MODES)}")
        print(VOICE_HELP)
        return 2
    print(f"🎙️ {S.ASSISTANT_NAME} voice mode ({mode}) — model: {voice_model()}")
    print("   Mic par bolo. Ctrl+C se band (~10-20s lagta hai pehli baar).")
    try:
        return start_livekit(argv or ["console"]) or 0
    except KeyboardInterrupt:
        print("\n👋 voice band.")
        return 0
    except Exception as exc:  # honest error + actionable hint
        msg = str(exc)
        print(f"❌ voice fail: {type(exc).__name__}: {msg[:300]}")
        low = msg.lower()
        if isinstance(exc, ModuleNotFoundError) or "plugin" in low or "livekit" in low and "install" in low:
            print('   → pip install "livekit-agents[google]" livekit-plugins-google livekit-plugins-silero')
        if "device" in low or "audio" in low or "portaudio" in low:
            print("   → audio device list dekho: python -m jarvis voice console --list-devices")
            print("   → mic permission (Windows: Settings → Privacy → Microphone) on karo")
        if "credential" in low or "401" in low or "unauthorized" in low:
            print("   → keys dobara check: python -m jarvis voice keys")
        print("   → full steps: python -m jarvis voice keys  |  bina keys test: python -m jarvis voice test")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
