"""
jarvis/tools/system_tools.py — Desktop / OS control (original Jarvis_window_CTRL upgraded)
=========================================================================================

Original file sirf Windows tha (win32gui, `start "" ...`, hardcoded C:\\Users\\gaura paths).
Yahan:
  * cross-platform app registry (Windows / macOS / Linux)
  * safe subprocess launch (shell injection se bacha gaya — list args)
  * volume / hotkey / typing / screenshot — pyautogui optional, graceful degrade
  * `run_shell_command` guardrails ke saath (dangerous pattern block)
"""
from __future__ import annotations

import asyncio
import os
import time
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from langchain_core.tools import tool

from ..config import (
    EXEC_APPROVAL,
    EXEC_APPROVAL_TOKEN,
    LOGS_DIR,
    SAFETY_ALLOW_SHELL,
    SCREENSHOTS_DIR,
    WORKSPACE_DIR,
)

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

# ---------------------------------------------------------------------------
# App registry — naya app add karna ho to yahan ek line daalo
# ---------------------------------------------------------------------------
APP_REGISTRY: dict[str, dict[str, str]] = {
    "notepad": {"windows": "notepad.exe", "macos": "TextEdit", "linux": "gedit"},
    "chrome": {"windows": "chrome.exe", "macos": "Google Chrome", "linux": "google-chrome"},
    "firefox": {"windows": "firefox.exe", "macos": "Firefox", "linux": "firefox"},
    "edge": {"windows": "msedge.exe", "macos": "Microsoft Edge", "linux": "microsoft-edge"},
    "calculator": {"windows": "calc.exe", "macos": "Calculator", "linux": "gnome-calculator"},
    "vs code": {"windows": "code.cmd", "macos": "Visual Studio Code", "linux": "code"},
    "vscode": {"windows": "code.cmd", "macos": "Visual Studio Code", "linux": "code"},
    "terminal": {"windows": "wt.exe", "macos": "Terminal", "linux": "gnome-terminal"},
    "cmd": {"windows": "cmd.exe", "macos": "Terminal", "linux": "xterm"},
    "explorer": {"windows": "explorer.exe", "macos": "Finder", "linux": "nautilus"},
    "vlc": {"windows": "vlc.exe", "macos": "VLC", "linux": "vlc"},
    "paint": {"windows": "mspaint.exe", "macos": "-", "linux": "-"},
    "spotify": {"windows": "spotify.exe", "macos": "Spotify", "linux": "spotify"},
    "settings": {"windows": "ms-settings:", "macos": "-", "linux": "-"},
    "task manager": {"windows": "taskmgr.exe", "macos": "Activity Monitor", "linux": "gnome-system-monitor"},
}

_DANGEROUS = (
    "rm -rf /", "rm -rf /*", "mkfs", "dd if=", ":(){", "format c:", "del /f /s /q c:\\",
    "shutdown", "reboot", "> /dev/sda", "chmod -R 777 /", "diskpart",
)

# risky-but-allowed commands: approval mode "risky" me inpar owner ka token chahiye
_RISKY_PATTERNS = (
    "rm ", "rmdir", "del ", "erase ", "kill ", "pkill", "taskkill", "sudo", "runas",
    "pip install", "npm install", "pnpm install", "yarn add", "apt install", "apt-get install",
    "brew install", "winget install", "choco install", "git push", "git reset --hard",
    "git clean", "drop table", "truncate", "shutil.rmtree", ">", "mkfs", "chmod", "chown",
    "systemctl", "sc delete", "netsh", "reg delete", "schtasks /delete", "curl -X POST", "wget",
)


# approval kitni der valid rahe (ek baar owner ne "haan" bola to 5 min tak)
APPROVAL_TTL_S = float(os.environ.get("NIKHILAI_APPROVAL_TTL_S", "300"))


def _approval_fresh(granted: dict) -> bool:  # type: ignore[type-arg]
    """Approval token fresh hai?

    PEHLE ka bug: `grant_exec_approval` ek baar chalta tha aur `jarvis_memory.json` me
    hamesha ke liye `{token: True}` pada rehta tha — matlab ek hi "haan" ke baad risky
    commands (rm/kill/pip install) permanently bina confirmation chalne lagti thin.
    Ab: time-stamped + ek hi baar ka (use hote hi consume).
    """
    entry = (granted or {}).get(EXEC_APPROVAL_TOKEN)
    if not entry:
        return False
    if entry is True:  # purana format — bharosa mat karo, dubara "haan" mangwao
        return False
    try:
        at = float(entry.get("at") or 0)
    except Exception:
        return False
    return (time.time() - at) <= APPROVAL_TTL_S


