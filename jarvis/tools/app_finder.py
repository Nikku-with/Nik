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

Launch behavior:
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
import tempfile
import threading
import time
from pathlib import Path

from langchain_core.tools import tool

from ..config import APP_MEMORY_FILE

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

_INDEX_CACHE: dict[str, object] = {"ts": 0.0, "apps": []}
INDEX_TTL_S = 600  # 10 min
_LAUNCH_LOCK = threading.RLock()
_RECENT_LAUNCHES: dict[str, float] = {}
LAUNCH_DEDUPE_S = 4.0

# manual aliases: "owner jo naam bolta hai" → index me jo naam hoga
ALIASES: dict[str, list[str]] = {
    "vs code": ["visual studio code", "code"],
    "vscode": ["visual studio code", "code"],
    "browser": ["google chrome", "chromium", "firefox", "microsoft edge", "brave", "vivaldi", "opera", "safari", "tor browser"],
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


def _memory_keys(query: str, app_name: str = "") -> list[str]:
    query_norm = _norm(query)
    app_norm = _norm(app_name)
    keys = [query_norm] if query_norm else []
    for alias, targets in ALIASES.items():
        alias_norm = _norm(alias)
        target_norms = {_norm(target) for target in targets}
        if (query_norm == alias_norm or (app_norm and app_norm in target_norms)) and alias_norm not in keys:
            keys.append(alias_norm)
    if app_norm and app_norm not in keys:
        keys.append(app_norm)
    return keys


class AppLaunchMemory:
    """Atomic, app-launch-only mapping cache; it never stores user task/history text."""

    def __init__(self, path: str | Path = APP_MEMORY_FILE) -> None:
        self.path = Path(path)
        self.backup_path = self.path.with_name(self.path.name + ".bak")
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self.load_error = ""
        self._primary_valid = False
        self.load()

    def _read(self, path: Path) -> dict[str, dict]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("apps", {}), dict):
            raise ValueError("invalid app memory document")
        records: dict[str, dict] = {}
        for key, value in raw.get("apps", {}).items():
            if isinstance(value, dict) and value.get("name") and value.get("path") and value.get("kind"):
                records[_norm(key)] = {"name": str(value["name"]), "path": str(value["path"]),
                                       "kind": str(value["kind"]),
                                       "verified_at": float(value.get("verified_at", 0.0) or 0.0)}
        return records

    def load(self) -> None:
        with self._lock:
            self._records.clear()
            self.load_error = ""
            self._primary_valid = False
            if self.path.exists():
                try:
                    self._records = self._read(self.path)
                    self._primary_valid = True
                    return
                except Exception as exc:
                    self.load_error = f"app memory unreadable ({type(exc).__name__})"
            if self.backup_path.exists():
                try:
                    self._records = self._read(self.backup_path)
                    self.load_error = f"{self.load_error}; restored app mapping from backup"
                except Exception as exc:
                    self.load_error = f"app memory and backup unreadable ({type(exc).__name__})"

    def _save_locked(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self._primary_valid:
                fd, backup_tmp = tempfile.mkstemp(prefix=f".{self.backup_path.name}.", dir=self.path.parent)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(self.path.read_bytes())
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(backup_tmp, self.backup_path)
                finally:
                    try:
                        os.unlink(backup_tmp)
                    except FileNotFoundError:
                        pass
            payload = {"apps": self._records, "_updated_at": time.time()}
            fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
            finally:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
            self._primary_valid = True
            self.load_error = ""
            return True
        except Exception as exc:
            self.load_error = f"app memory save failed ({type(exc).__name__})"
            return False

    def lookup(self, query: str) -> dict | None:
        with self._lock:
            for key in _memory_keys(query):
                record = self._records.get(key)
                if record:
                    return dict(record)
        return None

    def remember(self, query: str, app: dict) -> bool:
        record = {"name": str(app.get("name", "")), "path": str(app.get("path", "")),
                  "kind": str(app.get("kind", "")), "verified_at": time.time()}
        if not all(record[key] for key in ("name", "path", "kind")):
            return False
        with self._lock:
            for key in _memory_keys(query, record["name"]):
                self._records[key] = record
            return self._save_locked()

    def forget_record(self, record: dict) -> bool:
        identity = (str(record.get("name", "")), str(record.get("path", "")), str(record.get("kind", "")))
        with self._lock:
            self._records = {
                key: value for key, value in self._records.items()
                if (value.get("name"), value.get("path"), value.get("kind")) != identity
            }
            return self._save_locked()

    def forget(self, query: str) -> bool:
        with self._lock:
            keys = _memory_keys(query)
            matched = [self._records[key] for key in keys if key in self._records]
            if not matched:
                return False
            identity = {(item.get("name"), item.get("path"), item.get("kind")) for item in matched}
            self._records = {
                key: value for key, value in self._records.items()
                if (value.get("name"), value.get("path"), value.get("kind")) not in identity
            }
            return self._save_locked()

    def clear(self) -> bool:
        with self._lock:
            self._records.clear()
            return self._save_locked()

    def list(self) -> list[dict]:
        with self._lock:
            unique = {(item.get("name"), item.get("path"), item.get("kind")): dict(item)
                      for item in self._records.values()}
            return sorted(unique.values(), key=lambda item: item.get("name", "").casefold())


_APP_MEMORY = AppLaunchMemory()


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
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | Select-Object -Property Name,AppID | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=4,
        )
        if out.returncode == 0 and out.stdout.strip():
            data = json.loads(out.stdout)
            items = data if isinstance(data, list) else [data]
            for item in items:
                _add(apps, str(item.get("Name", "")), f"shell:AppsFolder\\{item.get('AppID', '')}", "store")
    except Exception:
        pass

    # common executables on PATH
    for exe in ("notepad.exe", "calc.exe", "mspaint.exe", "cmd.exe", "wt.exe", "explorer.exe",
                "code.cmd", "chrome.exe", "msedge.exe", "firefox.exe", "vlc.exe", "spotify.exe"):
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
        out = subprocess.run(["mdfind", "kMDItemContentType == 'com.apple.application-bundle'"],
                             capture_output=True, text=True, timeout=20)
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
        key = (str(a.get("norm") or _norm(a.get("name", ""))), str(a.get("kind", "")))
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
        elif q in words:                     # "chrome" == ek poora word of "google chrome"
            best = max(best, 90.0)
        elif len(c) >= 3 and (q in c or c in q):
            best = max(best, 85.0)
        elif len(q) >= 3 and any(w.startswith(q) or q.startswith(w) for w in words):
            best = max(best, 88.0)           # "notep" → "notepad"
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
        best = max(best, alias_score * 0.9)                 # indirect match → thoda discount
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

            return difflib.SequenceMatcher(None, a, b).ratio()   # pehle se 0..1


