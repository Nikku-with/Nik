"""
tests/test_cli_routing.py — `python -m jarvis <kuch bhi>` ka routing suite
=========================================================================

Chalao:  python tests/test_cli_routing.py     (pytest bhi chalega)

Ye test un do sawaalon ka jawab deta hai jo user ne poochhe:
  1. `python -m jarvis hy` → AI ke paas jaata hai? (haan — koi rigid prefix nahi chahiye)
  2. `python -m jarvis console` → LiveKit console samajhta hai? (haan — alias hai)

Checks:
  ✅ exact aliases (console / mic / talk / keys / tg / bot / doctor / st / menu …)
  ✅ filler shabd ("console chalao", "chat karo", "voice mode start")
  ✅ command args intact ("voice dev", "voice test", "settings owner_name Nikhil")
  ✅ close typo auto-route ("consol", "cosole", "vouce") + note
  ✅ normal sentences galti se command na ban jaayein ("setting kya hai", "time kya hai", "notes banao")
  ✅ free text → request ("hy", "hlo", "aaj ka mausam batao")
  ✅ interactive CLI ke natural lines ("console chalao", "telegram chalao", "menu") + hijack-safe
  ✅ end-to-end: `main(['hy'])` asli agent chalata hai (offline demo me bhi jawab aata hai)
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JARVIS_OFFLINE_MODE", "1")
os.environ.setdefault("PROVIDER", "demo")

PASS, FAIL = "✅", "❌"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))


def t_aliases() -> None:
    from jarvis.cli.routing import resolve_command as R

    cases = {
        "console": ("voice", ["console"]),
        "mic": ("voice", ["console"]),
        "talk": ("voice", ["console"]),
        "speak": ("voice", ["console"]),
        "console chalao": ("voice", ["console"]),
        "console kholo": ("voice", ["console"]),
        "console start": ("voice", ["console"]),
        "voice console": ("voice", ["console"]),
        "voice": ("voice", ["console"]),
        "realtime": ("voice", ["console"]),
        "keys": ("voice", ["keys"]),
        "vkeys": ("voice", ["keys"]),
        "voice keys": ("voice", ["keys"]),
        "voice test": ("voice", ["test"]),
        "voice status": ("voice", ["status"]),
        "voice dev": ("voice", ["dev"]),
        "tg": ("telegram", []),
        "bot": ("telegram", []),
        "telegram": ("telegram", []),
        "doctor": ("doctor", []),
        "diag": ("doctor", []),
        "doctor --json": ("doctor", ["--json"]),
        "status": ("status", []),
        "st": ("status", []),
        "menu": ("menu", []),
        "cli": ("cli", []),
        "text": ("cli", []),
        "chat": ("cli", []),
        "chat karo": ("cli", []),
        "text mode": ("cli", []),
        "test": ("test", []),
        "help": ("help", []),
        "-h": ("help", []),
        "version": ("version", []),
        "settings owner_name Nikhil": ("settings", ["owner_name", "Nikhil"]),
        "serve": ("serve", []),
    }
    bad = []
    for argv_str, (kind, args) in cases.items():
        res = R(argv_str.split())
        if (res.kind, res.args) != (kind, args):
            bad.append(f"{argv_str}→({res.kind},{res.args})")
    check(f"routing: {len(cases)} aliases/commands sahi resolve hote hain", not bad, ", ".join(bad[:4]))
    check("routing: aliases me console/mic/keys/tg maujood hain",
          all(k in __import__("jarvis.cli.routing", fromlist=["x"]).ALIASES
              for k in ("console", "mic", "talk", "keys", "tg", "bot", "st")))


def t_typos() -> None:
    from jarvis.cli.routing import resolve_command as R

    typo_cases = {"consol": ("voice", ["console"]), "cosole": ("voice", ["console"]),
                  "vouce": ("voice", ["console"]), "voic": ("voice", ["console"]),
                  "mic chalao": ("voice", ["console"])}
    bad = []
    for tok, expected in typo_cases.items():
        res = R(tok.split())
        if (res.kind, res.args) != expected:
            bad.append(f"{tok}→({res.kind},{res.args})")
    check("routing: close typo auto-route (consol/cosole/vouce)", not bad, ", ".join(bad[:3]))
    res = R(["consol"])
    check("routing: typo par user ko note milta hai", bool(res.note), res.note[:60])


def t_requests() -> None:
    from jarvis.cli.routing import resolve_command as R

    free_text = ["hy", "hlo", "hi bhai", "namaste", "aaj ka mausam batao", "kitne tools hain",
                 "chrome kholo", "notepad kholo aur hello likho", "yaad rakh mera colour black hai",
                 "setting kya hai", "time kya hai", "notes banao", "something weird here",
                 "kitne desh hain duniya me", "meri resume pdf dhoondh ke summary do"]
    bad = [t for t in free_text if not R(t.split()).is_request]
    check(f"routing: {len(free_text)} normal sentences AI request hi rehte hain", not bad,
          "; ".join(bad[:4]))
    check("routing: 'hy' → AI request", R(["hy"]).is_request)
    check("routing: request ke args poori line hote hain",
          R(["aaj", "ka", "mausam", "batao"]).args == ["aaj", "ka", "mausam", "batao"])
    # normal sentence galti se settings na ban jaaye (typo auto-route ka false positive check)
    check("routing: 'setting kya hai' settings command NAHI banta",
          R(["setting", "kya", "hai"]).kind == "request")
    check("routing: 'test kya hota hai' bhi command nahi banta?",
          R(["test", "kya", "hota", "hai"]).kind in ("test", "request"),
          f"kind={R(['test', 'kya', 'hota', 'hai']).kind} (test exact command hai — existing behaviour)")


def t_natural_routes() -> None:
    from jarvis.cli.routing import route_natural_command as N

    positive = {
        "console": ("voice", ["console"]),
        "console chalao": ("voice", ["console"]),
        "voice mode chalu karo": ("voice", ["console"]),
        "mic se baat karni hai": ("voice", ["console"]),
        "awaaz wala mode chalao": ("voice", ["console"]),
        "voice console": ("voice", ["console"]),
        "telegram chalao": ("telegram", []),
        "tg start karo": ("telegram", []),
        "doctor chalao": ("doctor", []),
        "menu": ("menu", []),
    }
    bad = []
    for line, expected in positive.items():
        res = N(line)
        if res is None or (res.kind, res.args) != expected:
            bad.append(f"{line}→{None if res is None else (res.kind, res.args)}")
    check(f"cli: {len(positive)} natural lines command me convert hote hain", not bad, ", ".join(bad[:3]))

    negative = ["aaj ka mausam batao", "chrome kholo", "telegram par message bhej do",
                "email bhej do telegram se", "yaad rakh mera colour black hai", "/help",
                "https://example.com dekho", "meri voice notes dhoondho", ""]
    hijacked = [line for line in negative if N(line) is not None]
    check(f"cli: {len(negative)} normal lines hijack NAHI hoti (agent ke paas jaati hain)",
          not hijacked, "; ".join(hijacked[:3]))
    check("cli: 'telegram par message bhej do' agent ka kaam hi rehta hai", N("telegram par message bhej do") is None)
    check("cli: slash commands natural router se nahi guzarte", N("/voice") is None)


def t_main_e2e() -> None:
    """`python -m jarvis hy` asli agent chalata hai (offline demo mode me)."""
    from jarvis.__main__ import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(["hy"])
    out = buf.getvalue()
    check("e2e: `python -m jarvis hy` exit 0", code == 0, f"code={code}")
    check("e2e: `hy` ka jawab aata hai (agent chala)",
          "answer" in out.lower() or "Samajh" in out or "Nikhil" in out, out.strip().splitlines()[-1][:60] if out.strip() else "(khali)")

    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        code2 = main(["kitne", "tools", "hain"])
    check("e2e: multi-word request bhi chalti hai", code2 == 0)

    buf3 = io.StringIO()
    with contextlib.redirect_stdout(buf3):
        code3 = main(["consol"])          # typo → console
    out3 = buf3.getvalue()
    check("e2e: typo 'consol' console ban jata hai",
          "samjha" in out3 and ("voice" in out3.lower() or "Voice" in out3), out3.strip().splitlines()[0][:70] if out3.strip() else "(khali)")
    check("e2e: console ready na hone par honest guidance + exit != 0",
          code3 != 0 and "voice keys" in out3, f"code={code3}")


def t_help_and_docs() -> None:
    from jarvis.__main__ import USAGE

    check("docs: usage me `python -m jarvis console` alias likha hai", "jarvis console" in USAGE)
    check("docs: usage me 'baaki text = AI request' samjhaya hai",
          "AI ke liye request" in USAGE and "hy" in USAGE)
    src = (ROOT / "README.md").read_text(encoding="utf-8")
    check("docs: README me console alias + free-text rule hai",
          "jarvis console" in src and "python -m jarvis hy" in src)


def main() -> int:
    print("\n" + "=" * 66)
    print("  NIKHILAI — CLI routing suite (`python -m jarvis <kuch bhi>`)")
    print("=" * 66)
    print("\n[1] exact aliases / commands")
    t_aliases()
    print("\n[2] typos")
    t_typos()
    print("\n[3] free text → AI request")
    t_requests()
    print("\n[4] interactive CLI natural lines")
    t_natural_routes()
    print("\n[5] end-to-end (main())")
    t_main_e2e()
    print("\n[6] help/docs")
    t_help_and_docs()

    passed = sum(1 for _n, ok, _d in _results if ok)
    total = len(_results)
    print("\n" + "=" * 66)
    print(f"  RESULT: {passed}/{total} checks passed  ({'ALL GOOD 🚀' if passed == total else 'kuch fail hue ⚠️'})")
    print("=" * 66 + "\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
