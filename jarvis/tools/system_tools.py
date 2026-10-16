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
import platform
import shutil
import signal
import sys
from pathlib import Path

from langchain_core.tools import tool

from ..config import DESKTOP_KEYBOARD_ENABLED, SAFETY_ALLOW_SHELL, SCREENSHOTS_DIR, WORKSPACE_DIR
from ..security.gate import authorize_command, report_approval_completion
from ..security.permissions import current_surface

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

def _expand_secrets(text: str) -> str:
    """
    ${SECRET_name} / $JARVIS_SECRET_name placeholders ko asli value se badlo —
    is tarah agent command likhta hai par asli secret uske context me nahi aata.
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

    for key, value in _os.environ.items():
        if key.startswith("JARVIS_SECRET_") and value and len(value) > 3:
            text = text.replace(value, f"<{key}>")
    return text


async def focus_window(title_keyword: str) -> bool:
    """App-finder ki single verified window-focus implementation use karo."""
    from .app_finder import focus_window as shared_focus_window

    return await shared_focus_window(title_keyword)



# ---------------------------------------------------------------------------
# NATIVE INPUT LAYER — bina kisi pip install ke (pyautogui optional hai)
# ---------------------------------------------------------------------------
# Kyun: pehle type_text/press_hotkey sirf pyautogui par chalte the, jo default
# dependencies me hi nahi hai. Isliye Windows par typing "❌ pyautogui install karo"
# return karta tha — aur LLM phir bhi "likh diya" bol deta tha. Ab har OS ka
# apna native rasta hai (PowerShell SendKeys / osascript / xdotool).
_PS_SENDBAR = ('Add-Type -AssemblyName System.Windows.Forms; '
               'Add-Type -AssemblyName Microsoft.VisualBasic; '
               '[System.Windows.Forms.SendKeys]::SendWait({combo})')


def _powershell_single_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _applescript_literal(value: str) -> str:
    escaped = (str(value).replace("\\", "\\\\").replace('"', '\\"')
               .replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t"))
    return f'"{escaped}"'


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
    return {"ctrl": "^", "control": "^", "alt": "%", "shift": "+"}.get(p, p)


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
    cmd = _PS_SENDBAR.format(combo=_powershell_single_quote(combo))
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
        script = (
            'on run argv\n'
            'tell application "System Events" to keystroke (item 1 of argv)\n'
            'if (item 2 of argv) is "yes" then tell application "System Events" to key code 36\n'
            'end run'
        )
        code, out = await _run_quiet("osascript", "-e", script, text, "yes" if press_enter else "no")
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
        unsupported = {"win", "super", "meta", "cmd", "command"}
        if any(part.lower() in unsupported for part in combo):
            return False, "Windows native SendKeys me Win/Super key supported nahi; Ctrl/Alt/Shift shortcuts use karo."
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
        script = f'tell application "System Events" to keystroke {_applescript_literal(key)}{using}'
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
    from .app_finder import open_any_app

    return await open_any_app.ainvoke({"query": app_title})


@tool
async def close_app(window_title: str) -> str:
    """Close a running application / window by title keyword ("notepad band karo", "close vlc")."""
    kw = (window_title or "").strip().lower()
    if not kw:
        return "❌ Khali title."
    approval = await authorize_command(
        f"close application window {window_title!r}", str(WORKSPACE_DIR), force=True,
        surface=current_surface(), reason="closing a process/window not started by this session",
    )
    if not approval.allowed:
        return f"🚫 Window close nahi kiya: {approval.reason}"
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
            script = f"tell application {_applescript_literal(window_title)} to quit"
            proc = await asyncio.create_subprocess_exec("osascript", "-e", script)
            await proc.communicate()
            if proc.returncode != 0:
                return f"⚠️ {window_title} quit nahi hua."
            return f"✅ {window_title} quit signal bhej diya."
        proc = await asyncio.create_subprocess_exec(
            "ps", "-eo", "pid=,ppid=,comm=", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        target = Path(window_title).name.casefold().removesuffix(".exe")
        own_pids = {os.getpid(), os.getppid()}
        matches: list[int] = []
        for line in (out or b"").decode(errors="ignore").splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) != 3:
                continue
            try:
                pid, ppid = int(fields[0]), int(fields[1])
            except ValueError:
                continue
            process_name = fields[2].casefold()
            if pid in own_pids or ppid in own_pids:
                continue
            if process_name == target:
                matches.append(pid)
        closed = 0
        for pid in matches:
            try:
                os.kill(pid, signal.SIGTERM)
                closed += 1
            except (ProcessLookupError, PermissionError):
                continue
        return (f"✅ Exact process '{target}' ko close signal bheja ({closed})." if closed else
                f"⚠️ Exact process '{target}' match nahi hua; koi process nahi roka.")
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
    if not DESKTOP_KEYBOARD_ENABLED:
        return "🚫 Keyboard input disabled hai (`DESKTOP_KEYBOARD_ENABLED=false`)."
    # 1) pyautogui agar installed hai (sabse smooth)
    try:
        import pyautogui  # type: ignore

        pyautogui.typewrite(text, interval=0.01) if text.isascii() else pyautogui.write(text)
        if press_enter:
            pyautogui.press("enter")
        return f"⌨️ {len(text)} characters type kiye (pyautogui)."
    except ImportError:
        pass
    except Exception:  # pyautogui present par display/headless issue
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
    if not DESKTOP_KEYBOARD_ENABLED:
        return "🚫 Keyboard input disabled hai (`DESKTOP_KEYBOARD_ENABLED=false`)."
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
    if not DESKTOP_KEYBOARD_ENABLED:
        return "🚫 Keyboard/media input disabled hai (`DESKTOP_KEYBOARD_ENABLED=false`)."
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
                  f"$img.Save({_powershell_single_quote(str(path))}, [System.Drawing.Imaging.ImageFormat]::Png)")
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


@tool
async def run_shell_command(
    command: str,
    cwd: str = "",
    timeout: int = 120,
    background: bool = False,
    name: str = "",
) -> str:
    """Kisi shell command ko chalao; flagged commands ke liye human-only Gate approval."""
    if not SAFETY_ALLOW_SHELL:
        return "🚫 run_shell_command disabled hai (JARVIS_ALLOW_SHELL=0)."

    work_dir = Path(cwd).expanduser() if cwd else WORKSPACE_DIR
    if not work_dir.exists():
        work_dir = WORKSPACE_DIR
    expanded = _expand_secrets(command)
    approval = await authorize_command(
        command, str(work_dir), surface=current_surface(), assessment_command=expanded,
    )
    if not approval.allowed:
        message = f"🚫 Command nahi chalaya: {approval.reason}. Koi command execute nahi hui."
        await report_approval_completion(approval.request_id, current_surface(), "cancelled", approval.reason)
        return message

    if background:
        from ..runtime.process_manager import start_process

        try:
            info = start_process(expanded, cwd=str(work_dir), name=name)
            from ..runtime.process_manager import read_logs

            tail = read_logs(info.pid, tail_lines=10)
            if not tail.strip():
                tail = "(process started; check process_logs for output)"
            state = ("running" if info.alive else
                     f"exited ({info.popen.poll() if info.popen is not None else 'unknown'})")
            response = (f"⚙️ Background process {state}: **{info.pid}** "
                        f"(os pid {info.popen.pid if info.popen else '?'})\n"
                        f"📜 log: {info.log_path}\n---\n{_redact(tail)[-1200:]}")
            await report_approval_completion(approval.request_id, current_surface(), "ran", response)
            return response
        except Exception as exc:
            response = f"❌ background start fail: {exc}"
            await report_approval_completion(approval.request_id, current_surface(), "failed", response)
            return response

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
            response = (f"⏱️ Command {timeout}s me complete nahi hua — kill kar diya.\n"
                        f"Tip: agar ye long-running hai (server/watcher) to `background=true` use karo.")
            await report_approval_completion(approval.request_id, current_surface(), "failed", response)
            return response
        stdout = (out or b"").decode(errors="ignore")
        stderr = (err or b"").decode(errors="ignore")
        rc = proc.returncode
        body = f"$ {_redact(command)[:300]}\n[exit {rc}]\n{_redact(stdout)[-4000:]}"
        if stderr.strip():
            body += f"\n--- stderr ---\n{_redact(stderr)[-2000:]}"
        await report_approval_completion(
            approval.request_id, current_surface(), "ran" if rc == 0 else "failed", body,
        )
        return body
    except Exception as exc:
        response = f"❌ shell error: {exc}"
        await report_approval_completion(approval.request_id, current_surface(), "failed", response)
        return response


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
    system_info,
    *PROCESS_TOOLS,
]