_BROWSER_MARKERS = (
    "google chrome", "chrome", "chromium", "firefox", "mozilla firefox", "microsoft edge", "msedge",
    "brave", "vivaldi", "opera", "safari", "tor browser", "librewolf", "waterfox", "floorp", "arc browser",
)
_BROWSER_NEGATIVE_MARKERS = ("webview", "runtime", "driver", "updater", "helper", "test", "simulator", "engine")


def _is_browser_app(app: dict) -> bool:
    name = _norm(str(app.get("name", "")))
    path = _norm(str(app.get("path", "")))
    combined = f"{name} {path}"
    if any(marker in name for marker in _BROWSER_NEGATIVE_MARKERS):
        return False
    if any(marker in combined for marker in _BROWSER_MARKERS):
        return True
    return name in {"browser", "web browser", "internet browser"}


def find_apps(query: str, limit: int = 8, min_score: float = 55.0) -> list[dict]:
    """Fuzzy app search — score ke saath list.

    `min_score` exists because a clarifying question still needs candidates when nothing
    clears the normal floor: "kuch nahi mila" gives the owner nothing to answer with.
    """
    apps = build_app_index()
    if not apps:
        return []
    query_norm = _norm(query)
    browser_intent = query_norm in {"browser", "web browser", "internet browser", "default browser"}
    if browser_intent:
        apps = [app for app in apps if _is_browser_app(app)]
        if not apps:
            return []
    scored: list[dict] = []
    for app in apps:
        score = _similarity(query, app)
        if score >= min_score:
            scored.append({**app, "score": round(score, 1)})
    scored.sort(key=lambda a: -a["score"])
    # same naam ke alag-alag kinds me sirf best rakho
    out: list[dict] = []
    seen_names: set[str] = set()
    for a in scored:
        # `norm` is set by `_add`, but an index restored or rebuilt by any other path may
        # lack it; a bare `a["norm"]` turned that into KeyError and surfaced to the owner as
        # "open_any_app: error: 'norm'" with no app launched at all.
        key = str(a.get("norm") or _norm(a.get("name", "")))
        if key in seen_names:
            continue
        seen_names.add(key)
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
            subprocess.Popen(argv, shell=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        else:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
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
                # PowerShell Start-Process — quoting-safe
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
            # Native Win32 focus: pygetwindow is optional and often absent in packaged installs.
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            matches: list[int] = []
            enum_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(wintypes.BOOL,
                                                                          wintypes.HWND, wintypes.LPARAM)

            @enum_type
            def _visit(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, length + 1)
                if kw in title.value.lower():
                    matches.append(int(hwnd))
                return True

            user32.EnumWindows(_visit, 0)
            if not matches:
                return False
            hwnd = matches[0]
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            return bool(user32.SetForegroundWindow(hwnd))
        if IS_MAC:
            safe_name = title_keyword.replace('"', '\\"')
            script = (f'tell application "System Events" to set frontmost of '
                      f'(first process whose name contains "{safe_name}") to true')
            proc = await asyncio.create_subprocess_exec("osascript", "-e", script,
                                                        stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.DEVNULL)
            await proc.communicate()
            return proc.returncode == 0
        if IS_LINUX and shutil.which("wmctrl"):
            proc = await asyncio.create_subprocess_exec("wmctrl", "-a", kw,
                                                        stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.DEVNULL)
            await proc.communicate()
            return proc.returncode == 0
    except Exception:
        return False
    return False


async def window_exists(title_keyword: str) -> bool:
    """Check for an existing matching window separately from whether the OS allowed focus."""
    kw = (title_keyword or "").lower().strip()
    if not kw:
        return False
    try:
        if IS_WINDOWS:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            found = False
            enum_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(wintypes.BOOL,
                                                                          wintypes.HWND, wintypes.LPARAM)

            @enum_type
            def _visit(hwnd, _lparam):
                nonlocal found
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        title = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, title, length + 1)
                        if kw in title.value.lower():
                            found = True
                            return False
                return True

            user32.EnumWindows(_visit, 0)
            return found
        if IS_MAC:
            safe_name = title_keyword.replace('"', '\\"')
            script = (f'tell application "System Events" to exists '
                      f'(first process whose name contains "{safe_name}")')
            proc = await asyncio.create_subprocess_exec("osascript", "-e", script,
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.DEVNULL)
            stdout, _ = await proc.communicate()
            return proc.returncode == 0 and b"true" in stdout.lower()
        if IS_LINUX and shutil.which("wmctrl"):
            proc = await asyncio.create_subprocess_exec("wmctrl", "-l",
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.DEVNULL)
            stdout, _ = await proc.communicate()
            return proc.returncode == 0 and any(kw in line.lower() for line in stdout.decode(errors="ignore").splitlines())
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------
def _app_memory_entry_valid(app: dict) -> bool:
    kind, raw_path = str(app.get("kind", "")), str(app.get("path", ""))
    if kind == "store":
        return IS_WINDOWS and raw_path.startswith("shell:AppsFolder\\")
    if kind == "desktop":
        desktop_id = raw_path.removeprefix("desktop:")
        roots = [Path("/usr/share/applications"), Path("/usr/local/share/applications"),
                 Path.home() / ".local/share/applications", Path("/var/lib/snapd/desktop/applications"),
                 Path("/var/lib/flatpak/exports/share/applications"),
                 Path.home() / ".local/share/flatpak/exports/share/applications"]
        return any((root / f"{desktop_id}.desktop").exists() for root in roots)
    try:
        path = Path(raw_path).expanduser()
        return path.exists() or bool(shutil.which(raw_path))
    except Exception:
        return False


