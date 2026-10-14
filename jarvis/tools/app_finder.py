"""
jarvis/tools/app_finder.py — "kisi bhi app ko naam se khol do"
=============================================================

Owner ka ask: *"mai name bolu, ye dhunde ... chars se similar ... aur any app open kr paaye search krke"*

Ye module OS ke installed apps ka **index** banata hai aur fuzzy naam se launch karta hai:

  Windows : Start Menu ke .lnk shortcuts (User + All Users) + `Get-StartApps` (Store apps) +
            `App Paths` registry + known executables on PATH
  macOS   : /Applications, /System/Applications, ~/Applications ke *.app bundles (+ mdfind)
  Linux   : /usr/share/applications, ~/.local/share/applications, snap, flatpak ke *.desktop
            (Name / GenericName / Keywords / Exec parse karke)

Fuzzy match: rapidfuzz → fuzzywuzzy → difflib (jo mile). Matching me alias aur keyword
overlap bhi count hota hai ("photo editor" → GIMP/Photoshop type hits).

Launch (OpenClaw ke open-path se seekha hua):
  * Windows: PowerShell `Start-Process -FilePath '<path>'` (quoting-safe, `start ""` se better)
  * macOS  : `open -a <App>` / `open <file>`
  * Linux  : `gtk-launch <desktop-id>` → `xdg-open` fallback
  * Detached + unref — app gateway/bot band hone par bhi chalti rahe
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

from langchain_core.tools import tool


IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

_INDEX_CACHE: dict[str, object] = {"ts": 0.0, "apps": []}
INDEX_TTL_S = 600  # 10 min

# manual aliases: "owner jo naam bolta hai" → index me jo naam hoga
ALIASES: dict[str, list[str]] = {
    "vs code": ["visual studio code", "code"],
    "vscode": ["visual studio code", "code"],
    "browser": ["google chrome", "firefox", "microsoft edge"],
    "chrome": ["google chrome"],
    "editor": ["notepad", "gedit", "kate", "visual studio code"],
    "notepad": ["notepad", "text editor", "gedit", "kate", "mousepad"],
    "explorer": ["file explorer", "finder", "nautilus", "dolphin", "files"],
    "terminal": ["windows terminal", "terminal", "gnome-terminal", "konsole", "xterm", "cmd"],
    "music": ["spotify", "vlc", "rhythmbox", "amazon music"],
    "video player": ["vlc", "mpv", "windows media player"],
    "photo editor": ["gimp", "photoshop", "paint.net", "krita"],
    "notes": ["notepad", "obsidian", "notion", "joplin"],
    "calculator": ["calculator", "gnome-calculator", "kcalc", "speedcrunch"],
    "pdf": ["adobe acrobat", "foxit", "okular", "evince", "sumatra"],
    "chat": ["whatsapp", "telegram", "discord", "slack"],
    "word": ["microsoft word", "libreoffice writer", "wps writer"],
    "excel": ["microsoft excel", "libreoffice calc", "wps spreadsheet"],
    "powerpoint": ["microsoft powerpoint", "libreoffice impress"],
}


# ---------------------------------------------------------------------------
# indexing
# ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _add(apps: list[dict], name: str, path: str, kind: str, extra: str = "") -> None:
    if not name or not path:
        return
    apps.append({"name": name.strip(), "norm": _norm(name), "path": path.strip(), "kind": kind, "extra": extra.strip()})


def _index_windows(apps: list[dict]) -> None:
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    for root in roots:
        try:
            if not root.exists():
                continue
            for lnk in root.rglob("*.lnk"):
                _add(apps, lnk.stem, str(lnk), "lnk")
            for exe in root.rglob("*.exe"):
                _add(apps, exe.stem, str(exe), "exe")
        except Exception:
            continue

    # App Paths registry (registered apps)
    try:
        import winreg  # type: ignore

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")
                for i in range(winreg.QueryInfoKey(key)[0]):
                    sub = winreg.EnumKey(key, i)
                    try:
                        with winreg.OpenKey(key, sub) as sk:
                            target = winreg.QueryValue(sk, None)
                            _add(apps, Path(sub).stem, str(target), "apppath")
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass

    # Store apps (Get-StartApps) — best-effort, PowerShell se
    try:
        out = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-StartApps | Select-Object -Property Name,AppID | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
        if out.returncode == 0 and out.stdout.strip():
            data = json.loads(out.stdout)
            items = data if isinstance(data, list) else [data]
            for item in items:
                _add(apps, str(item.get("Name", "")), f"shell:AppsFolder\\{item.get('AppID', '')}", "store")
    except Exception:
        pass

    # common executables on PATH
    for exe in (
        "notepad.exe",
        "calc.exe",
        "mspaint.exe",
        "cmd.exe",
        "wt.exe",
        "explorer.exe",
        "code.cmd",
        "chrome.exe",
        "msedge.exe",
        "firefox.exe",
        "vlc.exe",
        "spotify.exe",
    ):
        found = shutil.which(exe)
        if found:
            _add(apps, Path(exe).stem, found, "path")


def _index_macos(apps: list[dict]) -> None:
    for root in ("/Applications", "/System/Applications", "/Applications/Utilities", str(Path.home() / "Applications")):
        base = Path(root)
        try:
            if not base.exists():
                continue
            for app in base.glob("*.app"):
                _add(apps, app.stem, str(app), "app")
            # ek level neeche (e.g. Adobe folder)
            for sub in base.glob("*/*.app"):
                _add(apps, sub.stem, str(sub), "app")
        except Exception:
            continue
    try:
        out = subprocess.run(
            ["mdfind", "kMDItemContentType == 'com.apple.application-bundle'"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        for line in (out.stdout or "").splitlines():
            p = Path(line)
            if p.suffix == ".app":
                _add(apps, p.stem, str(p), "app")
    except Exception:
        pass


def _index_linux(apps: list[dict]) -> None:
    dirs = [
        Path("/usr/share/applications"),
        Path("/usr/local/share/applications"),
        Path.home() / ".local/share/applications",
        Path("/var/lib/snapd/desktop/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
        Path.home() / ".local/share/flatpak/exports/share/applications",
    ]
    for d in dirs:
        try:
            if not d.exists():
                continue
            for desktop in d.glob("*.desktop"):
                name = generic = exec_line = ""
                keywords = ""
                for line in desktop.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("Name=") and not name:
                        name = line[5:]
                    elif line.startswith("GenericName=") and not generic:
                        generic = line[12:]
                    elif line.startswith("Exec=") and not exec_line:
                        exec_line = line[5:]
                    elif line.startswith("Keywords=") and not keywords:
                        keywords = line[9:]
                    elif line.startswith("NoDisplay=true"):
                        name = ""
                        break
                if not name:
                    continue
                _add(apps, name, f"desktop:{desktop.stem}", "desktop", extra=f"{generic} {keywords}".strip())
        except Exception:
            continue


def build_app_index(force: bool = False) -> list[dict]:
    now = time.time()
    if not force and _INDEX_CACHE["apps"] and now - float(_INDEX_CACHE["ts"]) < INDEX_TTL_S:
        return list(_INDEX_CACHE["apps"])  # type: ignore[arg-type]

    apps: list[dict] = []
    try:
        if IS_WINDOWS:
            _index_windows(apps)
        elif IS_MAC:
            _index_macos(apps)
        else:
            _index_linux(apps)
    except Exception:
        pass

    # dedupe by (norm, kind)
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for a in apps:
        key = (a["norm"], a["kind"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(a)

    _INDEX_CACHE.update({"ts": now, "apps": unique})
    return unique


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------
def _score_against(q: str, candidates) -> float:
    """Query ko candidate names se compare karo (0..100)."""
    best = 0.0
    for c in candidates:
        if not c:
            continue
        if c == q:
            return 100.0
        words = c.split()
        # 1-2 letter candidates ("r", "x") par startswith/contains bekaar hai —
        # pehle "run chrome".startswith("r") = 92% jaisa fake score aata tha.
        if len(c) >= 3 and (c.startswith(q) or q.startswith(c)):
            best = max(best, 92.0)
        elif q in words:  # "chrome" == ek poora word of "google chrome"
            best = max(best, 90.0)
        elif len(c) >= 3 and (q in c or c in q):
            best = max(best, 85.0)
        elif len(q) >= 3 and any(w.startswith(q) or q.startswith(w) for w in words):
            best = max(best, 88.0)  # "notep" → "notepad"
        best = max(best, _ratio(q, c) * 85)
    return best


def _similarity(query: str, app: dict) -> float:
    """Query vs ek app ka score.

    BUG FIX (bada): pehle alias ke targets seedha *candidate pool* me daal diye jaate the.
    Matlab "notepad" query par candidate me 'notepad' bhi hota tha aur wo HAR app ke liye
    match kar jata — isliye ImageMagick/R/Zutty sab 100% dikhate the aur galat app khulta tha.
    Ab alias sirf tab use hota hai jab direct naam kamzor ho, aur score *alias discount*
    ke saath aata hai (indirect match hai isliye).
    """
    q = _norm(query)
    if not q:
        return 0.0

    own = (app.get("norm", ""), _norm(app.get("extra", "")), _norm(Path(app.get("path", "")).stem))
    best = _score_against(q, own)
    if best >= 70.0:
        return min(best, 100.0)

    # alias: sirf poore word/phrase par (substring se fake hit na ho).
    # Sahi semantics: alias *target* ko APP ke naam se match karo — query se nahi.
    # (Pehle query ko target se match karne se har app 85%+ dikhata tha.)
    q_words = set(q.split())
    alias_score = 0.0
    for alias_q, targets in ALIASES.items():
        if not (q == alias_q or alias_q in q_words or alias_q in q):
            continue
        for target in targets:
            alias_score = max(alias_score, _score_against(_norm(target), own))
    if alias_score:
        best = max(best, alias_score * 0.9)  # indirect match → thoda discount
    return min(best, 100.0)


def _ratio(a: str, b: str) -> float:
    """0..1 similarity (scales consistent rehni chahiye warna score 2000% aata hai)."""
    try:
        from rapidfuzz import fuzz  # type: ignore

        return float(fuzz.token_set_ratio(a, b)) / 100.0
    except Exception:
        try:
            from fuzzywuzzy import fuzz  # type: ignore

            return float(fuzz.token_set_ratio(a, b)) / 100.0
        except Exception:
            import difflib

            return difflib.SequenceMatcher(None, a, b).ratio()  # pehle se 0..1


def find_apps(query: str, limit: int = 8) -> list[dict]:
    """Fuzzy app search — score ke saath list."""
    apps = build_app_index()
    if not apps:
        return []
    scored: list[dict] = []
    for app in apps:
        score = _similarity(query, app)
        if score >= 55:
            scored.append({**app, "score": round(score, 1)})
    scored.sort(key=lambda a: -a["score"])
    # same naam ke alag-alag kinds me sirf best rakho
    out: list[dict] = []
    seen_names: set[str] = set()
    for a in scored:
        if a["norm"] in seen_names:
            continue
        seen_names.add(a["norm"])
        out.append(a)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# launching
# ---------------------------------------------------------------------------
def _spawn_detached(argv: list[str], via_shell: bool = False) -> tuple[bool, str]:
    """App ko detached launch karo — gateway band hone par bhi app chalti rahe."""
    try:
        if via_shell:
            proc = subprocess.Popen(
                argv, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
            )
        else:
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            proc.unref  # type: ignore[attr-defined]
        except Exception:
            pass
        return True, "ok"
    except FileNotFoundError as exc:
        return False, f"launcher nahi mila: {exc}"
    except Exception as exc:
        return False, str(exc)[:200]


def launch_app(app: dict) -> tuple[bool, str]:
    kind, path = app.get("kind", ""), app.get("path", "")
    try:
        if kind == "store":  # shell:AppsFolder\...
            return _spawn_detached(["cmd", "/c", "start", "", path])
        if kind in ("lnk", "exe", "apppath", "path"):
            if IS_WINDOWS and kind in ("lnk", "exe", "apppath"):
                # PowerShell Start-Process — quoting-safe (OpenClaw pattern)
                ps = f"Start-Process -FilePath '{path.replace(chr(39), chr(39) * 2)}'"
                return _spawn_detached(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps])
            return _spawn_detached([path])
        if kind == "app":  # macOS bundle
            return _spawn_detached(["open", "-a", path])
        if kind == "desktop":  # linux
            ident = path.replace("desktop:", "")
            if shutil.which("gtk-launch"):
                ok, msg = _spawn_detached(["gtk-launch", ident])
                if ok:
                    return True, ident
            if shutil.which("gtk4-launch"):
                _spawn_detached(["gtk4-launch", ident])
                return True, ident
            return _spawn_detached(["xdg-open", f"/usr/share/applications/{ident}.desktop"])
        # fallback: direct binary
        if shutil.which(path):
            return _spawn_detached([path])
        return False, f"unsupported kind: {kind}"
    except Exception as exc:
        return False, str(exc)[:200]


async def focus_window(title_keyword: str) -> bool:
    kw = (title_keyword or "").lower().strip()
    if not kw:
        return False
    try:
        if IS_WINDOWS:
            try:
                import pygetwindow as gw  # type: ignore

                for w in gw.getAllWindows():
                    if kw in (w.title or "").lower():
                        if getattr(w, "isMinimized", False):
                            w.restore()
                        w.activate()
                        return True
            except Exception:
                return False
        elif IS_MAC:
            script = f'tell application "System Events" to set frontmost of (first process whose name contains "{title_keyword}") to true'
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc.communicate()
            return proc.returncode == 0
        elif IS_LINUX and shutil.which("wmctrl"):
            proc = await asyncio.create_subprocess_exec(
                "wmctrl", "-a", kw, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc.communicate()
            return proc.returncode == 0
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------
@tool
async def find_apps_tool(query: str, limit: int = 8) -> str:
    """Search installed applications by (approximate) name and show the best matches.

    Use jab owner app ka naam perfectly na bole — "photo editor", "wo video wala app",
    "browser kholo", "code editor".
    """
    results = await asyncio.to_thread(find_apps, query, limit)
    if not results:
        return (
            f"❌ '{query}' naam ka koi app installed nahi mila.\n"
            'Tip: `python -c "from jarvis.tools.app_finder import build_app_index as b; print(len(b(True)))"` '
            "se index refresh kar sakte ho."
        )
    lines = [f"🔍 '{query}' ke closest apps:"]
    for i, a in enumerate(results, 1):
        lines.append(f"{i}. {a['name']}  ({a['kind']}, match {a['score']}%)")
    return "\n".join(lines)


@tool
async def open_any_app(query: str, refresh: bool = False) -> str:
    """Open ANY installed app by approximate name — search karke khud dhoondta hai aur launch karta hai.

    Examples: "notepad kholo", "photo editor app kholo", "wo video player chala do",
              "vs code open karo", "settings kholo".
    Agar naam galat/adhoora ho to bhi closest match launch hota hai (aur batata hai kaun sa).
    """
    results = await asyncio.to_thread(find_apps, query, 5)
    if not results and refresh:
        await asyncio.to_thread(build_app_index, True)
        results = await asyncio.to_thread(find_apps, query, 5)
    if not results:
        hint = ""
        try:
            from ..memory.session_state import get_state

            hint = get_state().recent_apps_hint()
        except Exception:
            pass
        msg = (
            f"❌ '{query}' ka app nahi mila. Maine poora index check kiya.\n"
            "• Exact naam bolo (jaisa Start Menu / Applications me hai)\n"
            "• Ya `run_shell_command` se direct command chalao"
        )
        return msg + (f"\n💡 {hint}" if hint else "")

    best = results[0]
    # Weak match par GALAT app kholna sabse bura outcome hai (user bola "chrome", hum ne
    # ImageMagick khol diya). 60% se upar bhi "shayad" hota hai, isliye 88% se neeche
    # par launch nahi karte — pehle confirm karwate hain.
    if best["score"] < 88:
        alts = "\n".join(f"  • {a['name']} ({a['score']}%)" for a in results[:5])
        return (
            f"🤔 '{query}' ka exact app nahi mila (best guess: {best['name']} — {best['score']}% match).\n"
            f"Galat app kholna theek nahi, isliye maine launch NAHI kiya.\n"
            f"Candidates:\n{alts}\n"
            f'• Confirm karo to bolo: "{best["name"]} kholo"\n'
            f"• Ya `find_apps_tool` se poora list dekho"
        )
    ok, detail = await asyncio.to_thread(launch_app, best)
    if not ok:
        return f"❌ {best['name']} launch nahi hua: {detail}"

    from .system_tools import record_app_launch

    record_app_launch(best["name"])  # ownership (Gate) + launcher memory
    await asyncio.sleep(1.5)
    focused = await focus_window(best["name"])
    others = ", ".join(a["name"] for a in results[1:4])
    extra = f"\n(candidates: {others})" if others else ""
    return (
        f"🚀 {best['name']} launch ho gaya (match {best['score']}%)"
        + (" aur focus me hai." if focused else ".")
        + extra
    )


@tool
async def list_apps(query: str = "", limit: int = 60) -> str:
    """List installed apps (optionally filtered by query). Index debug ke liye."""
    if query:
        results = await asyncio.to_thread(find_apps, query, limit)
        return "\n".join(f"- {a['name']} ({a['kind']}, {a['score']}%)" for a in results) or "koi match nahi"
    apps = await asyncio.to_thread(build_app_index)
    if not apps:
        return "⚠️ App index khali hai (headless environment ho sakta hai)."
    names = sorted({a["name"] for a in apps})
    return f"📦 {len(names)} apps indexed:\n" + "\n".join(f"- {n}" for n in names[:limit])


@tool
async def refresh_app_index() -> str:
    """Rebuild the installed-apps index (naya app install karne ke baad chalao)."""
    apps = await asyncio.to_thread(build_app_index, True)
    return f"🔄 App index refresh ho gaya: {len(apps)} entries."


APP_FINDER_TOOLS = [open_any_app, find_apps_tool, list_apps, refresh_app_index]