def _consume_approval() -> None:
    """Token use hote hi hata do — ek approval = ek command."""
    try:
        from ..memory.session_state import get_state

        state = get_state()
        granted = state.get("exec_approval_granted", {}) or {}
        granted.pop(EXEC_APPROVAL_TOKEN, None)
        state.set("exec_approval_granted", granted)
    except Exception:
        pass


def _is_risky(command: str) -> str | None:
    low = (command or "").lower()
    for pat in _RISKY_PATTERNS:
        if pat in low:
            return pat
    return None


def _expand_secrets(text: str) -> str:
    """
    ${SECRET_name} / $JARVIS_SECRET_name placeholders ko asli value se badlo —
    is tarah agent command likhta hai par asli secret uske context me nahi aata (OpenClaw pattern).
    """
    import os as _os
    import re as _re

    def _sub(match) -> str:
        key = (match.group(1) or match.group(2) or "").strip()
        bare = key[len("SECRET_"):] if key.upper().startswith("SECRET_") else (
            key[len("JARVIS_SECRET_"):] if key.upper().startswith("JARVIS_SECRET_") else key)
        candidates = [
            key, key.upper(),
            f"JARVIS_{key}", f"JARVIS_{key.upper()}",
            f"JARVIS_SECRET_{bare}", f"JARVIS_SECRET_{bare.upper()}", bare, bare.upper(),
        ]
        for cand in candidates:
            if cand and cand in _os.environ:
                return _os.environ[cand]
        return match.group(0)

    text = _re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, text or "")
    text = _re.sub(r"\$(JARVIS_SECRET_[A-Za-z0-9_]+)", _sub, text)
    return text


def _redact(text: str) -> str:
    """Output me secrets wapas redact karo (leak na ho)."""
    import os as _os
    import re as _re

    for key, value in _os.environ.items():
        if key.startswith("JARVIS_SECRET_") and value and len(value) > 3:
            text = text.replace(value, f"<{key}>")
    return text


def _resolve_app(app_title: str) -> str | None:
    key = (app_title or "").strip().lower()
    entry = APP_REGISTRY.get(key)
    if entry:
        plat = "windows" if IS_WINDOWS else "macos" if IS_MAC else "linux"
        return entry.get(plat)
    # registry miss → direct command (PATH par ho to chalega)
    return key if shutil.which(key) else None


