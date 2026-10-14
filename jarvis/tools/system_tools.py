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

# NOTE: dangerous/risky detection ab `jarvis/security/gate.py` me hai (real parsing:
# shlex per segment, flag normalize, sudo/bash -c unwrap). Yahan koi substring gate nahi —
# model kabhi approve nahi kar sakta, sirf human (CLI/Telegram/voice/web) approval deta hai.


def _expand_secrets(text: str) -> str:
    """
    ${SECRET_name} / $JARVIS_SECRET_name placeholders ko asli value se badlo —
    is tarah agent command likhta hai par asli secret uske context me nahi aata (OpenClaw pattern).
    """
    import os as _os
    import re as _re

    def _sub(match) -> str:
        key = (match.group(1) or match.group(2) or "").strip()
        bare = (
            key[len("SECRET_") :]
            if key.upper().startswith("SECRET_")
            else (key[len("JARVIS_SECRET_") :] if key.upper().startswith("JARVIS_SECRET_") else key)
        )
        candidates = [
            key,
            key.upper(),
            f"JARVIS_{key}",
            f"JARVIS_{key.upper()}",
            f"JARVIS_SECRET_{bare}",
            f"JARVIS_SECRET_{bare.upper()}",
            bare,
            bare.upper(),
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
            proc = await asyncio.create_subprocess_exec(
                "open", "-a", target, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
        elif IS_WINDOWS:
            if target.endswith(":"):  # ms-settings:
                await asyncio.create_subprocess_exec("cmd", "/c", "start", "", target)
                return True, target
            proc = await asyncio.create_subprocess_exec(
                "cmd", "/c", "start", "", target, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
        else:
            if shutil.which("gtk-launch"):
                proc = await asyncio.create_subprocess_exec(
                    target, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    target, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
                )
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


_LAUNCH_TTL_S = 24 * 3600


def record_app_launch(title: str) -> None:
    """Agent ne ye app khola — close_app isse ownership verify karta hai (Gate)."""
    try:
        from ..memory.session_state import get_state

        launched = get_state().get("launched_apps", {}) or {}
        launched[str(title or "").strip().lower()] = time.time()
        # purane entries saaf karo
        cutoff = time.time() - _LAUNCH_TTL_S
        launched = {k: v for k, v in launched.items() if float(v or 0) >= cutoff}
        get_state().set("launched_apps", launched)
    except Exception:
        pass


def was_launched_by_agent(title: str) -> bool:
    try:
        from ..memory.session_state import get_state

        launched = get_state().get("launched_apps", {}) or {}
        at = float(launched.get(str(title or "").strip().lower()) or 0)
        return (time.time() - at) <= _LAUNCH_TTL_S
    except Exception:
        return False


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
_PS_SENDBAR = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "Add-Type -AssemblyName Microsoft.VisualBasic; "
    "Start-Sleep -Milliseconds 350; "
    '[System.Windows.Forms.SendKeys]::SendWait("{combo}")'
)


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
    return {
        "ctrl": "^",
        "control": "^",
        "alt": "%",
        "shift": "+",
        "win": "^",
        "cmd": "^",
        "command": "^",
        "meta": "^",
        "super": "^",
    }.get(p, p)


async def _run_quiet(*args: str, stdin_data: bytes | None = None) -> tuple[int, str]:
    """Chhota subprocess runner (stdout/stderr capture, crash-safe)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
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
        if any(ord(ch) > 126 for ch in text):  # Hindi/emoji → clipboard route
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
        return await _windows_sendkeys(
            "".join(parts[:-1]) + "{" + parts[-1].upper() + "}" if len(parts[-1]) > 1 else "".join(parts)
        )
    if IS_MAC:
        mods = {
            "ctrl": "control down",
            "control": "control down",
            "alt": "option down",
            "option": "option down",
            "shift": "shift down",
            "cmd": "command down",
            "command": "command down",
            "win": "command down",
            "super": "command down",
        }
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
async def _native_click(x: int, y: int, button: str, clicks: int) -> tuple[bool, str]:
    """Bina pyautogui ke mouse click (OS native raaste)."""
    if IS_WINDOWS:
        btn_down = {"left": 0x0002, "right": 0x0008, "middle": 0x0020}.get(button, 0x0002)
        btn_up = btn_down * 2
        ps = (
            f'Add-Type -MemberDefinition \'[DllImport("user32.dll")] '
            "public static extern void mouse_event(int f,int x,int y,int d,int e); "
            '[DllImport("user32.dll")] public static extern bool SetCursorPos(int x,int y);\' '
            f"-Name M -Namespace W; [W.M]::SetCursorPos({x},{y}); "
            + "".join(
                f"[W.M]::mouse_event({btn_down},0,0,0,0); [W.M]::mouse_event({btn_up},0,0,0,0); "
                for _ in range(max(1, clicks))
            )
        )
        code, out = await _run_quiet("powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
        return (True, "windows native click") if code == 0 else (False, out[:160] or "powershell click fail")
    if IS_MAC:
        if shutil.which("cliclick"):
            args = ["cliclick"]
            for _ in range(max(1, clicks)):
                args += [f"m:{x},{y}", "c:." if button == "left" else f"c:{button}"]
            code, out = await _run_quiet(*args)
            return (True, "cliclick") if code == 0 else (False, out[:160] or "cliclick fail")
        return (False, "macOS par click ke liye `brew install cliclick` ya `pip install pyautogui` chahiye")
    # linux
    if shutil.which("xdotool"):
        args = ["xdotool", "mousemove", str(x), str(y)]
        btn_num = {"left": "1", "right": "3", "middle": "2"}.get(button, "1")
        for _ in range(max(1, clicks)):
            args += ["click", btn_num]
        code, out = await _run_quiet(*args)
        return (True, "xdotool") if code == 0 else (False, out[:160] or "xdotool click fail")
    if shutil.which("wtype"):
        return (False, "wtype se click nahi hota — `pip install pyautogui` karo ya xdotool (X11) lagao")
    return (False, "Linux par click ke liye `xdotool` (X11) ya `pip install pyautogui` chahiye")


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
    record_app_launch(app_title)
    await asyncio.sleep(1.2)
    focused = await focus_window(app_title if IS_WINDOWS else Path(target).stem)
    return f"🚀 {app_title} launch ho gaya" + (" aur focus me hai." if focused else ".")


@tool
async def close_app(window_title: str) -> str:
    """Close a running application / window by title keyword ("notepad band karo", "close vlc")."""
    kw = (window_title or "").strip().lower()
    if not kw:
        return "❌ Khali title."
    # Gate: agent ne khud ye app nahi khola tha to close = foreign kill → human approval
    if not was_launched_by_agent(window_title):
        from ..security.gate import check_action

        gate = await asyncio.to_thread(check_action, "kill", f"close_app({window_title})")
        if not gate.allowed:
            return gate.message
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
        proc = await asyncio.create_subprocess_exec(
            "pkill", "-f", window_title, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
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
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            'tell application "System Events" to get name of every process whose background only is false',
            stdout=asyncio.subprocess.PIPE,
        )
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
    return (
        f"❌ typing fail: {detail}. Windows/macOS par kuch install nahi chahiye; "
        f"Linux par `xdotool`/`wtype` chahiye — ya `pip install pyautogui` karo."
    )


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
    return f"⌨️ {'+'.join(combo)} press kiya ({detail})." if ok else f"❌ hotkey fail: {detail}"


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
            cmd = {
                "up": "set volume output volume ((output volume of (get volume settings)) + 6)",
                "down": "set volume output volume ((output volume of (get volume settings)) - 6)",
                "mute": "set volume output muted true",
            }.get(action)
            if not cmd:
                return "❌ invalid action."
            proc = await asyncio.create_subprocess_exec("osascript", "-e", cmd)
            await proc.communicate()
            return f"🔊 Volume {action}."
        # linux
        cmd = {
            "up": ["amixer", "-q", "sset", "Master", f"{steps}%+"],
            "down": ["amixer", "-q", "sset", "Master", f"{steps}%-"],
            "mute": ["amixer", "-q", "sset", "Master", "toggle"],
        }.get(action)
        if not cmd or not shutil.which("amixer"):
            return "⚠️ Linux par amixer nahi mila."
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.communicate()
        return f"🔊 Volume {action}."
    except Exception as exc:
        return f"❌ volume error: {exc}"


async def _capture_screenshot(path: Path) -> tuple[bool, str]:
    """Screen capture karke `path` par save karo. Returns (ok, detail)."""
    # 1) pyautogui (agar hai)
    try:
        import pyautogui  # type: ignore

        pyautogui.screenshot().save(path)
        if path.exists() and path.stat().st_size > 0:
            return True, f"pyautogui ({path.stat().st_size // 1024} KB)"
    except Exception:
        pass

    # 2) native raaste
    try:
        if IS_WINDOWS:
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
                "$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
                "$img = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
                "$g = [System.Drawing.Graphics]::FromImage($img); "
                "$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size); "
                f"$img.Save('{str(path)}', [System.Drawing.Imaging.ImageFormat]::Png)"
            )
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
            return (
                False,
                "Linux par `gnome-screenshot`/`scrot`/ImageMagick chahiye, "
                "ya `pip install pyautogui`. (Headless server par screen hi nahi hoti.)",
            )
        if path.exists() and path.stat().st_size > 0:
            return True, f"native ({path.stat().st_size // 1024} KB)"
        return False, out[:160] or "output file nahi bani"
    except Exception as exc:
        return False, f"screenshot error: {exc}"


@tool
async def take_screenshot(name: str = "screen") -> str:
    """Take a screenshot and save it in the workspace screenshots folder.

    Zero-install: Windows (PowerShell + System.Drawing), macOS (screencapture),
    Linux (gnome-screenshot / scrot / import). pyautogui ho to wahi pehle.
    """
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOTS_DIR / f"{name.strip().replace(' ', '_')[:40] or 'screen'}.png"
    ok, detail = await _capture_screenshot(path)
    if ok:
        return f"📸 Screenshot saved: {path} ({detail})"
    return f"❌ Screenshot nahi ho paya — {detail}"


@tool
async def click_at(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    """Mouse se screen par click karo (absolute pixel coordinates).

    Args:
        x, y: screen coordinates (0,0 = top-left). `describe_screen` se pehle dekh lo kahan hai.
        button: left | right | middle.
        clicks: 1 = single, 2 = double-click.
    """
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return "❌ x, y numbers hone chahiye (jaise 500, 300)."
    if x < 0 or y < 0 or x > 10000 or y > 10000:
        return "❌ coordinates 0..10000 ke andar do."
    button = (button or "left").lower()
    if button not in ("left", "right", "middle"):
        return "❌ button left/right/middle me se ho."
    clicks = max(1, min(int(clicks or 1), 3))
    await asyncio.sleep(0.3)

    try:
        import pyautogui  # type: ignore

        pyautogui.click(x=x, y=y, button=button, clicks=clicks)
        return f"🖱️ Click ({x},{y}) {button} x{clicks} (pyautogui)."
    except ImportError:
        pass
    except Exception:
        pass  # display/headless issue → native try karo

    ok, detail = await _native_click(x, y, button, clicks)
    if ok:
        return f"🖱️ Click ({x},{y}) {button} x{clicks} ({detail})."
    return f"❌ click fail: {detail}"


@tool
async def describe_screen(question: str = "") -> str:
    """Screen ka screenshot lo aur vision model se dekho kya dikh raha hai.

    Args:
        question: specific sawaal, jaise "error popup me kya likha hai?" (khaali = poori screen batao).

    Offline/demo mode me model generic jawab dega (screenshot path hamesha milta hai);
    vision-capable provider (Gemini/OpenRouter) par asli screen reading milti hai.
    """
    import base64

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOTS_DIR / "screen_describe.png"
    ok, detail = await _capture_screenshot(path)
    if not ok:
        return f"❌ Screen dekh nahi paya — screenshot fail: {detail}"
    try:
        raw = path.read_bytes()
        try:
            import io as _io

            from PIL import Image  # type: ignore

            img = Image.open(_io.BytesIO(raw)).convert("RGB")
            img.thumbnail((1280, 1280))
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            raw = buf.getvalue()
        except Exception:
            pass  # PIL nahi → original PNG bhejo
        b64 = base64.b64encode(raw).decode("ascii")
        from langchain_core.messages import HumanMessage

        from ..llm import get_chat_model

        q = (question or "").strip() or "Is screen par kya dikh raha hai? Mukhya cheezein short me batao."
        msg = HumanMessage(
            content=[
                {"type": "text", "text": f"Screen dekh kar Hinglish me jawab do: {q}"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
        )
        model = get_chat_model("vision")
        resp = await model.ainvoke([msg])
        answer = str(getattr(resp, "content", resp) or "").strip()
        if not answer:
            return f"⚠️ Vision model ne khaali jawab diya. 📸 Screenshot: {path}"
        return f"👁️ {answer}\n📸 Screenshot: {path}"
    except Exception as exc:
        return f"⚠️ Vision model fail ({type(exc).__name__}: {str(exc)[:150]}). 📸 Screenshot: {path}"


@tool
async def run_shell_command(
    command: str,
    cwd: str = "",
    timeout: int = 120,
    background: bool = False,
    name: str = "",
) -> str:
    """Kisi bhi shell/terminal command ko chalao (git clone, npm install, pip, ffmpeg, python CLI, curl...).

    • background=True → process background me chalta hai (dev server/watcher) aur process id milta hai;
      phir `process_logs`/`process_stop` use karo.
    • Dangerous commands (recursive delete, disk ops, force-push, kill, ...) par owner se
      approval maangi jaati hai (Dangerous-Command Gate, JARVIS_EXEC_APPROVAL=off|risky|all).
      Model khud approve NAHI kar sakta — approval sirf human deta hai (CLI/Telegram/voice/web).
    • Passwords/keys: command me ${SECRET_github_token} likho — asli value safely inject hoti hai
      aur output me redact ho jaati hai.
    """
    if not SAFETY_ALLOW_SHELL:
        return "🚫 run_shell_command disabled hai (JARVIS_ALLOW_SHELL=0)."

    # Dangerous-Command Gate — human-only approval (model-visible koi bypass nahi)
    from ..security.gate import check_shell

    gate = await asyncio.to_thread(check_shell, command, cwd or "")
    if not gate.allowed:
        return gate.message

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
            return (
                f"🚀 Background me chalu: **{info.pid}** (os pid {info.popen.pid if info.popen else '?'})\n"
                f"📜 log: {info.log_path}\n---\n{_redact(tail)[-1200:]}"
            )
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
            return (
                f"⏱️ Command {timeout}s me complete nahi hua — kill kar diya.\n"
                f"Tip: agar ye long-running hai (server/watcher) to `background=true` use karo."
            )
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
async def system_info() -> str:
    """Machine ki info: OS, python, CPU cores, disk, workspace path."""
    import shutil as _sh

    try:
        import psutil  # type: ignore

        mem = psutil.virtual_memory()
        mem_line = f"RAM: {mem.used / 1e9:.1f}/{mem.total / 1e9:.1f} GB ({mem.percent}%)"
        cpu_line = f"CPU: {psutil.cpu_count(logical=True)} cores @ {psutil.cpu_percent(interval=0.2)}%"
    except Exception:
        mem_line, cpu_line = "RAM: n/a", f"CPU: {os.cpu_count()} cores"

    disk = _sh.disk_usage(str(WORKSPACE_DIR))
    return (
        f"🖥️ {platform.system()} {platform.release()} ({platform.machine()})\n"
        f"🐍 Python {sys.version.split()[0]} @ {sys.executable}\n"
        f"{cpu_line}\n{mem_line}\n"
        f"💾 Disk ({WORKSPACE_DIR}): {disk.used / 1e9:.1f}/{disk.total / 1e9:.1f} GB free={disk.free / 1e9:.1f} GB\n"
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
    click_at,
    control_volume,
    take_screenshot,
    describe_screen,
    run_shell_command,
    system_info,
    *PROCESS_TOOLS,
]