async def _verify_active_window(name: str, tries: int = 6, timeout_s: float = 3.0) -> bool:
    """Poll for the launched window until it appears or the budget runs out.

    A cold-start launch (Outlook, Steam, a Store app) routinely takes 2-3 s to register a
    window. The old loop spent 6 x 0.15 s = 0.9 s and then declared "window verify nahi
    hui", so a perfectly good launch was reported as a failure and typing was skipped.
    The budget is wall-clock based, so a slow machine cannot shorten it.
    """
    poll_s = 0.25
    deadline = time.monotonic() + max(0.5, float(timeout_s))
    # `tries` is a safety cap only; the real budget is the wall clock, so the poll count
    # always covers the full window even if a caller passes the old small default.
    max_attempts = max(int(tries), int(max(0.5, float(timeout_s)) / poll_s) + 2)
    for _ in range(max_attempts):
        if await focus_window(name):
            return True
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))
    return await focus_window(name)


def _release_launch_claim(name: str) -> None:
    with _LAUNCH_LOCK:
        _RECENT_LAUNCHES.pop(_norm(name), None)


def _claim_launch(name: str) -> bool:
    """Cross-call dedupe so parallel/repeated requests never spawn the same app repeatedly."""
    now = time.monotonic()
    key = _norm(name)
    with _LAUNCH_LOCK:
        for old, stamp in list(_RECENT_LAUNCHES.items()):
            if now - stamp > LAUNCH_DEDUPE_S:
                _RECENT_LAUNCHES.pop(old, None)
        if now - _RECENT_LAUNCHES.get(key, 0.0) < LAUNCH_DEDUPE_S:
            return False
        _RECENT_LAUNCHES[key] = now
        return True