async def _launch(target: str) -> tuple[bool, str]:
    try:
        if IS_MAC:
            proc = await asyncio.create_subprocess_exec("open", "-a", target, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        elif IS_WINDOWS:
            if target.endswith(":"):  # ms-settings:
                await asyncio.create_subprocess_exec("cmd", "/c", "start", "", target)
                return True, target
            proc = await asyncio.create_subprocess_exec("cmd", "/c", "start", "", target, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        else:
            if shutil.which("gtk-launch"):
                proc = await asyncio.create_subprocess_exec(target, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            else:
                proc = await asyncio.create_subprocess_exec(target, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=8)
        except asyncio.TimeoutError:
            return True, target  # GUI app chalti rehti hai — timeout = success
        if proc.returncode not in (0, None):
            return False, (err or b"").decode(errors="ignore")[:200]
        return True, target
    except FileNotFoundError:
        return False, f"'{target}' machine par installed nahi mila"
    except Exception as exc:  # pragma: no cover
        return False, str(exc)[:200]


async def focus_window(title_keyword: str) -> bool:
    """Window ko foreground laao (pygetwindow / osascript / wmctrl)."""
    kw = (title_keyword or "").lower().strip()
    if not kw:
        return False
    try:
        if IS_WINDOWS:
            import pygetwindow as gw  # type: ignore
            for w in gw.getAllWindows():
                if kw in (w.title or "").lower():
                    if getattr(w, "isMinimized", False):
                        w.restore()
                    w.activate()
                    return True
        elif IS_MAC:
            script = f'tell application "System Events" to set frontmost of (first process whose name contains "{title_keyword}") to true'
            proc = await asyncio.create_subprocess_exec("osascript", "-e", script)
            await proc.communicate()
            return proc.returncode == 0
        elif IS_LINUX and shutil.which("wmctrl"):
            proc = await asyncio.create_subprocess_exec("wmctrl", "-a", kw)
            await proc.communicate()
            return proc.returncode == 0
    except Exception:
        return False
    return False



# ---------------------------------------------------------------------------
# NATIVE INPUT LAYER — bina kisi pip install ke (pyautogui optional hai)
# ---------------------------------------------------------------------------
# Kyun: pehle type_text/press_hotkey sirf pyautogui par chalte the, jo default
# dependencies me hi nahi hai. Isliye Windows par typing "❌ pyautogui install karo"
# return karta tha — aur LLM phir bhi "likh diya" bol deta tha. Ab har OS ka
# apna native rasta hai (PowerShell SendKeys / osascript / xdotool).
_PS_SENDBAR = ('Add-Type -AssemblyName System.Windows.Forms; '
               'Add-Type -AssemblyName Microsoft.VisualBasic; '
               'Start-Sleep -Milliseconds 350; '
               '[System.Windows.Forms.SendKeys]::SendWait("{combo}")')


def _escape_sendkeys(text: str) -> str:
    """SendKeys ke special characters ko escape karo ({} + ^ % ~ ( ) [ ])."""
    out = []
    for ch in text or "":
        if ch in "{}[]()^%~+":
            out.append("{" + ch + "}")
        elif ch == "\n":
            out.append("{ENTER}")
        elif ch == "\t":
            out.append("{TAB}")
        else:
            out.append(ch)
    return "".join(out)


def _key_name(part: str) -> str:
    """'ctrl' → '^', 'alt' → '%', 'shift' → '+', 'win' → '^%'? (SendKeys style)."""
    p = part.strip().lower()
    return {"ctrl": "^", "control": "^", "alt": "%", "shift": "+", "win": "^",
            "cmd": "^", "command": "^", "meta": "^", "super": "^"}.get(p, p)


async def _run_quiet(*args: str, stdin_data: bytes | None = None) -> tuple[int, str]:
    """Chhota subprocess runner (stdout/stderr capture, crash-safe)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate(input=stdin_data)
        return proc.returncode or 0, (out or b"").decode(errors="ignore").strip()
    except FileNotFoundError:
        return 127, f"{args[0]} not found"
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


async def _windows_sendkeys(combo: str) -> tuple[bool, str]:
    """Windows native: PowerShell SendKeys (koi install nahi chahiye)."""
    if not shutil.which("powershell"):
        return False, "powershell nahi mila"
    cmd = _PS_SENDBAR.format(combo=combo.replace('"', '`"'))
    code, out = await _run_quiet("powershell", "-NoProfile", "-NonInteractive", "-Command", cmd)
    if code != 0:
        return False, out[:200] or "powershell SendKeys fail"
    return True, "SendKeys"


async def _windows_paste_text(text: str) -> tuple[bool, str]:
    """Windows native: clipboard me daalo (clip.exe) aur Ctrl+V bhejo.

    SendKeys ASCII-only hota hai; unicode/Hindi text ke liye yahi rasta sahi hai.
    """
    if not shutil.which("clip"):
        return False, "clip.exe nahi mila"
    code, out = await _run_quiet("cmd", "/c", "clip", stdin_data=text.encode("utf-16le"))
    if code != 0:
        return False, out[:160] or "clip fail"
    ok, detail = await _windows_sendkeys("^v")
    return (True, "clipboard + Ctrl+V") if ok else (False, detail)


async def _native_type(text: str, press_enter: bool) -> tuple[bool, str]:
    """Har OS ka native typing rasta. Returns (ok, detail)."""
    if IS_WINDOWS:
        if any(ord(ch) > 126 for ch in text):          # Hindi/emoji → clipboard route
            ok, detail = await _windows_paste_text(text)
            if ok and press_enter:
                await _windows_sendkeys("{ENTER}")
            return ok, detail
        ok, detail = await _windows_sendkeys(_escape_sendkeys(text))
        if ok and press_enter:
            await _windows_sendkeys("{ENTER}")
        return ok, detail

    if IS_MAC:
        esc = text.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "System Events" to keystroke "{esc}"'
        if press_enter:
            script += '\ntell application "System Events" to key code 36'
        code, out = await _run_quiet("osascript", "-e", script)
        return (code == 0, out[:200] or "osascript keystroke")

    if shutil.which("xdotool"):
        code, out = await _run_quiet("xdotool", "type", "--clearmodifiers", "--delay", "12", text)
        if code == 0 and press_enter:
            await _run_quiet("xdotool", "key", "Return")
        return (code == 0, out[:200] or "xdotool type")
    if shutil.which("wtype"):
        code, out = await _run_quiet("wtype", text)
        if code == 0 and press_enter:
            await _run_quiet("wtype", "-k", "Return")
        return (code == 0, out[:200] or "wtype")

    if any(ord(ch) > 126 for ch in text) and shutil.which("xclip"):
        code, _ = await _run_quiet("xclip", "-selection", "clipboard", stdin_data=text.encode())
        return (code == 0, "xclip + paste")
    return False, "Linux par xdotool ya wtype chahiye (sudo apt install xdotool)"


async def _native_hotkey(combo: list[str]) -> tuple[bool, str]:
    """Cross-platform hotkey, bina install."""
    if IS_WINDOWS:
        parts = [_key_name(p) for p in combo]
        return await _windows_sendkeys("".join(parts[:-1]) + "{" + parts[-1].upper() + "}"
                                       if len(parts[-1]) > 1 else "".join(parts))
    if IS_MAC:
        mods = {"ctrl": "control down", "control": "control down", "alt": "option down",
                "option": "option down", "shift": "shift down", "cmd": "command down",
                "command": "command down", "win": "command down", "super": "command down"}
        key = combo[-1].lower()
        mods_used = [mods[p.lower()] for p in combo[:-1] if p.lower() in mods]
        using = (" using {" + ", ".join(mods_used) + "}") if mods_used else ""
        script = f'tell application "System Events" to keystroke "{key}"{using}'
        code, out = await _run_quiet("osascript", "-e", script)
        return (code == 0, out[:200] or "osascript hotkey")
    if shutil.which("xdotool"):
        code, out = await _run_quiet("xdotool", "key", "--clearmodifiers", "+".join(combo))
        return (code == 0, out[:200] or "xdotool key")
    return False, "Linux par xdotool chahiye (sudo apt install xdotool)"


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------
@tool
async def open_app(app_title: str) -> str:
    """Open a desktop application by name (notepad, chrome, vscode, calculator, vlc, terminal...).

    Use jab user bole: "notepad kholo", "chrome open karo", "vs code chala do".
    """
    target = _resolve_app(app_title)
    if not target:
        return f"❌ '{app_title}' ko main launch nahi kar paya — app registry ya PATH me nahi hai. APP_REGISTRY me add kar sakte hain."
    ok, detail = await _launch(target)
    if not ok:
        return f"❌ {app_title} launch fail: {detail}"
    await asyncio.sleep(1.2)
    focused = await focus_window(app_title if IS_WINDOWS else Path(target).stem)
    return f"🚀 {app_title} launch ho gaya" + (" aur focus me hai." if focused else ".")


@tool
async def close_app(window_title: str) -> str:
    """Close a running application / window by title keyword ("notepad band karo", "close vlc")."""
    kw = (window_title or "").strip().lower()
    if not kw:
        return "❌ Khali title."
    try:
        if IS_WINDOWS:
            try:
                import win32con  # type: ignore
                import win32gui  # type: ignore
            except ImportError:
                return "❌ Windows par 'pip install pywin32' chahiye close_app ke liye."
            killed = {"n": 0}

            def _handler(hwnd, _):
                if win32gui.IsWindowVisible(hwnd) and kw in win32gui.GetWindowText(hwnd).lower():
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                    killed["n"] += 1

            win32gui.EnumWindows(_handler, None)
            return f"✅ {killed['n']} window(s) close ki gayi: {window_title}"
        if IS_MAC:
            proc = await asyncio.create_subprocess_exec("osascript", "-e", f'tell application "{window_title}" to quit')
            await proc.communicate()
            return f"✅ {window_title} quit signal bhej diya."
        proc = await asyncio.create_subprocess_exec("pkill", "-f", window_title, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        ok = proc.returncode == 0
        return f"{'✅' if ok else '⚠️'} {window_title} {'band ho gaya' if ok else 'match nahi hua'}."
    except Exception as exc:
        return f"❌ close_app error: {exc}"


@tool
async def list_windows() -> str:
    """List all open windows (debug ke liye: kis naam se window milegi)."""
    try:
        if IS_WINDOWS:
            import pygetwindow as gw  # type: ignore
            titles = [w.title for w in gw.getAllWindows() if w.title.strip()]
            return "Open windows:\n" + "\n".join(f"- {t}" for t in titles[:40])
        if IS_LINUX and shutil.which("wmctrl"):
            proc = await asyncio.create_subprocess_exec("wmctrl", "-l", stdout=asyncio.subprocess.PIPE)
            out, _ = await proc.communicate()
            return out.decode(errors="ignore")[:3000] or "(koi window nahi)"
        proc = await asyncio.create_subprocess_exec("osascript", "-e", 'tell application "System Events" to get name of every process whose background only is false', stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        return out.decode(errors="ignore")[:2000]
    except Exception as exc:
        return f"⚠️ list_windows unavailable: {exc}"


@tool
async def type_text(text: str, press_enter: bool = False) -> str:
    """Type text into the currently focused window (keyboard automation).

    Zero-install: Windows (PowerShell SendKeys / clipboard), macOS (osascript keystroke),
    Linux (xdotool/wtype). pyautogui ho to wahi pehle use hota hai.
    """
    await asyncio.sleep(0.4)

    # 1) pyautogui agar installed hai (sabse smooth)
    try:
        import pyautogui  # type: ignore

        pyautogui.typewrite(text, interval=0.01) if text.isascii() else pyautogui.write(text)
        if press_enter:
            pyautogui.press("enter")
        return f"⌨️ {len(text)} characters type kiye (pyautogui)."
    except ImportError:
        pass
    except Exception as exc:  # pyautogui present par display/headless issue
        pass

    # 2) native fallback — koi install nahi chahiye
    ok, detail = await _native_type(text, press_enter)
    if ok:
        return f"⌨️ {len(text)} characters type kiye ({detail})."
    return (f"❌ typing fail: {detail}. Windows/macOS par kuch install nahi chahiye; "
            f"Linux par `xdotool`/`wtype` chahiye — ya `pip install pyautogui` karo.")


@tool
async def press_hotkey(keys: str) -> str:
    """Press a keyboard shortcut, e.g. 'ctrl+s', 'ctrl+shift+t', 'alt+f4', 'win+d'."""
    combo = [k.strip() for k in (keys or "").split("+") if k.strip()]
    if not combo:
        return "❌ Khali hotkey."
    try:
        import pyautogui  # type: ignore

        pyautogui.hotkey(*combo)
        return f"⌨️ {'+'.join(combo)} press kiya (pyautogui)."
    except ImportError:
        pass
    except Exception:
        pass
    ok, detail = await _native_hotkey(combo)
    return (f"⌨️ {'+'.join(combo)} press kiya ({detail})." if ok
            else f"❌ hotkey fail: {detail}")


@tool
async def control_volume(action: str = "up", steps: int = 5) -> str:
    """Control system volume. action: 'up' | 'down' | 'mute'."""
    action = (action or "up").lower()
    try:
        if IS_WINDOWS:
            key = {"up": 0xAF, "down": 0xAE, "mute": 0xAD}.get(action)
            if key is None:
                return "❌ action 'up'/'down'/'mute' me se ho."
            import ctypes
            for _ in range(max(1, steps if action != "mute" else 1)):
                ctypes.windll.user32.keybd_event(key, 0, 0, 0)
                ctypes.windll.user32.keybd_event(key, 0, 2, 0)
            return f"🔊 Volume {action}."
        if IS_MAC:
            cmd = {"up": "set volume output volume ((output volume of (get volume settings)) + 6)",
                   "down": "set volume output volume ((output volume of (get volume settings)) - 6)",
                   "mute": "set volume output muted true"}.get(action)
            if not cmd:
                return "❌ invalid action."
            proc = await asyncio.create_subprocess_exec("osascript", "-e", cmd)
            await proc.communicate()
            return f"🔊 Volume {action}."
        # linux
        cmd = {"up": ["amixer", "-q", "sset", "Master", f"{steps}%+"],
               "down": ["amixer", "-q", "sset", "Master", f"{steps}%-"],
               "mute": ["amixer", "-q", "sset", "Master", "toggle"]}.get(action)
        if not cmd or not shutil.which("amixer"):
            return "⚠️ Linux par amixer nahi mila."
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.communicate()
        return f"🔊 Volume {action}."
    except Exception as exc:
        return f"❌ volume error: {exc}"


@tool
async def take_screenshot(name: str = "screen") -> str:
    """Take a screenshot and save it in the workspace screenshots folder.

    Zero-install: Windows (PowerShell + System.Drawing), macOS (screencapture),
    Linux (gnome-screenshot / scrot / import). pyautogui ho to wahi pehle.
    """
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOTS_DIR / f"{name.strip().replace(' ', '_')[:40]}.png"

    # 1) pyautogui (agar hai)
    try:
        import pyautogui  # type: ignore

        pyautogui.screenshot().save(path)
        if path.exists():
            return f"📸 Screenshot saved: {path}"
    except Exception:
        pass

    # 2) native raaste
    try:
        if IS_WINDOWS:
            ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
                  "$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
                  "$img = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
                  "$g = [System.Drawing.Graphics]::FromImage($img); "
                  "$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size); "
                  f"$img.Save('{str(path)}', [System.Drawing.Imaging.ImageFormat]::Png)")
            code, out = await _run_quiet("powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
        elif IS_MAC:
            code, out = await _run_quiet("screencapture", "-x", str(path))
        elif shutil.which("gnome-screenshot"):
            code, out = await _run_quiet("gnome-screenshot", "-f", str(path))
        elif shutil.which("scrot"):
            code, out = await _run_quiet("scrot", "-o", str(path))
        elif shutil.which("import"):
            code, out = await _run_quiet("import", "-window", "root", str(path))
        else:
            return ("❌ Screenshot nahi ho paya — Linux par `gnome-screenshot`/`scrot`/ImageMagick "
                    "chahiye, ya `pip install pyautogui`. (Headless server par screen hi nahi hoti.)")
        if path.exists() and path.stat().st_size > 0:
            return f"📸 Screenshot saved: {path} ({path.stat().st_size // 1024} KB)"
        return f"❌ Screenshot fail: {out[:160] or 'output file nahi bani'}"
    except Exception as exc:
        return f"❌ screenshot error: {exc}"


def _is_dangerous(cmd: str) -> str | None:
    low = (cmd or "").lower()
    for bad in _DANGEROUS:
        if bad in low:
            return bad
    return None


@tool
async def run_shell_command(
    command: str,
    cwd: str = "",
    timeout: int = 120,
    confirmed: bool = False,
    background: bool = False,
    name: str = "",
) -> str:
    """Kisi bhi shell/terminal command ko chalao (git clone, npm install, pip, ffmpeg, python CLI, curl...).

    • background=True → process background me chalta hai (dev server/watcher) aur process id milta hai;
      phir `process_logs`/`process_stop` use karo.
    • Risky commands (delete/kill/install/git push) ke liye pehle owner ki permission lo,
      phir `confirmed=True` bhejo (approval mode: JARVIS_EXEC_APPROVAL=off|risky|all).
    • Passwords/keys: command me ${SECRET_github_token} likho — asli value safely inject hoti hai
      aur output me redact ho jaati hai.
    """
    if not SAFETY_ALLOW_SHELL:
        return "🚫 run_shell_command disabled hai (JARVIS_ALLOW_SHELL=0)."

    bad = _is_dangerous(command)
    if bad:
        return (f"🚫 Blocked: '{bad}' jaisa destructive pattern mila. "
                f"Ye kabhi allow nahi hota (chahe confirmed=True ho).")

    risky = _is_risky(command)
    mode = EXEC_APPROVAL
    from ..memory.session_state import get_state

    if mode == "all" and not confirmed:
        # har command ke liye owner ka explicit "haan"
        return (f"🔐 Approval chahiye: `{command[:120]}`"
                + (f" (risky pattern: `{risky.strip()}`)" if risky else "")
                + "\nOwner se 'haan' lo, phir dobara call karo `confirmed=true` ke saath. "
                  "(JARVIS_EXEC_APPROVAL=off karke ise band kar sakte ho.)")
    if risky and mode == "risky":
        # PEHLE sirf `not confirmed` par gate lagta tha — matlab LLM khud `confirmed=true`
        # bhej kar gate bypass kar sakta tha (self-approval). Ab do cheezein chahiye:
        #   1) owner ka fresh "haan" (grant_exec_approval se, 5 min TTL) aur
        #   2) `confirmed=true` (ek hi command ke liye — use hote hi token consume).
        granted = get_state().get("exec_approval_granted", {}) or {}
        if not _approval_fresh(granted):
            return (
                f"🔐 Ye command risky hai: `{command[:160]}`\n"
                f"Confirm karne ke liye owner se poocho: \"kar do\". Jab owner haan bole, tab "
                f"`grant_exec_approval` chalao aur phir `confirmed=true` ke saath dobara try karo."
            )
        if not confirmed:
            return (f"🔐 Owner ka approval mil gaya hai, par `confirmed=true` chahiye: `{command[:120]}`")
        _consume_approval()  # ek approval = ek command

    work_dir = Path(cwd).expanduser() if cwd else WORKSPACE_DIR
    if not work_dir.exists():
        work_dir = WORKSPACE_DIR

    expanded = _expand_secrets(command)

    if background:
        from ..runtime.process_manager import start_process

        try:
            info = start_process(expanded, cwd=str(work_dir), name=name)
            await asyncio.sleep(1.5)
            from ..runtime.process_manager import read_logs

            tail = read_logs(info.pid, tail_lines=10)
            return (f"🚀 Background me chalu: **{info.pid}** (os pid {info.popen.pid if info.popen else '?'})\n"
                    f"📜 log: {info.log_path}\n---\n{_redact(tail)[-1200:]}")
        except Exception as exc:
            return f"❌ background start fail: {exc}"

    try:
        proc = await asyncio.create_subprocess_shell(
            expanded,
            cwd=str(work_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return (f"⏱️ Command {timeout}s me complete nahi hua — kill kar diya.\n"
                    f"Tip: agar ye long-running hai (server/watcher) to `background=true` use karo.")
        stdout = (out or b"").decode(errors="ignore")
        stderr = (err or b"").decode(errors="ignore")
        rc = proc.returncode
        body = f"$ {_redact(command)[:300]}\n[exit {rc}]\n{_redact(stdout)[-4000:]}"
        if stderr.strip():
            body += f"\n--- stderr ---\n{_redact(stderr)[-2000:]}"
        return body
    except Exception as exc:
        return f"❌ shell error: {exc}"


@tool
async def grant_exec_approval() -> str:
    """Owner ne risky command ke liye 'haan' bola ho to ye call karo — agle `confirmed=true` call allow ho jayegi."""
    from ..memory.session_state import get_state

    granted = get_state().get("exec_approval_granted", {}) or {}
    granted[EXEC_APPROVAL_TOKEN] = {"at": time.time(), "ttl_s": APPROVAL_TTL_S}
    get_state().set("exec_approval_granted", granted)
    mins = int(APPROVAL_TTL_S // 60)
    return (f"✅ Approval granted — {mins} minute ke andar ek risky command "
            f"`confirmed=true` ke saath chal jaayegi (ek hi baar).")

@tool
async def system_info() -> str:
    """Machine ki info: OS, python, CPU cores, disk, workspace path."""
    import shutil as _sh
    try:
        import psutil  # type: ignore
        mem = psutil.virtual_memory()
        mem_line = f"RAM: {mem.used/1e9:.1f}/{mem.total/1e9:.1f} GB ({mem.percent}%)"
        cpu_line = f"CPU: {psutil.cpu_count(logical=True)} cores @ {psutil.cpu_percent(interval=0.2)}%"
    except Exception:
        mem_line, cpu_line = "RAM: n/a", f"CPU: {os.cpu_count()} cores"

    disk = _sh.disk_usage(str(WORKSPACE_DIR))
    return (
        f"🖥️ {platform.system()} {platform.release()} ({platform.machine()})\n"
        f"🐍 Python {sys.version.split()[0]} @ {sys.executable}\n"
        f"{cpu_line}\n{mem_line}\n"
        f"💾 Disk ({WORKSPACE_DIR}): {disk.used/1e9:.1f}/{disk.total/1e9:.1f} GB free={disk.free/1e9:.1f} GB\n"
        f"📁 Workspace: {WORKSPACE_DIR}"
    )


try:
    from ..runtime.process_manager import PROCESS_TOOLS
except Exception:  # pragma: no cover
    PROCESS_TOOLS = []

SYSTEM_TOOLS = [
    open_app,
    close_app,
    list_windows,
    type_text,
    press_hotkey,
    control_volume,
    take_screenshot,
    run_shell_command,
    grant_exec_approval,
    system_info,
    *PROCESS_TOOLS,
]
