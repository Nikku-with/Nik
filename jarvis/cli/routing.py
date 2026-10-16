"""Command routing — "user ne kya likha, usse kya matlab hai" ka ek hi jagah jawab.

Kyun chahiye:
    `python -m jarvis <kuch bhi>` me pehla shabd command bhi ho sakta hai aur AI ke liye
    request bhi. Pehle sirf exact command naam kaam karte the — `python -m jarvis console`
    ko woh "console" naam ki request samajh leta tha (aur AI us par bekaar jawab banata).

Rules (predictable rakhne ke liye):
    1. Pehla shabd kisi known command/alias se match kare → wo command chalti hai.
       (`console`, `mic`, `talk`, `keys`, `tg`, `bot`, `doc` … sab alias hain)
    2. Filler shabd (`chalao`, `kholo`, `start`, `mode`, `kar do` …) command se hata diye jate hain
       → `python -m jarvis console chalao` bhi kaam karta hai.
    3. Bahut close typo (≥85% similar, aur single word) → alias maan liya jata hai + note print hota hai
       → `python -m jarvis consol` = voice console.
    4. Baaki *kuch bhi* = AI ke liye request (yahi default hai — rigid prefixes ki zaroorat nahi)
       → `python -m jarvis hy`, `python -m jarvis aaj ka mausam batao`.
"""
from __future__ import annotations

import difflib
import ipaddress
import re
from dataclasses import dataclass, field

__all__ = ["Resolution", "resolve_command", "route_natural_command", "COMMAND_NAMES", "ALIASES"]


@dataclass
class Resolution:
    kind: str                       # request | voice | telegram | cli | menu | doctor | status | test | settings | serve | help | version
    args: list[str] = field(default_factory=list)
    note: str = ""                  # user ko dikhane wali line (typo/alias samjhane ke liye)
    is_request: bool = False

    def label(self) -> str:
        return "AI request" if self.is_request else f"{self.kind} {' '.join(self.args)}".strip()


# ---------------------------------------------------------------------------
# aliases — "python -m jarvis <alias>"
# ---------------------------------------------------------------------------
ALIASES: dict[str, tuple[str, list[str]]] = {
    # voice (LiveKit console wala mode)
    "voice": ("voice", []),
    "realtime": ("voice", []),
    "console": ("voice", ["console"]),          # python -m jarvis console  ← seedha LiveKit console
    "voice-console": ("voice", ["console"]),
    "voiceconsole": ("voice", ["console"]),
    "mic": ("voice", ["console"]),
    "talk": ("voice", ["console"]),
    "speak": ("voice", ["console"]),
    "awaaz": ("voice", ["console"]),
    "aawaz": ("voice", ["console"]),
    "bolna": ("voice", ["console"]),
    "keys": ("voice", ["keys"]),                # voice keys (exact steps)
    "vkeys": ("voice", ["keys"]),
    "voice-keys": ("voice", ["keys"]),
    "voice-test": ("voice", ["test"]),
    "vtalk": ("voice", ["console"]),
    # telegram
    "telegram": ("telegram", []), "tg": ("telegram", []), "bot": ("telegram", []),
    # cli / text
    "cli": ("cli", []), "text": ("cli", []), "chat": ("cli", []),
    # utility
    "doctor": ("doctor", []), "diag": ("doctor", []), "diagnostics": ("doctor", []),
    "status": ("status", []), "st": ("status", []),
    "test": ("test", []), "tests": ("test", []), "selftest": ("test", []),
    "settings": ("settings", []),
    "serve": ("serve", []), "api": ("serve", []), "server": ("serve", []), "web": ("serve", []),
    "menu": ("menu", []),
    "help": ("help", []), "?": ("help", []),
    "version": ("version", []), "-v": ("version", []), "--version": ("version", []),
    "-h": ("help", []), "--help": ("help", []),
}

COMMAND_NAMES: list[str] = sorted(set(ALIASES))

# command ke saath ye filler shabd aaye to ignore karo: "console chalao", "voice mode start"
FILLER = {
    "chalao", "chala", "chalu", "kholo", "khol", "start", "open", "run", "karo", "kar",
    "banao", "bana", "do", "de", "na", "pls", "please", "mode", "abhi", "zara",
}
# voice ke valid modes (inhe filler ki tarah mat phenko)
VOICE_MODES = {"console", "dev", "start", "connect", "test", "keys", "status"}

TYP0_AUTO = 0.80      # itna similar → alias maan lo
TYP0_HINT = 0.70      # itna similar → hint do, par request hi chalao


def _clean(token: str) -> str:
    return token.strip().strip(".,!?;:\"'").lower()


def _is_typo_of_command(word: str) -> str | None:
    if len(word) < 4:
        return None
    hits = difflib.get_close_matches(word, COMMAND_NAMES, n=1, cutoff=TYP0_AUTO)
    return hits[0] if hits else None


def _apply_alias(word: str) -> tuple[str, list[str]] | None:
    return ALIASES.get(word)


def _valid_serve_args(args: list[str]) -> bool:
    if len(args) > 2:
        return False
    if args:
        host = args[0].strip().strip("[]")
        if host.lower() != "localhost":
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return False
    if len(args) == 2:
        try:
            port = int(args[1])
        except ValueError:
            return False
        if not 1 <= port <= 65535:
            return False
    return True