@tool
async def find_apps_tool(query: str, limit: int = 8) -> str:
    """Search installed applications by (approximate) name and show the best matches.

    Use jab owner app ka naam perfectly na bole — "photo editor", "wo video wala app",
    "browser kholo", "code editor".
    """
    results = await asyncio.to_thread(find_apps, query, limit)
    if not results:
        return (f"❌ '{query}' naam ka koi app installed nahi mila.\n"
                "Tip: `python -c \"from jarvis.tools.app_finder import build_app_index as b; print(len(b(True)))\"` "
                "se index refresh kar sakte ho.")
    lines = [f"🔍 '{query}' ke closest apps:"]
    for i, a in enumerate(results, 1):
        lines.append(f"{i}. {a['name']}  ({a['kind']}, match {a['score']}%)")
    return "\n".join(lines)


@tool
async def open_any_app(query: str, refresh: bool = False) -> str:
    """Use saved app-launch mapping first, then search, launch once, and save only after focus verification."""
    remembered = await asyncio.to_thread(_APP_MEMORY.lookup, query)
    if remembered:
        if not _app_memory_entry_valid(remembered):
            await asyncio.to_thread(_APP_MEMORY.forget_record, remembered)
        else:
            if await focus_window(remembered["name"]):
                return f"✅ {remembered['name']} pehle se open tha; saved app mapping se wahi window focus ki."
            if await window_exists(remembered["name"]):
                return (f"⚠️ {remembered['name']} ki window maujood hai, par focus confirm nahi hua. "
                        "Duplicate launch nahi kiya.")
            if not _claim_launch(remembered["name"]):
                return (f"⚠️ {remembered['name']} ke liye abhi launch request aayi thi; "
                        "duplicate launch skip kiya.")
            ok, detail = await asyncio.to_thread(launch_app, remembered)
            if ok:
                if await _verify_active_window(remembered["name"]):
                    await asyncio.to_thread(_APP_MEMORY.remember, query, remembered)
                    return f"✅ {remembered['name']} saved app mapping se launch hua aur active window verify hui."
                return (f"⚠️ {remembered['name']} launch request bheji, lekin active window verify nahi hui. "
                        "Memory nahi badli; duplicate launch nahi kiya.")
            await asyncio.to_thread(_APP_MEMORY.forget_record, remembered)
            _release_launch_claim(remembered["name"])
            # Stale paths self-heal: remove the cached entry and continue with a fresh index search.

    results = await asyncio.to_thread(find_apps, query, 5)
    if not results and refresh:
        await asyncio.to_thread(build_app_index, True)
        results = await asyncio.to_thread(find_apps, query, 5)
    if not results:
        return f"❌ '{query}' ka app installed-app index me nahi mila; launch nahi hua."

    best = results[0]
    if match_is_ambiguous(results):
        return _clarify_app(query, results)
    return await _open_any_app_resume(query, best)


AMBIGUOUS_SCORE = 88.0


def match_is_ambiguous(matches: list[dict]) -> bool:
    """True when the app search cannot justify a launch.

    Two cases: the best candidate is below the confidence floor, or two different apps tie
    below a perfect score ("notebook" scores the same against Notepad and Notepad++).
    An exact 100 % name match is never treated as ambiguous.
    """
    if not matches:
        return True
    best = float(matches[0].get("score", 0))
    if best < AMBIGUOUS_SCORE:
        return True
    if best >= 100.0:
        return False
    runner_up = float(matches[1].get("score", 0)) if len(matches) > 1 else 0.0
    if runner_up >= best:
        different = _norm(str(matches[1].get("name", ""))) != _norm(str(matches[0].get("name", "")))
        return different
    return False


