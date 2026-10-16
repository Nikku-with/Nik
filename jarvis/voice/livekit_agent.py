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

import asyncio
import inspect
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import runtime as R
from ..config import settings as S
from ..runtime.agent import run_agent
from ..voice.speech import for_speech, summary_for_speech

SESSION = "voice"


# ---------------------------------------------------------------------------
# graph bridge — voice tools isi ko call karte hain
# ---------------------------------------------------------------------------
async def run_nikhilai(
    query: str,
    session_id: str = SESSION,
    spoken: bool = True,
    approval_handler: Callable[..., Awaitable[bool | None] | bool | None] | None = None,
) -> dict[str, Any]:
    """Voice request uses the shared graph and an optional session-local human approval handler."""
    if approval_handler is None:
        result = await run_agent(query, session_id=session_id, surface="voice_owner", voice_output=spoken)
    else:
        from ..security.gate import use_approval_handler

        with use_approval_handler(approval_handler):
            result = await run_agent(query, session_id=session_id, surface="voice_owner", voice_output=spoken)
    # Voice replies are concise, while machine-readable fields still report real graph evidence.
    spoken_text = summary_for_speech(result.spoken or result.answer, max_sentences=3)
    return {
        "answer": result.answer,
        "spoken": for_speech(spoken_text),
        "workers": result.workers,
        "reasoning": result.reasoning,
        "artifacts": result.artifacts,
        "errors": result.errors,
        "status": result.status,
        "verified_execution": result.verified_execution,
        "memory_persisted": result.memory_persisted,
        "blocked_tools": result.blocked_tools,
        "seconds": result.seconds,
    }


# backwards-compatible alias (purana naam)
async def run_jarvis_graph(query: str, session_id: str = SESSION) -> dict[str, Any]:
    return await run_nikhilai(query, session_id=session_id)


def _yes_no(transcript: str) -> bool | None:
    """Accept only a clear spoken yes/no; anything else is a safe cancellation."""
    normalized = re.sub(r"[^\w\s]", " ", str(transcript or "").lower())
    normalized = " ".join(normalized.split())
    affirmative = {"yes", "yes run", "yes do it", "haan", "haan chalao", "haan kar do", "ha", "ha chalao", "kar do"}
    negative = {"no", "no cancel", "nahi", "nahin", "mat karo", "cancel", "stop"}
    if normalized in affirmative:
        return True
    if normalized in negative:
        return False
    return None