def resolve_command(argv: list[str]) -> Resolution:
    """argv (jaise `python -m jarvis` ke baad) → Resolution.

    Command na mile to `is_request=True` — poori line AI ke paas jaati hai.
    """
    argv = [a for a in (argv or []) if a is not None]
    if not argv:
        return Resolution("menu", [], "bina args = menu")

    first = _clean(argv[0])
    rest = list(argv[1:])

    # -- known alias / command --
    hit = _apply_alias(first)
    note = ""
    if hit is None:
        # typo auto-route sirf tab jab line command jaisi dikhe — warna "setting kya hai"
        # jaise normal sentence galti se settings command ban jaayenge
        looks_like_command = len(argv) == 1 or all(_clean(t) in FILLER for t in rest)
        typo = _is_typo_of_command(first) if looks_like_command else None
        if typo is not None:
            hit = _apply_alias(typo)
            note = f"'{argv[0]}' ko '{typo}' samjha (typo lag raha tha)"

    if hit is not None:
        kind, base_args = hit
        # filler shabd hatao ("console chalao", "chat karo", "text mode") — jin commands ko
        # asli args chahiye (test/settings/serve) unme kuch nahi chhedte
        if kind in ("voice", "cli", "telegram", "doctor", "status", "menu"):
            cleaned = [t for t in rest if _clean(t) not in FILLER]
        else:
            cleaned = rest
        if kind == "voice":
            # `console --help` / `console --list-devices` → mode bacha ke options aage bhejo,
            # aur agar user ne mode diya hi nahi (sirf options) to console default rakho
            if any(_clean(t) in VOICE_MODES for t in cleaned):
                args = cleaned
            elif cleaned:
                args = [*base_args, *cleaned] if base_args else cleaned
                if not base_args and cleaned[0].startswith("-"):
                    args = ["console", *cleaned]
            else:
                args = base_args or ["console"]
        else:
            args = cleaned if cleaned else base_args
        if kind == "voice" and not args:
            args = ["console"]          # `python -m jarvis voice` / `mic` → console
        if kind == "help" and args not in ([], ["-h"], ["--help"]):
            return Resolution("request", argv, note, is_request=True)
        if kind == "test" and args:
            return Resolution("request", argv, note, is_request=True)
        if kind == "serve" and not _valid_serve_args(args):
            return Resolution("request", argv, note, is_request=True)
        return Resolution(kind, args, note)

    # -- koi alias nahi: request hai --
    hint = ""
    if len(argv) <= 2 and " " not in argv[0]:
        close = difflib.get_close_matches(first, COMMAND_NAMES, n=1, cutoff=TYP0_HINT)
        if close:
            hint = f"'{argv[0]}' command jaisa lagta hai (shayad '{close[0]}'?) — abhi AI request ki tarah chala raha hoon"
    return Resolution("request", argv, hint, is_request=True)


# ---------------------------------------------------------------------------
# interactive CLI ke andar natural line → command (jaise "console chalao")
# ---------------------------------------------------------------------------
_NL_RULES: list[tuple[re.Pattern[str], tuple[str, list[str]], str]] = [
    (re.compile(r"^(console|voice console|voice mode|mic mode|talk mode)$"), ("voice", ["console"]),
     "voice console"),
    (re.compile(r"\b(voice|mic|console|awaaz|aawaz|realtime)\b.*\b"
                r"(chalu|chalao|chala|kholo|start|karo|on|baat|console|mode)\b"), ("voice", ["console"]),
     "voice console"),
    (re.compile(r"\b(voice|mic|console)\b.*\bkeys?\b"), ("voice", ["keys"]), "voice keys"),
    (re.compile(r"^(telegram|tg|bot)( chalao| chalu| kholo| start| mode)?$"), ("telegram", []),
     "telegram bot"),
    (re.compile(r"\b(telegram|tg)\b.*\b(chalu|chalao|chala|kholo|start|bot)\b"), ("telegram", []),
     "telegram bot"),
    (re.compile(r"^(doctor|diag|diagnostics)$"), ("doctor", []), "doctor"),
    (re.compile(r"\b(doctor|diagnostic|health check|checkup)\b.*\b(chalao|chala|karo|run)\b"),
     ("doctor", []), "doctor"),
    (re.compile(r"^(menu|buttons|button menu)$"), ("menu", []), "menu"),
]


def route_natural_command(line: str) -> Resolution | None:
    """Interactive CLI me likhi gayi natural line ko command me badlo (warna None = agent).

    Jaan-bujh kar narrow rakha hai: sirf wahi lines pakadta hai jo clearly *local session*
    start karni hain. Jaise "telegram par message bhej do" hijack nahi hoti — wo agent ka kaam hai.
    """
    text = (line or "").strip().lower()
    text = re.sub(r"[।!?,.]", "", text).strip()
    if not text:
        return None
    # URL/route jaisi cheezein kabhi command nahi
    if text.startswith(("http://", "https://", "/", "\\", "./")):
        return None
    for pattern, (kind, args), label in _NL_RULES:
        if pattern.search(text):
            return Resolution(kind, args, f"'{line.strip()}' → {label}")
    return None