def _clarify_app(query: str, results: list[dict]) -> str:
    """One owner-facing clarifying question with the real candidates — never a bare refusal.

    Returning a refusal left the owner with no next step; returning a question gives them a
    one-word answer, and the caller stores the pending choice so the answer becomes an alias.
    """
    options = [a for a in (results or [])[:3] if a.get("name")]
    if not options:
        return (f"⚠️ '{query}' ka koi installed app mila hi nahi; launch nahi kiya. "
                "Exact app naam batao to main wahi khol dunga.")
    lines = [f"🤔 '{query}' ka exact match clear nahi hai — galat app kholne se bachne ke liye "
             "main andaza nahi laga raha. Kaun sa kholun?"]
    for index, app in enumerate(options, 1):
        lines.append(f"  {index}. {app['name']}  ({app.get('kind', '?')}, {app.get('score', 0)}%)")
    lines.append("Bas number ya naam bol do; jo chunoge wahi is naam ke liye yaad rakh lunga "
                 "(dobara nahi poochhunga).")
    return "\n".join(lines)


def save_alias_choice(query: str, app_name: str) -> tuple[bool, str]:
    """Save `query -> app` as a verified-launch alias after the owner picked it by name.

    Ordinary aliases are only written after a *verified* launch. An owner's explicit answer
    to a clarifying question is at least as strong a signal, and the mapping is only written
    when the chosen app really exists in the installed-app index.
    """
    wanted = _norm(app_name)
    if not wanted:
        return False, "app naam khali hai; alias save nahi hua"
    for app in build_app_index():
        if _norm(str(app.get("name", ""))) == wanted:
            saved = _APP_MEMORY.remember(query, app)
            return saved, (f"✅ '{query}' ab {app['name']} ke liye yaad rakha gaya."
                           if saved else f"⚠️ alias save nahi hua ({_APP_MEMORY.load_error})")
    return False, f"⚠️ '{app_name}' installed apps me nahi mila; alias save nahi hua."


async def _open_any_app_resume(query: str, best: dict) -> str:
    """Continue the launch flow once a high-confidence candidate has been chosen."""
    if await focus_window(best["name"]):
        return f"✅ {best['name']} pehle se open tha; wahi window focus ki. Naya instance nahi khola."
    if await window_exists(best["name"]):
        return (f"⚠️ {best['name']} ki window pehle se maujood hai, lekin OS ne focus confirm nahi kiya. "
                "Duplicate launch se bachne ke liye naya instance nahi khola.")

    if not _claim_launch(best["name"]):
        if await focus_window(best["name"]):
            return f"✅ {best['name']} open/focus ho gaya; duplicate launch skip kiya."
        if await window_exists(best["name"]):
            return (f"⚠️ {best['name']} ki window maujood hai; focus confirm nahi hua. "
                    "Duplicate launch skip kiya.")
        return (f"⚠️ {best['name']} ke liye abhi launch request aayi thi; duplicate avoid kiya, "
                "active window verify nahi kar paaya.")

    ok, detail = await asyncio.to_thread(launch_app, best)
    if not ok:
        return f"❌ {best['name']} launch nahi hua: {detail}"
    if not await _verify_active_window(best["name"]):
        return (f"⚠️ {best['name']} ke liye launch request bheji, lekin active window verify nahi hui. "
                "App memory save nahi ki; duplicate launch nahi kiya.")
    saved = await asyncio.to_thread(_APP_MEMORY.remember, query, best)
    suffix = "" if saved else f" App open verify hua, lekin workspace memory save nahi hui ({_APP_MEMORY.load_error})."
    return f"✅ {best['name']} launch hua aur active window verify hui.{suffix}"


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


@tool
async def manage_apps(action: str = "list", query: str = "") -> str:
    """Manage only the verified open-app lookup memory: list, forget one query/app, or clear mappings."""
    action = str(action or "list").strip().lower()
    if action == "list":
        items = await asyncio.to_thread(_APP_MEMORY.list)
        warning = f"\n⚠️ {_APP_MEMORY.load_error}" if _APP_MEMORY.load_error else ""
        if not items:
            return "📱 Koi verified app-launch mapping saved nahi hai." + warning
        return "📱 Verified app mappings:\n" + "\n".join(
            f"- {item['name']} ({item['kind']}) — {item['path']}" for item in items[:80]
        ) + warning
    if action == "forget":
        if not query.strip():
            return "Usage: manage_apps(action='forget', query='chrome')"
        removed = await asyncio.to_thread(_APP_MEMORY.forget, query)
        return f"{'🗑️' if removed else 'ℹ️'} {'Mapping removed' if removed else 'No matching mapping'} for {query}."
    if action == "clear":
        ok = await asyncio.to_thread(_APP_MEMORY.clear)
        return "🧹 App-launch mappings cleared." if ok else f"❌ Memory save failed: {_APP_MEMORY.load_error}"
    return "Usage: action=list|forget|clear"


APP_FINDER_TOOLS = [open_any_app, find_apps_tool, list_apps, refresh_app_index, manage_apps]