async def spoken_confirmation(session: Any, transcript_queue: asyncio.Queue[str], request: Any,
                              timeout_s: float = 60.0) -> bool:
    """Prompt the owner aloud and return True only for a clear, fresh yes transcript."""
    while not transcript_queue.empty():
        try:
            transcript_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    command = str(getattr(request, "command", "sensitive action"))[:280]
    why = str(getattr(request, "why_flagged", "This action needs approval"))[:160]
    prompt = f"{why}. Command: {command}. Kya chalaoon? Haan ya nahi bolo."
    try:
        await session.say(prompt, allow_interruptions=True)
    except TypeError:
        await session.say(prompt)
    try:
        heard = await asyncio.wait_for(transcript_queue.get(), timeout=max(0.1, timeout_s))
    except asyncio.TimeoutError:
        return False
    return _yes_no(heard) is True


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
Rule 5: Kuch na samajh aaye to ek chhota sa sawaal poocho, phir kaam karo."""


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
        from livekit.agents import Agent, AgentSession, RunContext, function_tool
        from livekit.plugins import google
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "LiveKit agents installed nahi hain. Chalao:\n"
            '  pip install "livekit-agents[google]" livekit-plugins-google livekit-plugins-silero\n'
            f"(detail: {exc})"
        ) from exc

    active_approval_handler: Callable[..., Awaitable[bool | None] | bool | None] | None = None
    active_session: Any = None
    active_session_id = SESSION
    active_loop: asyncio.AbstractEventLoop | None = None

    async def _delayed_status() -> None:
        await asyncio.sleep(2.0)
        if active_session is None:
            return
        try:
            await active_session.say("Ek second, check kar raha hoon.", allow_interruptions=True)
        except TypeError:
            await active_session.say("Ek second, check kar raha hoon.")
        except Exception:
            return

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
        status_task = asyncio.create_task(_delayed_status()) if active_session is not None else None
        try:
            res = await run_nikhilai(
                request, session_id=active_session_id, approval_handler=active_approval_handler,
            )
        except Exception as exc:
            return f"Task fail hua: {type(exc).__name__}. Main successful result confirm nahi kar saka."
        finally:
            if status_task is not None:
                status_task.cancel()
                try:
                    await status_task
                except asyncio.CancelledError:
                    pass
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
        """Owner ki memory delete karne se pehle spoken yes/no confirmation maango."""
        from types import SimpleNamespace

        if active_approval_handler is None:
            return "Memory delete cancel kiya; spoken confirmation channel unavailable hai."
        request = SimpleNamespace(
            id=uuid.uuid4().hex[:12], command=f"forget memory about {about[:160]}",
            why_flagged="Memory delete permanent hai", cwd="memory", surface="voice_owner",
            expires_at=time.time() + 60.0,
        )
        try:
            response = active_approval_handler(request)
            response = await response if inspect.isawaitable(response) else response
        except Exception:
            response = False
        if response is not True:
            return "Memory delete cancel kiya; clear haan nahi suna. Kuch delete nahi hua."
        from ..memory.manager import get_manager

        out = get_manager().forget(about)
        return out["message"]

    async def entrypoint(ctx: agents.JobContext) -> None:
        nonlocal active_approval_handler, active_session, active_session_id, active_loop
        await ctx.connect()
        active_loop = asyncio.get_running_loop()
        transcripts: asyncio.Queue[str] = asyncio.Queue()
        active_session_id = f"voice:{getattr(ctx.room, 'name', 'room')}:{uuid.uuid4().hex[:8]}"
        realtime = google.realtime.RealtimeModel(
            model=voice_model(), api_key=S.GEMINI_API_KEY,
            voice=str(R.get("voice_name", S.VOICE_NAME)), instructions=voice_instructions(),
        )
        session = AgentSession(llm=realtime)
        active_session = session

        def on_transcription(event: Any) -> None:
            text = str(getattr(event, "transcript", "") or getattr(event, "text", "") or "").strip()
            if not text or getattr(event, "is_final", True) is False:
                return
            try:
                transcripts.put_nowait(text)
            except asyncio.QueueFull:
                pass

        on = getattr(session, "on", None)
        if callable(on):
            on("user_input_transcribed", on_transcription)

        async def approve_by_voice(request: Any) -> bool:
            return await spoken_confirmation(session, transcripts, request, timeout_s=60.0)

        def approve_scheduled_by_voice(request: Any) -> bool:
            if active_loop is None:
                return False
            future = asyncio.run_coroutine_threadsafe(approve_by_voice(request), active_loop)
            try:
                return bool(future.result(timeout=61.0))
            except Exception:
                future.cancel()
                return False

        def deliver_scheduled_by_voice(event: dict[str, Any]) -> None:
            if active_loop is None:
                raise RuntimeError("voice session loop unavailable")
            text = summary_for_speech(
                str(event.get("result") or event.get("error") or "Scheduled task finished."), max_sentences=3,
            )
            future = asyncio.run_coroutine_threadsafe(
                session.say(for_speech(text), allow_interruptions=True), active_loop,
            )
            future.result(timeout=15.0)

        from ..automation import scheduler as sch
        from ..security.gate import register_approval_handler

        sch.configure_agent_runner()
        sch.register_delivery_handler("voice_owner", deliver_scheduled_by_voice)
        register_approval_handler("voice_owner", approve_scheduled_by_voice)
        sch.start_background()
        active_approval_handler = approve_by_voice
        agent = Agent(instructions=voice_instructions(),
                      tools=[current_time, agent_task, remember, forget])
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
        try:
            from ..automation import scheduler as sch
            from ..security.gate import register_approval_handler

            sch.register_delivery_handler("voice_owner", None)
            register_approval_handler("voice_owner", None)
            sch.stop_background()
        except Exception:
            pass
        active_session = None
        active_loop = None


def voice_model() -> str:
    return str(R.get("voice_model", S.REALTIME_MODEL))


def _module_available(name: str) -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def voice_status() -> dict[str, Any]:
    """Voice readiness with exact missing config keys and optional plugin packages."""
    library = _module_available("livekit.agents")
    google_plugin = _module_available("livekit.plugins.google")
    silero_plugin = _module_available("livekit.plugins.silero")
    keys = {
        "LIVEKIT_URL": bool(S.LIVEKIT_URL),
        "LIVEKIT_API_KEY": bool(S.LIVEKIT_API_KEY),
        "LIVEKIT_API_SECRET": bool(S.LIVEKIT_API_SECRET),
    }
    gemini = bool(S.GEMINI_API_KEY)
    provider = str(R.get("provider", S.PROVIDER) or "").lower()
    model = voice_model()
    missing_dependencies: list[str] = []
    if not library:
        missing_dependencies.append('livekit-agents[google]')
    if not google_plugin:
        missing_dependencies.append('livekit-plugins-google')
    if not silero_plugin:
        missing_dependencies.append('livekit-plugins-silero')
    missing_config: list[str] = []
    if not gemini:
        missing_config.append("GEMINI_API_KEY (Google AI Studio realtime provider)")
    if provider not in {"google", "gemini", "auto", "demo"}:
        missing_config.append(f"voice provider setting is '{provider or 'unset'}'; set provider=google")
    if not model:
        missing_config.append("VOICE_MODEL / REALTIME_MODEL")
    missing_cloud: list[str] = []
    for key, present in keys.items():
        if not present:
            missing_cloud.append(f"{key} (LiveKit Cloud project settings)")
    console_ready = library and google_plugin and gemini and bool(model)
    cloud_ready = console_ready and all(keys.values())
    missing = [f"pip install {package}" for package in missing_dependencies] + missing_config
    return {
        "ready": console_ready, "console_ready": console_ready, "cloud_ready": cloud_ready,
        "status": "local_console_ready" if console_ready else "missing_console_setup",
        "connection_active": None, "connection_checked": False,
        "library": library, "google_plugin": google_plugin, "silero_plugin": silero_plugin,
        "keys": keys, "gemini": gemini, "provider": provider, "model": model,
        "voice": str(R.get("voice_name", S.VOICE_NAME)), "missing": missing,
        "missing_config": missing_config, "missing_dependencies": missing_dependencies,
        "cloud_missing": missing_cloud,
    }


def voice_status_text() -> str:
    st = voice_status()
    lines = [
        "🎙️ *Voice readiness (active room/mic connection is not checked here)*",
        f"• Local console ready: {'✅ haan' if st['console_ready'] else '❌ abhi nahi'}",
        f"• LiveKit cloud start ready: {'✅ haan' if st['cloud_ready'] else '❌ setup incomplete'}",
        f"• voice: `{st['voice']}`  •  realtime model: `{st['model']}`",
        f"• Provider setting: `{st['provider'] or 'unset'}`",
    ]
    if st["missing_dependencies"]:
        lines.append("• Missing packages:")
        lines += [f"   – pip install {item}" for item in st["missing_dependencies"]]
    if st["missing_config"]:
        lines.append("• Missing local voice configuration:")
        lines += [f"   – {item}" for item in st["missing_config"]]
    if st["cloud_missing"]:
        lines.append("• Cloud dev/start additionally requires:")
        lines += [f"   – {item}" for item in st["cloud_missing"]]
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
    if argv and str(argv[0]).startswith("-"):
        argv = ["console", *argv]              # `voice --list-devices` → console ke options
    mode = argv[0] if argv else "console"      # bina mode = console (menu option 2 bhi yahi hai)
    if mode not in VOICE_MODES:
        print(f"❌ '{mode}' voice ka mode nahi hai — ye chalte hain: {', '.join(VOICE_MODES)}")
        print(VOICE_HELP)
        return 2
    if not st["console_ready"]:
        print("❌ Voice console start nahi hua; ye exact setup missing hai:")
        for missing in st["missing"]:
            print(f"  • {missing}")
        print("➡️  Exact steps: python -m jarvis voice keys | offline test: python -m jarvis voice test")
        return 1
    if mode in {"dev", "start", "connect"} and not st["cloud_ready"]:
        print("❌ LiveKit cloud voice start nahi hua; ye config missing hai:")
        for missing in st["cloud_missing"]:
            print(f"  • {missing}")
        print("➡️  Setup: python -m jarvis voice keys")
        return 1
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
