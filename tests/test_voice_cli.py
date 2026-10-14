"""
tests/test_voice_cli.py — voice console CLI suite (LiveKit ka `console` subcommand)
==================================================================================

Chalao:  python tests/test_voice_cli.py     (pytest bhi chalega)

User ne ye asli bug pakda:
    Menu se option 2 (ya `python -m jarvis voice` bina mode) chalane par LiveKit ne apna
    `Usage: python -m jarvis [OPTIONS] COMMAND ...` help print kar diya — console start hi
    nahi hua. Wajah: hum LiveKit ko `console` subcommand bheje bina start kar rahe the, aur
    LiveKit apna subcommand **sys.argv** se padhta hai.

Checks:
  ✅ menu path: `voice_main([])` → LiveKit ko `["console"]` jaata hai (yahi toota tha)
  ✅ `voice console` / `voice dev` / `voice start` pass-through
  ✅ `voice console --list-devices` jaise LiveKit options bhi pass-through
  ✅ `keys` / `test` / `status` LiveKit start nahi karte
  ✅ galat mode / not-ready → honest error, LiveKit start NAHI hota
  ✅ runner khud `sys.argv` set karta hai (aur baad me restore) — livekit-agents ke asli
    `run_app` ke signature ke against test
  ✅ subprocess: `python -m jarvis voice console --help` → LiveKit ka "Usage: ... console" aata hai
  ✅ subprocess: `python -m jarvis console --help` (alias) bhi wahi karta hai
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JARVIS_OFFLINE_MODE", "1")
os.environ.setdefault("PROVIDER", "demo")

PASS, FAIL = "✅", "❌"
_results: list[tuple[str, bool, str]] = []

FAKE_ENV = {
    "LIVEKIT_URL": "wss://fake.livekit.cloud",
    "LIVEKIT_API_KEY": "APIfake1234567890",
    "LIVEKIT_API_SECRET": "fakesecret1234567890",
    "GEMINI_API_KEY": "AIzaSyFAKEfakefakefakefakefakefake",
}


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))


class _Capture:
    """start_livekit ko replace karke args capture karo (asli LiveKit start nahi hota)."""

    def __init__(self, exit_code: int = 0):
        self.calls: list[list[str]] = []
        self.exit_code = exit_code

    def __call__(self, args: list[str]) -> int:
        self.calls.append(list(args))
        return self.exit_code


def _patch(ready: bool = True):
    import jarvis.voice.livekit_agent as VA

    VA.voice_status = lambda: {"ready": ready, "library": ready, "keys": {"url": ready, "api_key": ready,
                                                                        "secret": ready},
                               "gemini": ready, "model": "gemini-3.8-live", "voice": "Charon",
                               "missing": [] if ready else ["LIVEKIT_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET"]}
    cap = _Capture()
    VA.start_livekit = cap
    return VA, cap


def t_menu_path() -> None:
    """Sabse important: menu option 2 = `voice_main([])`. Yahi toota tha."""
    VA, cap = _patch(ready=True)
    code = VA.main([])
    check("voice: menu option 2 (bina mode) → LiveKit ko ['console'] jaata hai",
          cap.calls == [["console"]] and code == 0, f"calls={cap.calls} code={code}")
    check("voice: khaali args par LiveKit ko KABHI khaali args nahi jaate",
          all(c for c in cap.calls), str(cap.calls))


def t_mode_passthrough() -> None:
    cases = {
        ("console",): ["console"],
        ("console", "--list-devices"): ["console", "--list-devices"],
        ("console", "--text"): ["console", "--text"],
        ("dev",): ["dev"],
        ("start",): ["start"],
        ("connect",): ["connect"],
        ("download-files",): ["download-files"],
    }
    bad = []
    for argv, expected in cases.items():
        VA, cap = _patch(ready=True)
        code = VA.main(list(argv))
        if cap.calls != [expected] or code != 0:
            bad.append(f"{argv}→{cap.calls}")
    check(f"voice: {len(cases)} mode/options LiveKit ko waise hi pass hote hain", not bad,
          ", ".join(bad[:3]))

    VA, cap = _patch(ready=True)
    code = VA.main(["bogus"])
    check("voice: galat mode → exit 2 aur LiveKit start nahi",
          code == 2 and cap.calls == [], f"code={code} calls={cap.calls}")


def t_helpers_dont_start_agent() -> None:
    VA, cap = _patch(ready=True)
    code = VA.main(["keys"])
    check("voice: `keys` LiveKit start nahi karta", cap.calls == [] and code == 0)
    VA, cap = _patch(ready=True)
    code = VA.main(["test", "aaj ki date batao"])
    check("voice: `test` LiveKit start nahi karta (bina keys pipeline test)", cap.calls == [] and code == 0)
    VA, cap = _patch(ready=True)
    code = VA.main(["status"])
    check("voice: `status` LiveKit start nahi karta", cap.calls == [] and code == 0)
    VA, cap = _patch(ready=True)
    code = VA.main(["--help"])
    check("voice: `voice --help` hamari help deta hai (LiveKit start nahi)", cap.calls == [] and code == 0)


def t_not_ready_guard() -> None:
    VA, cap = _patch(ready=False)
    code = VA.main(["console"])
    check("voice: keys nahi hain → exit 1, LiveKit start nahi", code == 1 and cap.calls == [],
          f"code={code}")


def t_runner_sets_argv() -> None:
    """Asli livekit-agents library ke saath: runner sys.argv sahi set karta hai."""
    import importlib.util

    if importlib.util.find_spec("livekit") is None:
        check("voice: runner sys.argv set karta hai (livekit install nahi — skip)", True, "skip")
        return
    from livekit import agents

    captured: dict[str, list[str]] = {}

    def fake_run_app(opts):  # noqa: ANN001
        captured["argv"] = list(sys.argv)
        return 0

    real = agents.cli.run_app
    agents.cli.run_app = fake_run_app
    try:
        import jarvis.voice.livekit_agent as VA
        before = list(sys.argv)
        VA.build_livekit_agent_runner(["console"])
        check("voice: runner LiveKit ko argv=['<prog>','console'] deta hai",
              captured.get("argv", [])[1:] == ["console"], str(captured.get("argv"))[:80])
        VA.build_livekit_agent_runner([])          # khaali → console default
        check("voice: runner khaali args par bhi 'console' bhejta hai (bug fix)",
              captured.get("argv", [])[1:] == ["console"], str(captured.get("argv"))[:80])
        VA.build_livekit_agent_runner(["console", "--list-devices"])
        check("voice: extra options bhi pass hote hain",
              captured.get("argv", [])[1:] == ["console", "--list-devices"])
        check("voice: sys.argv baad me restore ho jaata hai", list(sys.argv) == before)
    finally:
        agents.cli.run_app = real


def _livekit_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("livekit") is not None


def _run_cli(args: list[str], env: dict | None = None):
    base = {**os.environ, **FAKE_ENV, "PROVIDER": "demo", "JARVIS_OFFLINE_MODE": "1"}
    if env:
        base.update(env)
    p = subprocess.run([sys.executable, "-m", "jarvis", *args],
                       cwd=ROOT, env=base, capture_output=True, text=True, timeout=180)
    return p, p.stdout + p.stderr


def t_subprocess_real_cli() -> None:
    """Sabse asli proof: asli command chalao aur dekho LiveKit ka console subcommand mila.

    NOTE: `livekit-agents` install na ho to LiveKit ki help aa hi nahi sakti — tab hum
    (a) confirm karte hain ki routing sahi hua (banner 'voice mode (console)') aur
    (b) honest guidance mili, chup-chaap galat cheez nahi chali. Isse ye test har env me
    meaningful rehta hai (sandbox me livekit baar-baar ud jata hai, user ke system par hota hai).
    """
    lk = _livekit_available()

    p, out = _run_cli(["voice", "console", "--help"])
    routed_ok = ("voice mode (console)" in out) if lk else ("Voice mode abhi ready nahi" in out
                                                            and "voice keys" in out)
    check("cli: `voice console --help` voice path par hi jaata hai (AI request nahi)",
          routed_ok and "answer •" not in out, out.strip().splitlines()[0][:70] if out.strip() else "(khali)")
    if lk:
        check("cli: `voice console --help` → LiveKit ka console usage aata hai",
              "console [OPTIONS]" in out and p.returncode == 0)
        check("cli: LiveKit ke console options dikhte hain (--input-device/--text/--record)",
              "--input-device" in out and "--text" in out)
        check("cli: purana bug wapas nahi aaya (LiveKit ka top-level COMMAND list nahi)",
              "download-files" not in out and "COMMAND [ARGS]" not in out)
    else:
        check("cli: livekit-agents nahi hai → honest guidance (skip LiveKit help check)",
              "voice keys" in out and p.returncode != 0, "skip — livekit-agents install nahi")

    p2, out2 = _run_cli(["console", "--help"])
    routed2_ok = ("voice mode (console)" in out2) if lk else ("Voice mode abhi ready nahi" in out2)
    check("cli: shortcut `python -m jarvis console --help` bhi console mode me jaata hai",
          routed2_ok and "answer •" not in out2,
          out2.strip().splitlines()[0][:70] if out2.strip() else "(khali)")
    if lk:
        check("cli: shortcut se bhi LiveKit ka console usage aata hai",
              "console [OPTIONS]" in out2 and p2.returncode == 0)

    p3, out3 = _run_cli(["voice"], env={"GEMINI_API_KEY": ""})
    check("cli: keys missing par honest guidance (LiveKit help nahi)",
          p3.returncode == 1 and "voice keys" in out3 and "COMMAND [ARGS]" not in out3,
          f"code={p3.returncode}")


def t_docs() -> None:
    src = (ROOT / "docs" / "SETUP_APIS.md").read_text(encoding="utf-8")
    check("docs: SETUP_APIS me ye troubleshooting row hai (menu/bina-mode usage bug)",
          "Usage" in src and "voice console" in src)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check("docs: README me LiveKit options passthrough documented", "--list-devices" in readme)


def main() -> int:
    print("\n" + "=" * 66)
    print("  NIKHILAI — voice console CLI suite (LiveKit subcommand routing)")
    print("=" * 66)
    print("\n[1] menu path (option 2) — yahi toota tha")
    t_menu_path()
    print("\n[2] mode + LiveKit options passthrough")
    t_mode_passthrough()
    print("\n[3] keys/test/status LiveKit start nahi karte")
    t_helpers_dont_start_agent()
    print("\n[4] ready nahi → honest guard")
    t_not_ready_guard()
    print("\n[5] runner aur sys.argv (livekit-agents ke against)")
    t_runner_sets_argv()
    print("\n[6] asli subprocess CLI test")
    t_subprocess_real_cli()
    print("\n[7] docs")
    t_docs()

    passed = sum(1 for _n, ok, _d in _results if ok)
    total = len(_results)
    print("\n" + "=" * 66)
    print(f"  RESULT: {passed}/{total} checks passed  ({'ALL GOOD 🚀' if passed == total else 'kuch fail hue ⚠️'})")
    print("=" * 66 + "\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
