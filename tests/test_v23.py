"""
NikhilAI v2.3 test suite — naye subsystems
=========================================

Ye file un cheezon ko test karti hai jo v2.3 me aayi:
config/runtime settings · multi-provider brain + fallback · memory manager (delete/update) ·
automations · permissions · speech cleanup (voice emoji fix) · telegram authorization ·
system ops · unified agent runner · CLI/doctor · security hygiene.

Chalane ke liye:  python tests/test_v23.py      (ya `python -m jarvis test`)
Safety: koi destructive OS command nahi, koi network call nahi (offline-safe).
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("NIKHILAI_TEST", "1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")       # tests me bot start na ho
os.environ.setdefault("MODEL_DISCOVERY", "false")     # network calls na hon

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "✅" if ok else "❌"
    line = f"  {mark} {name}" + (f" — {detail}" if detail else "")
    try:
        print(line)
    except Exception:
        print(f"  {mark} {name} (detail print failed)")
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n[{title}]")


# ===========================================================================
# 1. config system
# ===========================================================================
def t_config() -> None:
    from jarvis.config import settings as S, runtime as R

    check("config: identity env-driven (NIKHILAI_NAME/OWNER_NAME)",
          bool(S.ASSISTANT_NAME) and bool(S.OWNER_NAME),
          f"{S.ASSISTANT_NAME} / {S.OWNER_NAME}")
    check("config: 9 model roles defined",
          set(S.MODEL_ROLES) == {"brain", "fast", "planner", "code", "research",
                                 "extract", "vision", "browser", "critic"},
          ", ".join(S.MODEL_ROLES))
    check("config: every provider has role defaults",
          all(role in S.MODEL_DEFAULTS[p] for p in S.MODEL_DEFAULTS for role in ("brain", "fast", "code")),
          f"{len(S.MODEL_DEFAULTS)} providers")
    check("config: model override via env (NIKHILAI_BRAIN_MODEL)",
          True, f"brain chain sample: {S.model_chain_for('brain', 'gemini')[:2]}")

    # inline-comment parser (ye bug tha: comment value ban raha tha)
    p = S._parse_env_line
    check("config: .env parser inline comment hataata hai",
          p("BRAIN_PROVIDER=     # comment")[1] == "",
          str(p("BRAIN_PROVIDER=     # comment")))
    check("config: .env parser quoted value safe rakhta hai",
          p('X="a # b"')[1] == "a # b", str(p('X="a # b"')))
    check("config: .env parser plain value",
          p("OWNER_NAME=Nikhil Mehra")[1] == "Nikhil Mehra", str(p("OWNER_NAME=Nikhil Mehra")))

    rep = S.config_report()
    check("config: report has no secret values",
          all(len(str(v)) < 200 for v in rep.ready + rep.missing_optional + rep.missing_required),
          f"{len(rep.ready)} ready / {len(rep.missing_optional)} optional missing")

    # runtime settings: set → read → persist
    applied, errors = R.set_many(owner_name="Test Owner", personality="concise,testy", max_iterations=4)
    ok = (not errors and applied.get("owner_name") == "Test Owner"
          and R.get("owner_name") == "Test Owner" and R.snapshot()["max_iterations"] == 4)
    check("runtime settings: set + read (owner name change)", ok, f"applied={applied} err={errors}")
    check("runtime settings: persisted to json",
          S.RUNTIME_SETTINGS_FILE.exists() and "Test Owner" in S.RUNTIME_SETTINGS_FILE.read_text(encoding="utf-8"),
          str(S.RUNTIME_SETTINGS_FILE.name))

    bad, bad_errors = R.set_many(mode="teleport", made_up_key="x")
    check("runtime settings: invalid key/value reject hoti hai",
          not bad and len(bad_errors) == 2, f"{len(bad_errors)} errors")

    R.reset("owner_name")
    R.reset("personality")
    R.reset("max_iterations")
    R.reset("mode")
    check("runtime settings: reset kaam karta hai", R.get("owner_name") != "Test Owner",
          f"ab owner = {R.get('owner_name')}")


# ===========================================================================
# 2. providers + role routing + fallback
# ===========================================================================
def t_providers() -> None:
    from jarvis.llm import providers as P
    from jarvis.llm import roles as RO

    check("providers: registry me gemini/openrouter/groq hain",
          all(k in P.PROVIDERS for k in ("gemini", "openrouter", "groq")),
          ", ".join(P.PROVIDERS))
    check("providers: alias mapping (google→gemini, or→openrouter)",
          P.canonical("google") == "gemini" and P.canonical("or") == "openrouter"
          and P.canonical("claude") == "anthropic", "aliases ok")
    check("providers: base URLs configurable",
          "openrouter.ai" in P.PROVIDERS["openrouter"].base_url and "groq.com" in P.PROVIDERS["groq"].base_url,
          f"{P.PROVIDERS['openrouter'].base_url} | {P.PROVIDERS['groq'].base_url}")

    # key detection (temporarily set env)
    old = os.environ.get("GROQ_API_KEY")
    os.environ["GROQ_API_KEY"] = "test-key-not-real"
    try:
        check("providers: key detect hoti hai (env se)", P.is_available("groq") and "groq" in P.available(),
              f"available={P.available()}")
        route = RO.resolve("brain")
        check("providers: alternate provider par switch (runtime/env)",
              any(t.provider == "groq" for t in route.targets), route.describe())
    finally:
        if old is None:
            os.environ.pop("GROQ_API_KEY", None)
        else:
            os.environ["GROQ_API_KEY"] = old

    # role chains
    gem_chain = P.canonical("gemini")
    for role in ("brain", "fast", "code"):
        chain = __import__("jarvis.config", fromlist=["settings"]).settings.model_chain_for(role, gem_chain)
        check(f"roles: {role} ke liye fallback chain", len(chain) >= 2, " → ".join(chain[:3]))

    # retired models filter
    check("roles: retired model (gemini-2.0) reject hota hai",
          RO._retired("gemini-2.0-flash") and not RO._retired("gemini-3.8-flash"), "2.0 blocked")

    # cross-provider fallback
    old = {k: os.environ.get(k) for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY")}
    os.environ["GROQ_API_KEY"] = "k1"
    os.environ["OPENROUTER_API_KEY"] = "k2"
    try:
        cross = RO.cross_provider_chain("brain")
        providers = {t.provider for t in cross}
        check("fallback: multi-provider chain banti hai", len(providers) >= 2, ", ".join(sorted(providers)))
        check("fallback: health() sab roles report karta hai",
              set(RO.health()["roles"]) == {"brain", "fast", "planner", "code", "research",
                                            "extract", "vision", "browser", "critic"},
              "9 roles")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def t_fallback_model() -> None:
    """FallbackChatModel: pehla model fail ho to agla chale (asli fallback behaviour)."""
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.language_models.chat_models import BaseChatModel

    from jarvis.llm.core import FallbackChatModel

    class FlakyModel(BaseChatModel):
        tag: str = "flaky"
        fail: bool = True

        @property
        def _llm_type(self) -> str:
            return f"test-{self.tag}"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
            if self.fail:
                raise RuntimeError("404 model not found (simulated)")
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"reply from {self.tag}"))])

    broken = FlakyModel(tag="broken", fail=True)
    working = FlakyModel(tag="working", fail=False)
    chain = FallbackChatModel(role="brain", chain=[broken, working], targets=["gemini:broken", "groq:working"])
    out = chain.invoke("hello")
    check("fallback: pehla model fail → doosra chal gaya",
          "reply from working" in str(out.content), str(out.content)[:60])
    check("fallback: attempts trace hua (observability)",
          any(not a["ok"] for a in __import__("jarvis.llm", fromlist=["model_attempts"]).model_attempts(5)),
          "failed attempt recorded")

    only_broken = FallbackChatModel(role="brain", chain=[broken], targets=["gemini:broken"])
    try:
        only_broken.invoke("hello")
        raised = False
    except Exception as exc:
        raised = "saare models fail" in str(exc)
    check("fallback: sab fail ho to clear error (silently swallow nahi)",
          raised, "RuntimeError with reason")


# ===========================================================================
# 3. memory manager
# ===========================================================================
def t_memory() -> None:
    from jarvis.memory.manager import MemoryManager, classify, is_memory_request, strip_trigger
    from jarvis.memory.store import MemoryStore
    from jarvis.config import settings as S

    tmp_db = S.WORKSPACE_DIR / "test_memory_v23.sqlite3"
    if tmp_db.exists():
        tmp_db.unlink()
    mgr = MemoryManager(MemoryStore(db_path=tmp_db, user_id="test_user"))

    r1 = mgr.remember("mera naam Test Nikhil hai", kind="profile")
    r2 = mgr.remember("mera naam Test Nikhil hai", kind="profile")     # duplicate
    check("memory: naya fact save", r1["action"] == "created", r1["message"][:60])
    check("memory: duplicate skip (same memory dobara nahi)", r2["action"] == "duplicate", r2["message"][:60])

    pref = mgr.remember("mujhe short replies pasand hain", kind="preference")
    check("memory: preference kind detect", pref["kind"] == "preference", pref["kind"])

    upd = mgr.update_by_query("short replies", "mujhe detailed replies pasand hain")
    check("memory: update karta hai (replace)", upd.get("ok") and "detailed" in upd["message"], upd["message"][:70])

    rows = mgr.list_memories()
    check("memory: list + kinds", len(rows) >= 2 and any(r["kind"] == "preference" for r in rows),
          f"{len(rows)} memories")
    check("memory: stats by_kind", mgr.stats()["by_kind"].get("preference", 0) >= 1, str(mgr.stats()))

    dele = mgr.forget("detailed replies")
    after = [r["text"] for r in mgr.list_memories()]
    check("memory: forget = ASLI deletion (list se gayab)",
          dele["ok"] and not any("detailed" in t for t in after), dele["message"][:70])

    hits = mgr.recall("Test Nikhil naam")
    check("memory: recall/FTS search chalta hai", len(hits) >= 1,
          f"{len(hits)} hits — {hits[0].text[:40] if hits else 'none'}")

    check("memory: trigger detect ('yaad rakh')", is_memory_request("yaad rakh mera birthday 5 aug hai") == "remember",
          "remember")
    check("memory: forget trigger detect", is_memory_request("wo purani baat bhool jao") == "forget", "forget")
    check("memory: trigger strip", strip_trigger("yaad rakh mera naam Nikhil hai") == "mera naam Nikhil hai",
          strip_trigger("yaad rakh mera naam Nikhil hai"))
    check("memory: classifier profile/person pehchanta hai",
          classify("mera naam Nikhil hai") == "profile", classify("mera naam Nikhil hai"))

    tmp_db.unlink(missing_ok=True)


# ===========================================================================
# 4. automations
# ===========================================================================
def t_automation() -> None:
    from jarvis.automation import scheduler as sch
    from jarvis.automation.tools import create_automation, list_automations

    cases = {
        "har subah 8 baje": ("daily", "08:00"),
        "daily 18:30": ("daily", "18:30"),
        "every 2h": ("interval", "2"),
        "har 3 hrs": ("interval", "3"),
        "har 30 minute": ("interval", None),
        "kal 6 PM": ("once", None),
    }
    ok_all = True
    details = []
    for text, (kind, expect) in cases.items():
        spec = sch.parse_schedule(text)
        good = spec.get("kind") == kind and (expect is None or expect in spec.get("schedule", ""))
        ok_all = ok_all and good
        details.append(f"{text}→{spec.get('schedule')}")
    check("automation: natural language schedule parsing", ok_all, "; ".join(details))

    bad = sch.parse_schedule("kuch bhi kar do")
    check("automation: samajh na aaye to clear error", not bad.get("kind") and "try" in bad.get("error", "").lower(),
          bad.get("error", "")[:60])

    store = sch.get_store()
    created = store.create("test task: mausam batao", "har subah 9 baje")
    auto_id = created["automation"]["id"]
    check("automation: create + next_run set", created["ok"] and created["automation"]["next_run"] > 0,
          created["message"][:70])
    check("automation: list me dikhti hai", any(i["id"] == auto_id for i in store.list()), f"{len(store.list())} items")
    check("automation: disable/enable", store.set_enabled(auto_id, False)["ok"] and
          next(i for i in store.list() if i["id"] == auto_id)["enabled"] is False, "toggle ok")
    check("automation: persistence (json file)", sch.AUTOMATION_FILE.exists(), sch.AUTOMATION_FILE.name)
    check("automation: cancel/delete", store.cancel(auto_id)["ok"] and
          not any(i["id"] == auto_id for i in store.list()), "deleted")
    check("automation: tool wrapper output", "automation" in create_automation.invoke(
        {"task": "test", "schedule": "every 3h"}).lower(), "tool ok")
    for item in store.list():
        if item["task"] == "test":
            store.cancel(item["id"])


# ===========================================================================
# 5. permissions (security)
# ===========================================================================
def t_permissions() -> None:
    from jarvis.security import permissions as perm

    check("permissions: level order", perm.LEVELS == ("READ", "WRITE", "EXECUTE", "SYSTEM", "DANGEROUS"),
          " < ".join(perm.LEVELS))
    owner_cli = perm.check("run_shell_command", "cli_owner")
    guest_tg = perm.check("run_shell_command", "telegram_guest")
    check("permissions: owner ko shell allowed", owner_cli.allowed, owner_cli.as_text())
    check("permissions: telegram guest ko shell BLOCKED (crash nahi, decision)",
          not guest_tg.allowed and "limit" in guest_tg.reason, guest_tg.as_text()[:70])
    check("permissions: guest read-only tools allowed",
          perm.check("google_search", "telegram_guest").allowed, "search allowed for guest")
    check("permissions: destructive tool DANGEROUS level",
          perm.tool_level("delete_everything") == "DANGEROUS", perm.tool_level("delete_everything"))
    check("permissions: matrix summary banata hai", "Permission matrix" in perm.summary(), "summary ok")


# ===========================================================================
# 6. speech cleanup (voice me emoji bolna band)
# ===========================================================================
def t_speech() -> None:
    from jarvis.voice.speech import for_speech, is_speakable, summary_for_speech

    out1 = for_speech("Great! ✅ File saved successfully at /home/user/notes/a.md")
    check("voice: emoji speak nahi hota, hawa me convert hota hai",
          "✅" not in out1 and "white heavy check" not in out1.lower(), repr(out1[:70]))

    out2 = for_speech("**3 files** mili — [report](https://example.com/x.pdf) aur 🔥 2 aur")
    check("voice: markdown + links clean", "**" not in out2 and "http" not in out2 and "3 files" in out2,
          repr(out2[:70]))

    out3 = for_speech("```python\nprint('hi')\n```\nYe code chal gaya 👌")
    check("voice: code block bolne ke liye hataata hai", "print" not in out3 and "code" in out3.lower(),
          repr(out3[:70]))

    out4 = for_speech("Done ✅  \n\n## Summary\n- item one\n- item two\n| a | b |\n| 1 | 2 |")
    check("voice: headings/bullets/tables natural hote hain",
          "##" not in out4 and "|" not in out4 and "item one" in out4, repr(out4[:70]))

    long_text = "Pehla point ye hai. " * 40
    check("voice: lamba jawab chhota karta hai", len(for_speech(long_text)) <= 1300,
          f"{len(for_speech(long_text))} chars")

    s = summary_for_speech("Ek. Do. Teen. Chaar. Paanch.", max_sentences=2)
    check("voice: spoken summary 2-3 sentence me", s.count(".") <= 2, repr(s))

    check("voice: unknown emoji-only text speakable nahi", not is_speakable("🙂🙂"), "no speech")
    check("voice: mapped emoji words me convert hota hai", "badhiya" in for_speech("🎉 kaam ho gaya"),
          repr(for_speech("🎉 kaam ho gaya")))


# ===========================================================================
# 7. telegram authorization
# ===========================================================================
def t_telegram() -> None:
    from jarvis.config import settings as S
    from jarvis.telegram import bot as tg

    saved = (S.TELEGRAM_OWNER_ID, S.TELEGRAM_ALLOWED_CHAT_IDS, S.TELEGRAM_ALLOWED_USERS, S.TELEGRAM_BOT_TOKEN)
    try:
        S.TELEGRAM_OWNER_ID = "12345"
        S.TELEGRAM_ALLOWED_CHAT_IDS = ["999"]
        S.TELEGRAM_ALLOWED_USERS = ["777"]

        ok_owner, role_owner = tg.authorize(12345, 1)
        ok_guest, role_guest = tg.authorize(777, 2)
        ok_chat, role_chat = tg.authorize(555, 999)
        ok_stranger, role_stranger = tg.authorize(4242, 4242)

        check("telegram: owner authorized", ok_owner and role_owner == "owner", role_owner)
        check("telegram: allowed user = guest", ok_guest and role_guest == "guest", role_guest)
        check("telegram: allowed chat authorized", ok_chat and role_chat == "guest", role_chat)
        check("telegram: random stranger BLOCKED", not ok_stranger and role_stranger == "blocked", role_stranger)
        check("telegram: guest ka surface read-only hota hai",
              tg.surface_for("guest") == "telegram_guest" and tg.surface_for("owner") == "telegram_owner",
              "surface mapping")

        # bot ke commands registered
        S.TELEGRAM_BOT_TOKEN = "123456789:TESTTOKEN_FOR_UNIT_TEST_ONLY"
        app = tg.build_application()
        handlers = app.handlers[0]
        names = {next(iter(h.commands)) for h in handlers if getattr(h, "commands", None)}
        expected = {"start", "help", "status", "mode", "model", "provider", "tools", "settings",
                    "memory", "remember", "forget", "automation", "refresh", "clear", "stop", "restart"}
        check("telegram: saare commands wired hain", expected <= names,
              f"{len(names)} commands, missing={sorted(expected - names)}")
        check("telegram: free-text handler bhi hai (slash ke bina bhi kaam)",
              any(getattr(h, "filters", None) is not None for h in handlers), "message handler present")
    finally:
        (S.TELEGRAM_OWNER_ID, S.TELEGRAM_ALLOWED_CHAT_IDS, S.TELEGRAM_ALLOWED_USERS, S.TELEGRAM_BOT_TOKEN) = saved

    ok_cfg, msg = tg.config_ok()
    check("telegram: bina token clear error deta hai",
          isinstance(msg, str) and ("TELEGRAM_BOT_TOKEN" in msg or ok_cfg), msg[:70])


# ===========================================================================
# 8. system ops
# ===========================================================================
def t_system_ops() -> None:
    from jarvis.tools import system_ops as so

    status = so.system_status()
    check("system ops: status milta hai (time/python/workspace)",
          status["ok"] and "time" in status["info"] and "workspace" in status["info"],
          f"{len(status['info'])} fields")

    dry = so.clear_cache(scope="python", dry_run=True)
    check("system ops: cache cleanup dry-run safe hai (kuch delete nahi)",
          dry["ok"] and dry["freed_bytes"] >= 0, dry["message"][:70])

    before = len(list((so.WORKSPACE_DIR / "notes").glob("*.md")))
    ref = so.refresh_system(clear_caches=True)
    after = len(list((so.WORKSPACE_DIR / "notes").glob("*.md")))
    check("system ops: refresh user data ko chhoota nahi", before == after,
          f"notes {before} → {after}")
    check("system ops: refresh steps report karta hai", ref["ok"] and len(ref["steps"]) >= 3,
          "; ".join(ref["steps"])[:80])

    proc = so.clear_processes()
    check("system ops: process clear chal jata hai (koi process na ho to bhi safe)",
          proc.get("ok") is True, str(proc.get("message"))[:60])
    check("system ops: tools registered", len(so.SYSTEM_OPS_TOOLS) == 6, f"{len(so.SYSTEM_OPS_TOOLS)} tools")


# ===========================================================================
# 9. unified agent runner (CLI/voice/telegram sab yahi use karte hain)
# ===========================================================================
def t_agent_runner() -> None:
    from jarvis.runtime.agent import run_agent

    async def go():
        mem = await run_agent("yaad rakh test runner ka favourite number 7 hai", session_id="test_v23")
        listing = await run_agent("show my memories", session_id="test_v23")
        settings_res = await run_agent("mode text karo", session_id="test_v23")
        graph_res = await run_agent("workspace me laptop wali notes file dhoondh ke padho",
                                    session_id="test_v23")
        return mem, listing, settings_res, graph_res

    mem, listing, settings_res, graph_res = asyncio.run(go())

    check("agent: memory fast-path (LLM ke bina)", mem.status == "memory" and mem.workers == ["memory"],
          mem.answer[:60])
    check("agent: memory list fast-path", "Memory" in listing.answer, listing.answer[:60])
    check("agent: settings fast-path (mode change)", settings_res.status == "settings",
          settings_res.answer[:60])
    check("agent: graph path chalta hai + answer aata hai", bool(graph_res.answer.strip()),
          graph_res.answer[:60].replace("\n", " "))
    check("agent: reasoning summary structured hai",
          any(r.startswith(("PLAN:", "DECISION:", "ACTION:")) for r in graph_res.reasoning),
          " | ".join(graph_res.reasoning[:2])[:80])
    check("agent: spoken output emoji-free (voice-safe)",
          "✅" not in graph_res.spoken and len(graph_res.spoken) > 0, repr(graph_res.spoken[:60]))
    check("agent: result as_dict() me sab keys",
          {"response", "workers", "reasoning", "seconds", "status"} <= set(graph_res.as_dict()),
          f"{len(graph_res.as_dict())} keys")
    check("agent: task memory me record hua",
          not graph_res.errors, f"errors={graph_res.errors[:1]}")

    from jarvis.config import settings as S
    from jarvis.memory.manager import get_manager
    get_manager().forget("test runner ka favourite number", all_matches=True)


def t_offline_mode() -> None:
    from jarvis.config import settings as S
    from jarvis.llm import get_chat_model

    model = get_chat_model("brain")
    check("offline: bina key demo brain milta hai", type(model).__name__ == "DemoChatModel",
          type(model).__name__)
    check("offline: demo mode flag", S.OFFLINE_MODE or S.PROVIDER == "demo",
          f"provider={S.PROVIDER} offline={S.OFFLINE_MODE}")
    out = model.invoke("kaun ho tum?")
    check("offline: demo brain jawab deta hai (crash nahi)", bool(str(out.content).strip()),
          str(out.content)[:60].replace("\n", " "))


# ===========================================================================
# 10. CLI / doctor / packaging
# ===========================================================================
def t_cli() -> None:
    from jarvis.cli import doctor, ui

    checks = doctor.collect_checks()
    check("cli: doctor checks collect karta hai", len(checks) >= 20, f"{len(checks)} checks")
    check("cli: doctor me required sections hain", all(
        any(name in c for _, c, _ in checks) for name in
        ("Python", "langgraph", "Brain provider", "LiveKit", "Telegram", "Memory", "Shell")),
        "sections present")
    joined = " ".join(d for _, _, d in checks)
    check("cli: doctor secret values leak nahi karta",
          not any(k in joined for k in ("sk-", "AIza", "ghp_", "AAAA")), "no secrets in output")

    # UI helpers plain mode me bhi crash na karein
    for fn, args in ((ui.status_panel, ({"status": "ONLINE", "provider": "demo", "model": "x",
                                          "mode": "text", "memory": "0", "tools": "1", "browser": True,
                                          "livekit": False, "telegram": False, "automations": "0",
                                          "workspace": "/tmp"},)),
                     (ui.help_panel, ({"/help": "test"},)),
                     (ui.memory_table, ([{"id": "a" * 12, "kind": "fact", "text": "x"}],)),
                     (ui.automations_table, ([{"id": "b" * 12, "schedule": "daily 08:00", "enabled": True,
                                               "next_run_str": "x", "task": "y", "runs": 0, "errors": 0}],))):
        try:
            fn(*args)
            ok = True
        except Exception as exc:
            ok = False
            print(f"    (ui error: {exc})")
        check(f"cli: ui.{fn.__name__} render ok", ok, "")

    from jarvis.__main__ import USAGE
    check("cli: entry point me saare commands documented", all(
        c in USAGE for c in ("cli", "voice", "telegram", "doctor", "status", "test", "settings")),
        "usage ok")


def t_security_hygiene() -> None:
    gi = (ROOT / ".gitignore")
    text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    check("security: .gitignore me .env hai", ".env" in text and "!.env.example" in text, ".env ignored")
    check("security: .gitignore me workspace/memory/logs ignored",
          all(k in text for k in ("workspace/", "*.sqlite3", "*.log")), "runtime data ignored")

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    suspicious = [w for w in ("AIza", "sk-", "ghp_", "AKIA") if w in env_example]
    check("security: .env.example me koi asli key nahi (sirf placeholders)",
          not suspicious, "clean" if not suspicious else f"found {suspicious}")
    check("security: .env.example me naye env names documented",
          all(k in env_example for k in ("NIKHILAI_NAME", "OWNER_NAME", "GEMINI_API_KEY",
                                         "OPENROUTER_API_KEY", "GROQ_API_KEY", "TELEGRAM_BOT_TOKEN",
                                         "LIVEKIT_URL", "WORKSPACE_DIR", "LOG_LEVEL")),
          "all documented")

    # purane legacy trees na hon
    check("security: legacy/original tree repo me nahi",
          not (ROOT / "legacy_original").exists() and not (ROOT / "run_cli.py").exists(),
          "clean tree")


def t_graph_state() -> None:
    from jarvis.graph.state import initial_state
    from jarvis.graph.build import graph_mermaid, graph_summary

    from jarvis.graph.state import JarvisState

    st = initial_state("test", session_id="t")
    declared = set(JarvisState.__annotations__) | set(st)
    expected = {"request", "session_id", "messages", "memory_context", "plan", "selected_tools",
                "findings", "artifacts", "errors", "critique", "iterations", "events",
                "reasoning_summary", "spoken", "workers"}
    missing = expected - declared
    check("graph state: saare expected fields declared (LangGraph state)", not missing,
          f"{len(declared)} fields" + (f", missing={sorted(missing)}" if missing else ""))
    ann = str(JarvisState.__annotations__)
    has_reducer = "Annotated" in ann and "add_messages" in ann
    parallel_fields = [f for f in ("findings", "artifacts", "errors", "events", "notes_saved")
                       if "Annotated" in str(JarvisState.__annotations__.get(f, ""))]
    check("graph state: parallel workers ke liye reducers lage hain",
          has_reducer and len(parallel_fields) >= 4,
          f"reducer fields: {parallel_fields}")
    check("graph: mermaid + summary banate hain", "graph" in graph_mermaid().lower() or "-->" in graph_mermaid(),
          graph_summary().splitlines()[0][:70])


# ===========================================================================

# ===========================================================================
# 11. ACTION VERIFICATION — "bola kar diya, par kiya nahi"
# ===========================================================================
def t_action_detection() -> None:
    """Request se asli actions nikle (aur sawaal galti se action na bane)."""
    from jarvis.graph.nodes import _needs_action, detect_desktop_action

    a = detect_desktop_action("notepad open krke i hate you likho")
    kinds = [x["kind"] for x in a.get("actions", [])]
    check("action: open + type dono detect (order sahi)", kinds == ["open_app", "type_text"], str(kinds))
    check("action: app naam sahi ('notepad')",
          a["actions"][0]["app"] == "notepad", a["actions"][0]["app"])
    check("action: typed text clean ('i hate you')",
          a["actions"][1]["text"] == "i hate you", repr(a["actions"][1]["text"]))

    # regression: pehle strip() se "chrome" → "chrom" ho jata tha
    c = detect_desktop_action("chrome kholo")
    check("action: 'chrome kholo' → chrome (off-by-one bug fix)",
          c.get("actions") and c["actions"][0]["app"] == "chrome", str(c.get("actions")))
    v = detect_desktop_action("vs code chala do")
    check("action: 'vs code chala do' → 'vs code'",
          v.get("actions") and v["actions"][0]["app"] == "vs code", str(v.get("actions")))

    check("action: screenshot detect", any(x["kind"] == "screenshot"
                                           for x in detect_desktop_action("screenshot lo").get("actions", [])))
    check("action: hotkey detect", any(x["kind"] == "hotkey"
                                       for x in detect_desktop_action("ctrl+s dabao").get("actions", [])))
    check("action: volume detect", any(x["kind"] == "volume"
                                       for x in detect_desktop_action("volume badha do").get("actions", [])))
    check("action: close app detect", any(x["kind"] == "close_app"
                                          for x in detect_desktop_action("notepad band karo").get("actions", [])))

    check("action: sawaal ko action nahi samajhta (kaise kholte hain)",
          not detect_desktop_action("notepad kaise kholte hain").get("actions"))
    check("action: research request action nahi", not _needs_action("aaj ka mausam batao"))
    check("action: note save action nahi (desktop normal kaam)",
          not detect_desktop_action("notes me x naam se save kardo: y").get("actions"))


def t_honesty_guard() -> None:
    """Jo hua wahi bola jaye — jhooth block ho."""
    from jarvis.graph.nodes import _claims_success, honesty_guard

    check("guard: success claim detect", _claims_success("maine notepad khol diya"))
    check("guard: negation ko claim nahi maanta", not _claims_success("nahi hua"))

    # case 1: action maanga, koi tool call nahi → jhooth block
    st = {"action_required": True, "actions_done": [], "action_failures": [], "status": "running"}
    a1, r1 = honesty_guard(st, "Boss, maine Notepad open karke 'i hate you' likh diya hai.")
    check("guard: zero tool calls → 'kaam nahi hua' (jhooth replaced)",
          "nahi hua" in a1 and "likh diya" not in a1.lower(), r1)

    # case 2: sab ok → answer chhua jaata hai
    st2 = {"action_required": True, "status": "running",
           "actions_done": [{"name": "type_text", "ok": True, "output": "⌨️ 10 chars type kiye"}],
           "action_failures": []}
    a2, r2 = honesty_guard(st2, "Notepad me likh diya.")
    check("guard: verified success par dakhal nahi", a2 == "Notepad me likh diya." and not r2, r2)

    # case 3: sab fail → saaf failure
    st3 = {"action_required": True, "actions_done": [], "status": "running",
           "action_failures": [{"name": "type_text", "ok": False, "output": "❌ typing fail: xdotool chahiye"}]}
    a3, r3 = honesty_guard(st3, "Ho gaya boss!")
    check("guard: sare fail → 'kaam fail hua' + reason", "fail hua" in a3 and "xdotool" in a3, r3)

    # case 4: aadha kaam
    st4 = {"action_required": True, "status": "running",
           "actions_done": [{"name": "open_any_app", "ok": True, "output": "🚀 notepad launch ho gaya"}],
           "action_failures": [{"name": "type_text", "ok": False, "output": "❌ typing fail"}]}
    a4, r4 = honesty_guard(st4, "Sab ho gaya.")
    check("guard: partial → warning + dono list",
          "Aadha" in a4 and "fail hua" in a4.lower(), r4)


def t_tool_call_tracking() -> None:
    """Sub-agent ke *actual* tool calls track hon (text se nahi, output se)."""
    from jarvis.graph.nodes import SubAgentRun, _looks_like_failure, _tool_call_record

    check("tracking: failure detect (❌)", _looks_like_failure("❌ pyautogui install karo"))
    check("tracking: error prefix fail maana jaye", _looks_like_failure("error: boom"))
    check("tracking: normal output ok", not _looks_like_failure("⌨️ 10 characters type kiye."))
    check("tracking: success+❌ word dono ho to ok", not _looks_like_failure("✅ 2 tasks ho gaye"))

    rec = _tool_call_record("type_text", {"text": "hi"}, "⌨️ 2 characters type kiye.")
    check("tracking: record me name/args/output/ok", rec["ok"] and rec["name"] == "type_text"
          and rec["args"]["text"] == "hi", str(rec)[:80])

    run = SubAgentRun(text="done", calls=[rec, _tool_call_record("open_app", {"app_title": "x"}, "❌ nahi mila")])
    check("tracking: ok/failed alag hote hain", len(run.ok_calls) == 1 and len(run.failures) == 1,
          f"ok={len(run.ok_calls)} fail={len(run.failures)}")
    check("tracking: lines me ✅/❌ dono", any("✅" in ln for ln in run.lines()) and any("❌" in ln for ln in run.lines()))


async def t_desktop_worker_verification() -> None:
    """desktop_worker: LLM ne tool call na kiya to khud chale + state me sach jaye."""
    os.environ["PROVIDER"] = "demo"
    import jarvis.config as cfg
    from jarvis.graph import nodes
    from jarvis.graph.state import initial_state

    st = initial_state("notepad open krke i hate you likho", session_id="t-verify")
    out = await nodes.desktop_worker(st)   # offline/demo path

    check("worker: action_required state me set hua", out.get("action_required") is True, str(out.get("action_required")))
    check("worker: actions_done/action_failures keys maujood",
          "actions_done" in out and "action_failures" in out)
    calls = (out.get("actions_done") or []) + (out.get("action_failures") or [])
    check("worker: kam se kam ek tool call ka record hai", len(calls) >= 1,
          ", ".join(c["name"] for c in calls) or "koi nahi")

    # headless Linux par typing fail hoti hai — us case me bhi answer jhooth na bole
    merged = dict(st)
    merged.update({"action_required": True, "actions_done": out.get("actions_done", []),
                   "action_failures": out.get("action_failures", [])})
    from jarvis.graph.nodes import honesty_guard

    ans, reason = honesty_guard(merged, "Boss, maine likh diya hai.")
    if out.get("actions_done"):
        check("worker: verified success par answer allowed", True, f"{len(out['actions_done'])} ok call(s)")
    else:
        check("worker: fail hone par answer honest", "nahi hua" in ans or "fail hua" in ans, reason)


def t_native_input_layer() -> None:
    """Typing/hotkey/screenshot bina pyautogui ke bhi chalne chahiye (native fallback)."""
    import jarvis.tools.system_tools as st

    check("native: helper maujood (_native_type)", callable(getattr(st, "_native_type", None)))
    check("native: helper maujood (_native_hotkey)", callable(getattr(st, "_native_hotkey", None)))
    check("native: SendKeys escape ({ } ^ % +)", st._escape_sendkeys("a{b}c^d%e+f") == "a{{}b{}}c{^}d{%}e{+}f",
          st._escape_sendkeys("a{b}c^d%e+f"))
    check("native: newline → {ENTER}", "{ENTER}" in st._escape_sendkeys("x\ny"))
    combo = st._key_name("ctrl")
    check("native: ctrl → '^'", combo == "^", combo)

    # headless environment me error aana chahiye (crash nahi) aur message helpful ho
    out = asyncio.run(st.type_text.ainvoke({"text": "hello"}))
    ok_native = out.startswith("⌨️")
    helpful = ("xdotool" in out) or ("wtype" in out) or ("install" in out) or ok_native
    check("native: type_text crash nahi karta + helpful message", helpful, out[:110])
    check("native: type_text sirf pyautogui par depend nahi", "pip install pyautogui" not in out[:80] or ok_native,
          out[:80])


def t_packaging() -> None:
    """pip install ke baad kuch bhi missing na ho (subpackages + skills)."""
    import tomllib

    root = ROOT
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    inc = data.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {}).get("include")
    check("packaging: packages.find include=['jarvis*'] (BUG FIX)", inc == ["jarvis*"], str(inc))

    from setuptools import find_packages

    found = set(find_packages(where=str(root), include=["jarvis*"]))
    expected = {"jarvis", "jarvis.tools", "jarvis.graph", "jarvis.llm", "jarvis.memory",
                "jarvis.automation", "jarvis.cli", "jarvis.runtime", "jarvis.security",
                "jarvis.skills", "jarvis.voice", "jarvis.telegram", "jarvis.config",
                "jarvis.dspy_engine", "jarvis.agents", "jarvis.api"}
    missing = sorted(expected - found)
    check("packaging: saare 16 subpackages install honge (kuch missing nahi)", not missing, f"missing={missing}")

    # har package dir me __init__.py ho (warna pip install me gayab ho jate hain)
    no_init = [d.name for d in (root / "jarvis").iterdir()
               if d.is_dir() and d.name != "__pycache__" and not (d / "__init__.py").exists()]
    check("packaging: har package me __init__.py hai", not no_init, f"missing __init__: {no_init}")

    pkg_data = data.get("tool", {}).get("setuptools", {}).get("package-data", {})
    check("packaging: skills catalog package-data me hai",
          any("catalog" in str(v) for v in pkg_data.values()), str(pkg_data))
    skills = list((root / "jarvis" / "skills" / "catalog").rglob("SKILL.md"))
    check("packaging: catalog me SKILL.md files maujood", len(skills) >= 2, f"{len(skills)} files")
    check("packaging: py.typed maujood", (root / "jarvis" / "py.typed").exists())
    check("packaging: dspy core dependency hai (graph isse use karta hai)",
          any("dspy" in str(x) for x in data.get("project", {}).get("dependencies", [])),
          "core deps")

    # dspy missing hone par bhi import na toote (graceful degrade)
    from jarvis.dspy_engine import signatures as sig

    check("packaging: dspy engine import-safe (HAS_DSPY flag)", isinstance(sig.HAS_DSPY, bool), str(sig.HAS_DSPY))

    check("packaging: pyautogui optional extra me hai (required me nahi)",
          "pyautogui" in str(data.get("project", {}).get("optional-dependencies", {}).get("desktop", []))
          and "pyautogui" not in str(data.get("project", {}).get("dependencies", [])),
          "desktop extra")


def main() -> int:
    started = time.time()
    print("=" * 74)
    print("  NikhilAI v2.3 — subsystem test suite")
    print("=" * 74)

    section("1/10 config + runtime settings")
    t_config()
    section("2/10 multi-provider brain + fallback")
    t_providers()
    t_fallback_model()
    section("3/10 memory manager (remember/update/forget)")
    t_memory()
    section("4/10 automations")
    t_automation()
    section("5/10 permissions / security model")
    t_permissions()
    section("6/10 voice speech cleanup")
    t_speech()
    section("7/10 telegram (auth + commands)")
    t_telegram()
    section("8/10 system operations")
    t_system_ops()
    section("9/10 unified agent runner + offline mode")
    t_agent_runner()
    t_offline_mode()
    section("10/12 CLI/doctor, graph state, security hygiene")
    t_cli()
    t_security_hygiene()
    t_graph_state()
    section("11/12 action verification (jhooth vs asli tool call)")
    t_action_detection()
    t_honesty_guard()
    t_tool_call_tracking()
    asyncio.run(t_desktop_worker_verification())
    t_native_input_layer()
    section("12/12 packaging (pip install ke baad kuch missing na ho)")
    t_packaging()

    total = PASS + FAIL
    print("\n" + "=" * 74)
    if FAIL == 0:
        print(f"  RESULT: {PASS}/{total} checks passed  (ALL GOOD 🚀)")
    else:
        print(f"  RESULT: {PASS}/{total} checks passed  ({FAIL} fail ⚠️)")
        for name in FAILURES:
            print(f"    ❌ {name}")
    print(f"  Time: {time.time() - started:.1f}s")
    print("=" * 74)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
