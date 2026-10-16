"""Typed desktop control with observe/act/verify semantics and testable backends.

Coordinates are always expressed in the last screenshot's pixel space. The controller
converts them to the backend's logical input space, so HiDPI screenshots do not shift clicks.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from ..config import (
    DESKTOP_ENABLED,
    DESKTOP_KEYBOARD_ENABLED,
    DESKTOP_MOUSE_ENABLED,
    SCREENSHOTS_DIR,
    WORKSPACE_DIR,
)
from ..security.gate import request_human_approval
from ..security.permissions import current_surface

OBSERVATION_TTL_S = 12.0


class DesktopUnavailable(RuntimeError):
    """The current environment cannot provide the requested desktop capability."""


@dataclass
class Observation:
    observation_id: str
    captured_at: float
    image_hash: str
    screenshot_path: str
    screenshot_size: tuple[int, int]
    device_size: tuple[int, int]
    active_window: str
    ocr_text: str
    ocr_status: str


class _PyAutoGUIBackend:
    def __init__(self) -> None:
        import pyautogui

        self.gui = pyautogui
        self.gui.FAILSAFE = True

    def screenshot(self):
        return self.gui.screenshot()

    def screen_size(self) -> tuple[int, int]:
        size = self.gui.size()
        return int(size.width), int(size.height)

    def click(self, x: int, y: int, button: str = "left") -> bool:
        self.gui.click(x=x, y=y, button=button)
        return True

    def move(self, x: int, y: int) -> bool:
        self.gui.moveTo(x, y, duration=0.12)
        return True

    def scroll(self, amount: int, x: int, y: int) -> bool:
        self.gui.moveTo(x, y, duration=0.08)
        self.gui.scroll(amount)
        return True

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> bool:
        self.gui.moveTo(x1, y1, duration=0.08)
        self.gui.dragTo(x2, y2, duration=0.25, button=button)
        return True

    def key_down(self, key: str) -> bool:
        self.gui.keyDown(str(key).lower())
        return True

    def key_up(self, key: str) -> bool:
        self.gui.keyUp(str(key).lower())
        return True

    def type_text(self, text: str) -> bool:
        if str(text).isascii():
            self.gui.write(str(text), interval=0.01)
            return True
        import pyperclip

        pyperclip.copy(str(text))
        self.gui.hotkey("ctrl", "v")
        return True

    def press(self, keys: list[str]) -> bool:
        self.gui.hotkey(*keys)
        return True

    def active_window(self) -> str:
        try:
            import pygetwindow

            return str(pygetwindow.getActiveWindowTitle() or "")
        except Exception:
            return ""

    def list_windows(self) -> list[str]:
        try:
            import pygetwindow

            return [str(window.title) for window in pygetwindow.getAllWindows() if window.title.strip()][:60]
        except Exception as exc:
            raise DesktopUnavailable(f"Window list unavailable: {type(exc).__name__}") from exc

    def focus_window(self, title: str) -> bool:
        try:
            import pygetwindow

            matches = [window for window in pygetwindow.getAllWindows() if title.casefold() in window.title.casefold()]
            if not matches:
                return False
            window = matches[0]
            if getattr(window, "isMinimized", False):
                window.restore()
            window.activate()
            return True
        except Exception:
            return False

    def clipboard_read(self) -> str:
        import pyperclip

        return str(pyperclip.paste() or "")

    def clipboard_write(self, text: str) -> bool:
        import pyperclip

        pyperclip.copy(str(text))
        return True


class _PynputBackend:
    """Optional keyboard/mouse backend; screenshots still use the native capture path."""

    def __init__(self) -> None:
        from pynput import keyboard, mouse

        self.mouse = mouse.Controller()
        self.keyboard = keyboard.Controller()
        self.keyboard_module = keyboard
        self.mouse_module = mouse

    async def screenshot(self):
        image = await _native_screenshot()
        self._cached_size = tuple(map(int, image.size))
        return image

    def screen_size(self) -> tuple[int, int]:
        return getattr(self, "_cached_size", (0, 0))

    def click(self, x: int, y: int, button: str = "left") -> bool:
        mouse_button = getattr(self.mouse_module.Button, button, None)
        if mouse_button is None:
            raise ValueError("button must be left, middle, or right")
        self.mouse.position = (x, y)
        self.mouse.click(mouse_button, 1)
        return True

    def move(self, x: int, y: int) -> bool:
        self.mouse.position = (x, y)
        return True

    def scroll(self, amount: int, x: int, y: int) -> bool:
        self.mouse.position = (x, y)
        self.mouse.scroll(0, amount)
        return True

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> bool:
        mouse_button = getattr(self.mouse_module.Button, button, None)
        if mouse_button is None:
            raise ValueError("button must be left, middle, or right")
        self.mouse.position = (x1, y1)
        self.mouse.press(mouse_button)
        try:
            steps = 12
            for index in range(1, steps + 1):
                self.mouse.position = (round(x1 + (x2 - x1) * index / steps),
                                       round(y1 + (y2 - y1) * index / steps))
                time.sleep(0.01)
        finally:
            self.mouse.release(mouse_button)
        return True

    def key_down(self, key: str) -> bool:
        mapping = {"ctrl": self.keyboard_module.Key.ctrl, "shift": self.keyboard_module.Key.shift,
                   "alt": self.keyboard_module.Key.alt, "cmd": self.keyboard_module.Key.cmd,
                   "enter": self.keyboard_module.Key.enter, "esc": self.keyboard_module.Key.esc,
                   "tab": self.keyboard_module.Key.tab, "space": self.keyboard_module.Key.space,
                   "delete": self.keyboard_module.Key.delete, "backspace": self.keyboard_module.Key.backspace}
        self.keyboard.press(mapping.get(str(key).lower(), key))
        return True

    def key_up(self, key: str) -> bool:
        mapping = {"ctrl": self.keyboard_module.Key.ctrl, "shift": self.keyboard_module.Key.shift,
                   "alt": self.keyboard_module.Key.alt, "cmd": self.keyboard_module.Key.cmd,
                   "enter": self.keyboard_module.Key.enter, "esc": self.keyboard_module.Key.esc,
                   "tab": self.keyboard_module.Key.tab, "space": self.keyboard_module.Key.space,
                   "delete": self.keyboard_module.Key.delete, "backspace": self.keyboard_module.Key.backspace}
        self.keyboard.release(mapping.get(str(key).lower(), key))
        return True

    def type_text(self, text: str) -> bool:
        self.keyboard.type(str(text))
        return True

    def press(self, keys: list[str]) -> bool:
        keymap = {"ctrl": self.keyboard_module.Key.ctrl, "shift": self.keyboard_module.Key.shift,
                  "alt": self.keyboard_module.Key.alt, "cmd": self.keyboard_module.Key.cmd,
                  "enter": self.keyboard_module.Key.enter, "esc": self.keyboard_module.Key.esc,
                  "tab": self.keyboard_module.Key.tab, "space": self.keyboard_module.Key.space,
                  "delete": self.keyboard_module.Key.delete, "backspace": self.keyboard_module.Key.backspace}
        parsed = [keymap.get(item.lower(), item) for item in keys]
        for key in parsed:
            self.keyboard.press(key)
        for key in reversed(parsed):
            self.keyboard.release(key)
        return True

    def active_window(self) -> str:
        return ""

    def list_windows(self) -> list[str]:
        return _native_list_windows_sync()

    def focus_window(self, title: str) -> bool:
        return _native_focus_window_sync(title)

    def clipboard_read(self) -> str:
        return _native_clipboard_read()

    def clipboard_write(self, text: str) -> bool:
        return _native_clipboard_write(text)


class _NativeBackend:
    """Platform-native screenshot/keyboard/mouse fallbacks; missing display is explicit."""

    async def screenshot(self):
        image = await _native_screenshot()
        self._cached_size = tuple(map(int, image.size))
        return image

    def screen_size(self) -> tuple[int, int]:
        return getattr(self, "_cached_size", (0, 0))

    async def click(self, x: int, y: int, button: str = "left") -> bool:
        system = platform.system()
        if system == "Windows":
            import ctypes

            ctypes.windll.user32.SetCursorPos(int(x), int(y))
            flags = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010),
                     "middle": (0x0020, 0x0040)}
            if button not in flags:
                raise ValueError("button must be left, middle, or right")
            down, up = flags[button]
            ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)
            return True
        if system == "Darwin" and shutil.which("cliclick"):
            return await _run_native("cliclick", f"c:{x},{y}")
        if system == "Linux" and shutil.which("xdotool"):
            button_number = {"left": "1", "middle": "2", "right": "3"}.get(button)
            if button_number is None:
                raise ValueError("button must be left, middle, or right")
            if not await _run_native("xdotool", "mousemove", str(x), str(y), "click", button_number):
                return False
            return True
        raise DesktopUnavailable("mouse input unavailable; install pyautogui or pynput (Linux: xdotool)")

    async def move(self, x: int, y: int) -> bool:
        system = platform.system()
        if system == "Windows":
            import ctypes

            return bool(ctypes.windll.user32.SetCursorPos(int(x), int(y)))
        if system == "Darwin" and shutil.which("cliclick"):
            return await _run_native("cliclick", f"m:{x},{y}")
        if system == "Linux" and shutil.which("xdotool"):
            return await _run_native("xdotool", "mousemove", str(x), str(y))
        raise DesktopUnavailable("mouse movement unavailable; install pyautogui or pynput")

    async def scroll(self, amount: int, x: int, y: int) -> bool:
        if platform.system() == "Linux" and shutil.which("xdotool"):
            button = "4" if amount > 0 else "5"
            for _ in range(min(abs(int(amount)), 20)):
                if not await _run_native("xdotool", "mousemove", str(x), str(y), "click", button):
                    return False
            return True
        raise DesktopUnavailable("scroll unavailable; install pyautogui or pynput")

    async def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> bool:
        system = platform.system()
        if system == "Windows":
            import ctypes

            flags = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010),
                     "middle": (0x0020, 0x0040)}
            if button not in flags:
                raise ValueError("button must be left, middle, or right")
            user32 = ctypes.windll.user32
            user32.SetCursorPos(int(x1), int(y1))
            user32.mouse_event(flags[button][0], 0, 0, 0, 0)
            user32.SetCursorPos(int(x2), int(y2))
            user32.mouse_event(flags[button][1], 0, 0, 0, 0)
            return True
        if system == "Darwin" and shutil.which("cliclick"):
            button_flag = {"left": "dd", "right": "dr", "middle": "dm"}.get(button)
            up_flag = {"left": "du", "right": "du", "middle": "du"}.get(button)
            if not button_flag:
                raise ValueError("button must be left, middle, or right")
            return await _run_native("cliclick", f"{button_flag}:{x1},{y1}", f"m:{x2},{y2}", f"{up_flag}:{x2},{y2}")
        if system == "Linux" and shutil.which("xdotool"):
            button_number = {"left": "1", "middle": "2", "right": "3"}.get(button)
            if not button_number:
                raise ValueError("button must be left, middle, or right")
            return await _run_native("xdotool", "mousemove", str(x1), str(y1), "mousedown", button_number,
                                     "mousemove", str(x2), str(y2), "mouseup", button_number)
        raise DesktopUnavailable("drag unavailable; install pyautogui/pynput (Linux: xdotool)")

    async def key_down(self, key: str) -> bool:
        system = platform.system()
        if system == "Linux" and shutil.which("xdotool"):
            return await _run_native("xdotool", "keydown", str(key))
        if system == "Windows":
            import ctypes

            code = ctypes.windll.user32.VkKeyScanW(str(key)[0]) & 0xFF
            ctypes.windll.user32.keybd_event(code, 0, 0, 0)
            return True
        if system == "Darwin":
            return await _run_native("osascript", "-e", f'tell application "System Events" to key down {{{str(key)}}}')
        raise DesktopUnavailable("key hold unavailable on this desktop backend")

    async def key_up(self, key: str) -> bool:
        system = platform.system()
        if system == "Linux" and shutil.which("xdotool"):
            return await _run_native("xdotool", "keyup", str(key))
        if system == "Windows":
            import ctypes

            code = ctypes.windll.user32.VkKeyScanW(str(key)[0]) & 0xFF
            ctypes.windll.user32.keybd_event(code, 0, 0x0002, 0)
            return True
        if system == "Darwin":
            return await _run_native("osascript", "-e", f'tell application "System Events" to key up {{{str(key)}}}')
        raise DesktopUnavailable("key release unavailable on this desktop backend")

    async def type_text(self, text: str) -> bool:
        from .system_tools import type_text

        result = await type_text.ainvoke({"text": str(text), "press_enter": False})
        return str(result).startswith("⌨️")

    async def press(self, keys: list[str]) -> bool:
        from .system_tools import press_hotkey

        result = await press_hotkey.ainvoke({"keys": "+".join(keys)})
        return str(result).startswith("⌨️")

    async def active_window(self) -> str:
        return ""

    async def list_windows(self) -> list[str]:
        text = await _native_list_windows()
        return [line.strip(" -*\t") for line in text.splitlines() if line.strip()][:60]

    async def focus_window(self, title: str) -> bool:
        return await _native_focus_window(title)

    async def clipboard_read(self) -> str:
        return await asyncio.to_thread(_native_clipboard_read)

    async def clipboard_write(self, text: str) -> bool:
        return await asyncio.to_thread(_native_clipboard_write, str(text))


class _DisabledBackend:
    async def screenshot(self):
        raise DesktopUnavailable("desktop tools are disabled; set DESKTOP_ENABLED=1")


_BACKEND: Any = None
_LAST_OBSERVATION: Observation | None = None


def set_desktop_backend(backend: Any | None) -> None:
    """Backend injection point for deterministic fake-backend tests and embedders."""
    global _BACKEND, _LAST_OBSERVATION
    _BACKEND = backend
    _LAST_OBSERVATION = None


def _backend() -> Any:
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    if not DESKTOP_ENABLED:
        _BACKEND = _DisabledBackend()
        return _BACKEND
    try:
        _BACKEND = _PyAutoGUIBackend()
    except Exception:
        try:
            _BACKEND = _PynputBackend()
        except Exception:
            _BACKEND = _NativeBackend()
    return _BACKEND


async def _call_backend(method: str, *args: Any) -> Any:
    target = getattr(_backend(), method, None)
    if not callable(target):
        raise DesktopUnavailable(f"desktop backend does not support {method}")
    result = target(*args)
    return await result if hasattr(result, "__await__") else result


def _open_image(value: Any):
    from PIL import Image

    if hasattr(value, "size") and hasattr(value, "save"):
        return value
    if isinstance(value, (bytes, bytearray)):
        return Image.open(io.BytesIO(value)).convert("RGB")
    if isinstance(value, (str, Path)):
        return Image.open(value).convert("RGB")
    raise DesktopUnavailable("screenshot backend returned no image")


async def _ocr(image: Any) -> tuple[str, str]:
    backend_ocr = getattr(_backend(), "ocr", None)
    if callable(backend_ocr):
        try:
            result = backend_ocr(image)
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, tuple):
                return str(result[0] or ""), str(result[1] or "ok")
            return str(result or ""), "ok"
        except Exception as exc:
            return "", f"backend OCR failed ({type(exc).__name__})"
    try:
        import pytesseract

        return str(pytesseract.image_to_string(image) or "").strip(), "ok"
    except Exception as exc:
        return "", f"OCR unavailable ({type(exc).__name__}); install pytesseract and Tesseract"


async def _observe_internal() -> Observation:
    global _LAST_OBSERVATION
    raw_image = await _call_backend("screenshot")
    image = _open_image(raw_image)
    screenshot_size = tuple(map(int, image.size))
    try:
        screen_size = await _call_backend("screen_size")
        device_size = (int(screen_size[0]), int(screen_size[1]))
    except Exception:
        device_size = screenshot_size
    if device_size[0] <= 0 or device_size[1] <= 0:
        device_size = screenshot_size
    png = io.BytesIO()
    image.save(png, format="PNG")
    image_bytes = png.getvalue()
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = SCREENSHOTS_DIR / f"desktop_{uuid.uuid4().hex[:10]}.png"
    screenshot_path.write_bytes(image_bytes)
    ocr_text, ocr_status = await _ocr(image)
    try:
        active_window = str(await _call_backend("active_window") or "")
    except Exception:
        active_window = ""
    now = time.time()
    observation = Observation(
        observation_id=f"{now_ns()}-{image_hash[:8]}", captured_at=now, image_hash=image_hash,
        screenshot_path=str(screenshot_path), screenshot_size=screenshot_size,
        device_size=device_size, active_window=active_window, ocr_text=ocr_text[:12000], ocr_status=ocr_status,
    )
    _LAST_OBSERVATION = observation
    return observation


def now_ns() -> str:
    return str(time.time_ns())[-10:]


def _observation_dict(obs: Observation) -> dict[str, Any]:
    width, height = obs.screenshot_size
    device_width, device_height = obs.device_size
    return {
        "ok": True, "observation_id": obs.observation_id, "captured_at": obs.captured_at,
        "screenshot": obs.screenshot_path, "screenshot_size": {"width": width, "height": height},
        "device_size": {"width": device_width, "height": device_height},
        "scale": {"x": device_width / width if width else 1, "y": device_height / height if height else 1},
        "active_window": obs.active_window, "ocr_text": obs.ocr_text,
        "ocr_status": obs.ocr_status,
    }


def _freshness_error(observation_id: str) -> str:
    obs = _LAST_OBSERVATION
    if obs is None:
        return "No screenshot observation yet. Call desktop_observe first; no action was taken."
    if str(observation_id or "") != obs.observation_id:
        return "Screenshot reference is stale or mismatched. Call desktop_observe and use its latest observation_id."
    if time.time() - obs.captured_at > OBSERVATION_TTL_S:
        return "Screenshot is too old for a safe action. Call desktop_observe again; no action was taken."
    return ""


def _coordinate_to_device(x: int, y: int, screenshot_size: tuple[int, int],
                          device_size: tuple[int, int]) -> tuple[int, int]:
    screenshot_width, screenshot_height = screenshot_size
    device_width, device_height = device_size
    if screenshot_width <= 0 or screenshot_height <= 0:
        raise ValueError("invalid screenshot size")
    return (
        round(int(x) * device_width / screenshot_width),
        round(int(y) * device_height / screenshot_height),
    )


def _is_destructive(summary: str, forced: bool = False) -> bool:
    if forced:
        return True
    normalized = str(summary or "").lower()
    return bool(re.search(
        r"\b(delete|remove|erase|trash|empty|wipe|format|send|submit|purchase|buy|pay|transfer|"
        r"shutdown|restart|close|quit|uninstall|install|confirm|apply|publish|post|save changes)\b",
        normalized,
    ))


async def _request_desktop_confirmation(operation: str, summary: str) -> tuple[bool, str]:
    request = await request_human_approval(
        f"desktop {operation} action={summary[:160]}", str(WORKSPACE_DIR),
        surface=current_surface(), reason=f"Destructive desktop interaction: {summary[:160]}",
    )
    return request.allowed, request.reason


async def _request_destructive_approval(x: int, y: int, summary: str) -> tuple[bool, str]:
    return await _request_desktop_confirmation(f"click x={x} y={y}", summary)


async def _verify_input_action(before: Observation, *, action: str, expected_text: str = "",
                               allow_retry: bool = False, x: int = 0, y: int = 0,
                               button: str = "left", first_sent: bool = True) -> dict[str, Any]:
    try:
        after = await _observe_internal()
    except Exception as exc:
        return {"ok": False, "verified": False,
                "message": f"{action} {'sent' if first_sent else 'failed'}, but screenshot verification failed: {type(exc).__name__}"}
    if after.image_hash != before.image_hash:
        return {"ok": first_sent, "verified": first_sent,
                "message": f"{action} {'sent' if first_sent else 'backend reported failure'}; visible desktop state changed.",
                "observation_id": after.observation_id}
    if expected_text and expected_text.casefold() in after.ocr_text.casefold():
        return {"ok": first_sent, "verified": first_sent, "message": f"{action} verified in OCR text.",
                "observation_id": after.observation_id}
    if allow_retry:
        try:
            retry_reference = await _observe_internal()
            if retry_reference.image_hash == after.image_hash:
                result = await _call_backend("click", x, y, button)
                if result is False:
                    return {"ok": False, "verified": False, "message": "Click retry failed; no further retry."
                            , "observation_id": retry_reference.observation_id}
                retried = await _observe_internal()
                if retried.image_hash != retry_reference.image_hash:
                    return {"ok": True, "verified": True, "retried": True,
                            "message": f"{action} verified after one retry; visible state changed.",
                            "observation_id": retried.observation_id}
                return {"ok": True, "verified": False, "retried": True,
                        "message": f"{action} was sent twice, but no visible change confirmed it.",
                        "observation_id": retried.observation_id}
        except Exception as exc:
            return {"ok": False, "verified": False,
                    "message": f"{action} retry failed ({type(exc).__name__}); no further retry."}
    if not first_sent:
        return {"ok": False, "verified": False,
                "message": f"{action} backend reported failure; no visible change confirmed it.",
                "observation_id": after.observation_id}
    return {"ok": True, "verified": False,
            "message": f"{action} was sent, but the screenshot shows no visible change; not confirmed.",
            "observation_id": after.observation_id}


async def _native_screenshot():
    from .system_tools import take_screenshot

    name = f"desktop_native_{uuid.uuid4().hex[:8]}"
    result = str(await take_screenshot.ainvoke({"name": name}))
    match = re.search(r"Screenshot saved:\s*(.+?\.png)(?:\s|$)", result)
    if not match:
        raise DesktopUnavailable(result[:220] or "No display/screenshot backend available")
    return _open_image(match.group(1))


async def _run_native(*args: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=8)
        if proc.returncode != 0:
            raise DesktopUnavailable(stderr.decode(errors="ignore")[:160] or f"{args[0]} exited {proc.returncode}")
        return True
    except FileNotFoundError as exc:
        raise DesktopUnavailable(f"{args[0]} is not installed") from exc
    except asyncio.TimeoutError as exc:
        raise DesktopUnavailable(f"{args[0]} timed out") from exc


def _native_list_windows_sync() -> list[str]:
    if platform.system() == "Linux" and shutil.which("wmctrl"):
        out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, timeout=4)
        return [line.split(None, 3)[-1] for line in out.stdout.splitlines() if line.strip()][:60]
    return []


async def _native_list_windows() -> str:
    from .system_tools import list_windows

    return str(await list_windows.ainvoke({}))


def _native_focus_window_sync(title: str) -> bool:
    if platform.system() == "Linux" and shutil.which("wmctrl"):
        result = subprocess.run(["wmctrl", "-a", str(title)], capture_output=True, timeout=4)
        return result.returncode == 0
    return False


async def _native_focus_window(title: str) -> bool:
    from .system_tools import focus_window

    return bool(await focus_window(str(title)))


def _native_clipboard_read() -> str:
    system = platform.system()
    commands = (["powershell", "-NoProfile", "-Command", "Get-Clipboard"] if system == "Windows" else
                ["pbpaste"] if system == "Darwin" else
                ["wl-paste", "--no-newline"] if shutil.which("wl-paste") else
                ["xclip", "-selection", "clipboard", "-o"] if shutil.which("xclip") else
                ["xsel", "--clipboard", "--output"] if shutil.which("xsel") else [])
    if not commands:
        raise DesktopUnavailable("clipboard read unavailable; install pyperclip or a native clipboard helper")
    try:
        out = subprocess.run(commands, capture_output=True, timeout=5)
        if out.returncode != 0:
            raise DesktopUnavailable("clipboard read failed")
        return out.stdout.decode(errors="replace")[:20000]
    except Exception as exc:
        raise DesktopUnavailable(f"clipboard read failed ({type(exc).__name__})") from exc


def _native_clipboard_write(text: str) -> bool:
    system = platform.system()
    commands = (["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"] if system == "Windows" else
                ["pbcopy"] if system == "Darwin" else
                ["wl-copy"] if shutil.which("wl-copy") else
                ["xclip", "-selection", "clipboard"] if shutil.which("xclip") else
                ["xsel", "--clipboard", "--input"] if shutil.which("xsel") else [])
    if not commands:
        raise DesktopUnavailable("clipboard write unavailable; install pyperclip or a native clipboard helper")
    try:
        result = subprocess.run(commands, input=str(text).encode(), capture_output=True, timeout=5)
        if result.returncode != 0:
            raise DesktopUnavailable("clipboard write failed")
        return True
    except Exception as exc:
        raise DesktopUnavailable(f"clipboard write failed ({type(exc).__name__})") from exc


def desktop_status() -> dict[str, Any]:
    """Report installed backend and likely headless status without making a live screenshot."""
    if not DESKTOP_ENABLED:
        return {"available": False, "backend": "disabled", "headless": False,
                "missing": ["DESKTOP_ENABLED=1"], "detail": "Set DESKTOP_ENABLED=1 to enable desktop tools."}
    backend = _backend()
    system = platform.system()
    headless = system == "Linux" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if isinstance(backend, _PyAutoGUIBackend):
        name = "pyautogui"
    elif isinstance(backend, _PynputBackend):
        name = "pynput + native screenshot"
    elif isinstance(backend, _DisabledBackend):
        name = "disabled"
    else:
        name = "platform-native fallback"
    missing: list[str] = []
    if headless:
        missing.append("attach a graphical desktop session (DISPLAY or WAYLAND_DISPLAY)")
    if system == "Linux":
        if not (isinstance(backend, (_PyAutoGUIBackend, _PynputBackend)) or shutil.which("xdotool")):
            missing.append("mouse/keyboard input requires pyautogui, pynput, or xdotool")
        if not (isinstance(backend, (_PyAutoGUIBackend, _PynputBackend))
                or any(shutil.which(name) for name in ("gnome-screenshot", "scrot", "import"))):
            missing.append("screenshot requires pyautogui/pynput, gnome-screenshot, scrot, or ImageMagick import")
    elif system == "Darwin" and not (isinstance(backend, (_PyAutoGUIBackend, _PynputBackend))
                                      or shutil.which("cliclick")):
        missing.append("mouse input requires pyautogui, pynput, or cliclick")
    missing.append("OCR requires pytesseract plus the Tesseract executable" if not shutil.which("tesseract")
                   else "OCR Python package pytesseract is optional")
    available = not headless and not any("requires" in item for item in missing)
    detail = ("; ".join(missing) if not available else
              "Input/screenshot backend selected; live display was not probed; OCR may need Tesseract.")
    return {"available": available, "backend": name, "headless": headless,
            "missing": missing, "detail": detail}


@tool
async def desktop_observe() -> str:
    """Capture a fresh screenshot, active window and OCR text before choosing any click coordinates."""
    try:
        observation = await _observe_internal()
        return json.dumps(_observation_dict(observation), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)[:240], "headless": desktop_status()["headless"]},
                          ensure_ascii=False)


@tool
async def desktop_ocr() -> str:
    """Take a fresh screenshot and return on-screen text via OCR when available."""
    try:
        observation = await _observe_internal()
        return json.dumps({"ok": bool(observation.ocr_text), "observation_id": observation.observation_id,
                           "text": observation.ocr_text, "ocr_status": observation.ocr_status}, ensure_ascii=False)
    except Exception as exc:
        return f"Desktop OCR unavailable: {str(exc)[:200]}"


@tool
async def desktop_click(x: int, y: int, observation_id: str, button: str = "left",
                        action_summary: str = "click", destructive: bool = False) -> str:
    """Click screenshot-pixel coordinates from the latest desktop_observe result; acts, verifies, retries once if safe."""
    if not DESKTOP_MOUSE_ENABLED:
        return json.dumps({"ok": False, "error": "Mouse input disabled hai (`DESKTOP_MOUSE_ENABLED=false`)."})
    global _LAST_OBSERVATION
    reference = _LAST_OBSERVATION
    stale = _freshness_error(observation_id)
    if stale:
        return json.dumps({"ok": False, "verified": False, "error": stale}, ensure_ascii=False)
    if button not in {"left", "middle", "right"}:
        return json.dumps({"ok": False, "error": "button must be left, middle, or right"})
    if not (0 <= int(x) < reference.screenshot_size[0] and 0 <= int(y) < reference.screenshot_size[1]):
        return json.dumps({"ok": False, "error": "coordinates are outside the latest screenshot"})
    try:
        fresh = await _observe_internal()
        if fresh.image_hash != reference.image_hash:
            return json.dumps({"ok": False, "verified": False,
                               "error": "Screen changed since observation. No click; inspect the latest screenshot and choose coordinates again.",
                               "observation_id": fresh.observation_id}, ensure_ascii=False)
        risky = _is_destructive(action_summary, destructive)
        if risky:
            approved, reason = await _request_destructive_approval(x, y, action_summary)
            if not approved:
                return json.dumps({"ok": False, "verified": False,
                                   "error": f"Destructive click cancelled; nothing clicked ({reason})."}, ensure_ascii=False)
            after_approval = await _observe_internal()
            if after_approval.image_hash != fresh.image_hash:
                return json.dumps({"ok": False, "verified": False,
                                   "error": "Screen changed while approval was pending. No click; observe again before requesting approval.",
                                   "observation_id": after_approval.observation_id}, ensure_ascii=False)
            fresh = after_approval
        device_x, device_y = _coordinate_to_device(x, y, fresh.screenshot_size, fresh.device_size)
        action_error = ""
        try:
            sent = await _call_backend("click", device_x, device_y, button)
        except Exception as exc:
            sent = False
            action_error = type(exc).__name__
        result = await _verify_input_action(
            fresh, action="Click", allow_retry=not risky, x=device_x, y=device_y,
            button=button, first_sent=sent is not False,
        )
        if action_error:
            result["backend_error"] = action_error
        result["coordinate_space"] = "screenshot pixels"
        result["device_coordinates"] = {"x": device_x, "y": device_y}
        result["destructive_confirmation"] = "approved" if risky else "not required"
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "verified": False,
                           "error": f"Desktop click failed ({type(exc).__name__}): {str(exc)[:160]}"}, ensure_ascii=False)


@tool
async def desktop_move(x: int, y: int, observation_id: str) -> str:
    """Move the pointer to screenshot-pixel coordinates from the latest observation."""
    if not DESKTOP_MOUSE_ENABLED:
        return json.dumps({"ok": False, "error": "Mouse input disabled hai (`DESKTOP_MOUSE_ENABLED=false`)."})
    stale = _freshness_error(observation_id)
    if stale:
        return json.dumps({"ok": False, "error": stale})
    reference = _LAST_OBSERVATION
    try:
        fresh = await _observe_internal()
        if fresh.image_hash != reference.image_hash:
            return json.dumps({"ok": False, "error": "Screen changed; observe again before moving.",
                               "observation_id": fresh.observation_id})
        device_x, device_y = _coordinate_to_device(x, y, fresh.screenshot_size, fresh.device_size)
        await _call_backend("move", device_x, device_y)
        return json.dumps({"ok": True, "verified": True, "device_coordinates": {"x": device_x, "y": device_y}})
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"Pointer move unavailable ({type(exc).__name__})"})


@tool
async def desktop_scroll(amount: int, x: int, y: int, observation_id: str) -> str:
    """Scroll at screenshot-pixel coordinates derived from the latest desktop_observe result."""
    if not DESKTOP_MOUSE_ENABLED:
        return json.dumps({"ok": False, "error": "Mouse input disabled hai (`DESKTOP_MOUSE_ENABLED=false`)."})
    stale = _freshness_error(observation_id)
    if stale:
        return json.dumps({"ok": False, "error": stale})
    reference = _LAST_OBSERVATION
    try:
        fresh = await _observe_internal()
        if fresh.image_hash != reference.image_hash:
            return json.dumps({"ok": False, "error": "Screen changed; observe again before scrolling.",
                               "observation_id": fresh.observation_id})
        device_x, device_y = _coordinate_to_device(x, y, fresh.screenshot_size, fresh.device_size)
        await _call_backend("scroll", int(amount), device_x, device_y)
        after = await _observe_internal()
        return json.dumps({"ok": True, "verified": after.image_hash != fresh.image_hash,
                           "observation_id": after.observation_id,
                           "message": "Scroll state changed." if after.image_hash != fresh.image_hash
                           else "Scroll sent; visible change not confirmed."}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"Scroll unavailable ({type(exc).__name__}): {str(exc)[:120]}"})


@tool
async def desktop_type(text: str, observation_id: str, action_summary: str = "type text",
                       press_enter: bool = False) -> str:
    """Type into the focused window only after a fresh observation; submits require human confirmation."""
    if not DESKTOP_KEYBOARD_ENABLED:
        return json.dumps({"ok": False, "error": "Keyboard input disabled hai (`DESKTOP_KEYBOARD_ENABLED=false`)."})
    stale = _freshness_error(observation_id)
    if stale:
        return json.dumps({"ok": False, "verified": False, "error": stale}, ensure_ascii=False)
    reference = _LAST_OBSERVATION
    try:
        before = await _observe_internal()
        if reference is not None and before.image_hash != reference.image_hash:
            return json.dumps({"ok": False, "verified": False,
                               "error": "Screen changed; observe again before typing.",
                               "observation_id": before.observation_id})
        risky = press_enter or _is_destructive(action_summary)
        if risky:
            approved, reason = await _request_desktop_confirmation(
                f"type {len(str(text))} characters" + (" and press Enter" if press_enter else ""),
                action_summary or "submit typed text",
            )
            if not approved:
                return json.dumps({"ok": False, "verified": False,
                                   "error": f"Typing/submission cancelled; no text sent ({reason})."})
            after_approval = await _observe_internal()
            if after_approval.image_hash != before.image_hash:
                return json.dumps({"ok": False, "verified": False,
                                   "error": "Screen changed while approval was pending; observe again."})
            before = after_approval
        sent = await _call_backend("type_text", str(text))
        if sent is False:
            return json.dumps({"ok": False, "verified": False, "error": "Keyboard backend refused text input."})
        if press_enter:
            await _call_backend("press", ["enter"])
        after = await _observe_internal()
        visible = bool(str(text).strip() and str(text).strip().casefold() in after.ocr_text.casefold())
        changed = after.image_hash != before.image_hash
        return json.dumps({"ok": True, "verified": visible or changed,
                           "message": "Text visible after input." if visible else
                           "(" + ("submit " if press_enter else "") + "input sent; visible change "
                           + ("observed" if changed else "not confirmed") + ")",
                           "observation_id": after.observation_id,
                           "characters": len(str(text))}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "verified": False,
                           "error": f"Keyboard input unavailable ({type(exc).__name__}): {str(exc)[:160]}"})


@tool
async def desktop_press(keys: str, observation_id: str, action_summary: str = "") -> str:
    """Press a shortcut after fresh observation; closing/submitting shortcuts ask the human first."""
    if not DESKTOP_KEYBOARD_ENABLED:
        return json.dumps({"ok": False, "error": "Keyboard input disabled hai (`DESKTOP_KEYBOARD_ENABLED=false`)."})
    stale = _freshness_error(observation_id)
    if stale:
        return json.dumps({"ok": False, "verified": False, "error": stale})
    parsed = [part.strip().lower() for part in str(keys).split("+") if part.strip()]
    if not parsed:
        return json.dumps({"ok": False, "error": "keys is empty"})
    summary = action_summary or f"keyboard shortcut {'+'.join(parsed)}"
    destructive_hotkey = any(key in {"delete", "backspace", "enter"} for key in parsed) or (
        "alt" in parsed and "f4" in parsed
    ) or ("ctrl" in parsed and "w" in parsed)
    risky = _is_destructive(summary, destructive_hotkey)
    reference = _LAST_OBSERVATION
    try:
        before = await _observe_internal()
        if reference is not None and before.image_hash != reference.image_hash:
            return json.dumps({"ok": False, "verified": False,
                               "error": "Screen changed; observe again before pressing keys.",
                               "observation_id": before.observation_id})
        if risky:
            approved, reason = await _request_desktop_confirmation(f"press {'+'.join(parsed)}", summary)
            if not approved:
                return json.dumps({"ok": False, "verified": False,
                                   "error": f"Shortcut cancelled; nothing pressed ({reason})."})
            after_approval = await _observe_internal()
            if after_approval.image_hash != before.image_hash:
                return json.dumps({"ok": False, "verified": False,
                                   "error": "Screen changed while approval was pending; observe again."})
            before = after_approval
        result = await _call_backend("press", parsed)
        if result is False:
            return json.dumps({"ok": False, "verified": False, "error": "Keyboard backend refused shortcut."})
        verified = await _verify_input_action(before, action="Shortcut", allow_retry=False)
        verified["shortcut"] = "+".join(parsed)
        return json.dumps(verified, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "verified": False,
                           "error": f"Shortcut failed ({type(exc).__name__}): {str(exc)[:140]}"})


async def _locate_text(image: Any, text: str, exact: bool = False) -> dict[str, Any] | None:
    backend = _backend()
    locator = getattr(backend, "locate_text", None)
    if callable(locator):
        try:
            try:
                result = locator(image, text, exact)
            except TypeError:
                try:
                    result = locator(text, image)
                except TypeError:
                    result = locator(text)
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, dict) and "x" in result and "y" in result:
                return {"x": int(result["x"]), "y": int(result["y"]),
                        "bbox": result.get("bbox"), "source": "backend"}
            if isinstance(result, (tuple, list)) and len(result) >= 2:
                return {"x": int(result[0]), "y": int(result[1]), "source": "backend"}
        except Exception:
            return None
    try:
        import pytesseract
        from pytesseract import Output

        data = pytesseract.image_to_data(image, output_type=Output.DICT)
        groups: dict[tuple[int, int, int], list[int]] = {}
        for index, token in enumerate(data.get("text", [])):
            if str(token).strip():
                key = (int(data["block_num"][index]), int(data["par_num"][index]), int(data["line_num"][index]))
                groups.setdefault(key, []).append(index)
        needle = " ".join(text.casefold().split())
        for indices in groups.values():
            phrase = " ".join(str(data["text"][i]).strip() for i in indices).casefold()
            matches = phrase == needle if exact else needle in phrase
            if matches:
                left = min(int(data["left"][i]) for i in indices)
                top = min(int(data["top"][i]) for i in indices)
                right = max(int(data["left"][i]) + int(data["width"][i]) for i in indices)
                bottom = max(int(data["top"][i]) + int(data["height"][i]) for i in indices)
                return {"x": (left + right) // 2, "y": (top + bottom) // 2,
                        "bbox": {"left": left, "top": top, "right": right, "bottom": bottom},
                        "source": "tesseract"}
    except Exception:
        return None
    return None


@tool
async def click_text(text: str, observation_id: str, exact: bool = False,
                     action_summary: str = "click visible text") -> str:
    """Locate visible OCR text and click its center after a fresh-screen check and normal approval gate."""
    stale = _freshness_error(observation_id)
    if stale:
        return json.dumps({"ok": False, "verified": False, "error": stale})
    reference = _LAST_OBSERVATION
    try:
        fresh = await _observe_internal()
        if reference is None or fresh.image_hash != reference.image_hash:
            return json.dumps({"ok": False, "verified": False,
                               "error": "Screen changed; inspect the latest screenshot before clicking text.",
                               "observation_id": fresh.observation_id})
        locator = await _locate_text(_open_image(fresh.screenshot_path), str(text), exact=exact)
        if not locator:
            return json.dumps({"ok": False, "verified": False,
                               "error": f"Visible text {text!r} could not be located; no click was sent.",
                               "observation_id": fresh.observation_id})
        result = json.loads(await desktop_click.ainvoke({
            "x": locator["x"], "y": locator["y"], "observation_id": fresh.observation_id,
            "action_summary": action_summary,
        }))
        result["matched_text"] = text
        result["locator"] = locator
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "verified": False,
                           "error": f"Text click unavailable ({type(exc).__name__}): {str(exc)[:160]}"})


@tool
async def wait_for_text(text: str, timeout: float = 5.0, interval: float = 0.25,
                        exact: bool = False) -> str:
    """Poll fresh screenshots until OCR text appears or a bounded timeout expires."""
    deadline = time.monotonic() + min(max(float(timeout), 0.0), 30.0)
    pause = min(max(float(interval), 0.05), 2.0)
    needle = " ".join(str(text).casefold().split())
    last: Observation | None = None
    while True:
        try:
            last = await _observe_internal()
        except Exception as exc:
            return json.dumps({"ok": False, "found": False,
                               "error": f"Screen observation unavailable ({type(exc).__name__})"})
        haystack = " ".join(last.ocr_text.casefold().split())
        found = needle == haystack if exact else bool(needle and needle in haystack)
        if found:
            return json.dumps({"ok": True, "found": True, "verified": True,
                               "text": text, "observation_id": last.observation_id,
                               "active_window": last.active_window})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return json.dumps({"ok": True, "found": False, "verified": False,
                               "text": text, "observation_id": last.observation_id,
                               "ocr_status": last.ocr_status,
                               "message": "Text did not appear before the bounded timeout."})
        await asyncio.sleep(min(pause, remaining))


def _normalize_image_match(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict) and ("x" in value or "left" in value):
        left = int(value.get("left", value.get("x", 0)))
        top = int(value.get("top", value.get("y", 0)))
        width = int(value.get("width", 0))
        height = int(value.get("height", 0))
        if "x" in value and "left" not in value and not width:
            return {"x": left, "y": top, "score": float(value.get("score", 1.0)),
                    "source": value.get("source", "backend")}
        return {"x": left + width // 2, "y": top + height // 2,
                "bbox": {"left": left, "top": top, "width": width, "height": height},
                "score": float(value.get("score", 1.0)), "source": value.get("source", "backend")}
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return {"x": int(value[0]), "y": int(value[1]), "score": 1.0, "source": "backend"}
    return None


async def _find_image_on_screen(image: Any, template_path: str, threshold: float) -> dict[str, Any] | None:
    backend = _backend()
    finder = getattr(backend, "find_image", None)
    if callable(finder):
        try:
            try:
                result = finder(image, template_path, threshold)
            except TypeError:
                result = finder(template_path, image)
            if hasattr(result, "__await__"):
                result = await result
            match = _normalize_image_match(result)
            if match and float(match.get("score", 1.0)) >= threshold:
                return match
            return None
        except Exception:
            return None
    try:
        import cv2
        import numpy as np

        template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        screen = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        if template is None or template.size == 0 or template.shape[0] > screen.shape[0] or template.shape[1] > screen.shape[1]:
            return None
        scores = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(scores)
        if float(score) < threshold:
            return None
        height, width = template.shape[:2]
        left, top = location
        return {"x": left + width // 2, "y": top + height // 2,
                "bbox": {"left": left, "top": top, "width": width, "height": height},
                "score": float(score), "source": "opencv"}
    except Exception:
        return None


@tool
async def find_image(image_path: str, observation_id: str = "", threshold: float = 0.88) -> str:
    """Find a local reference image in the current screenshot; uses OpenCV or a fake/backend locator."""
    template = Path(image_path).expanduser()
    if not template.is_file():
        return json.dumps({"ok": False, "found": False, "error": "reference image file not found"})
    if observation_id:
        stale = _freshness_error(observation_id)
        if stale:
            return json.dumps({"ok": False, "found": False, "error": stale})
    try:
        observation = await _observe_internal()
        match = await _find_image_on_screen(_open_image(observation.screenshot_path), str(template),
                                            min(max(float(threshold), 0.0), 1.0))
        if not match:
            return json.dumps({"ok": True, "found": False, "verified": False,
                               "observation_id": observation.observation_id,
                               "message": "Image not found at the requested confidence."})
        return json.dumps({"ok": True, "found": True, "verified": True,
                           "observation_id": observation.observation_id, **match}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "found": False,
                           "error": f"Image search unavailable ({type(exc).__name__}): {str(exc)[:140]}"})


@tool
async def describe_screen(question: str = "Describe the current screen and visible UI controls briefly.") -> str:
    """Describe a fresh screenshot with the configured vision role; returns a clear offline message otherwise."""
    try:
        observation = await _observe_internal()
    except Exception as exc:
        return json.dumps({"ok": False, "status": "unavailable",
                           "message": f"Screen capture unavailable ({type(exc).__name__}); no description generated."})
    try:
        from langchain_core.messages import HumanMessage

        from ..llm import get_chat_model, resolve_role

        route = resolve_role("vision", allow_discovery=False)
        if not getattr(route, "targets", None):
            return json.dumps({"ok": False, "status": "unavailable",
                               "message": "No vision-role model is configured; screenshot was not sent to a text-only model.",
                               "observation_id": observation.observation_id})
        import base64

        payload = base64.b64encode(Path(observation.screenshot_path).read_bytes()).decode("ascii")
        message = HumanMessage(content=[
            {"type": "text", "text": str(question)[:600]},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}"}},
        ])
        response = await get_chat_model("vision").ainvoke([message])
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = " ".join(str(part.get("text", "")) if isinstance(part, dict) else str(part)
                                for part in content)
        description = str(content or "").strip()
        if not description:
            raise ValueError("vision model returned an empty description")
        return json.dumps({"ok": True, "status": "described", "description": description[:4000],
                           "observation_id": observation.observation_id}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "status": "unavailable",
                           "message": f"Vision description unavailable ({type(exc).__name__}); screen was not described.",
                           "observation_id": observation.observation_id}, ensure_ascii=False)


@tool
async def desktop_drag(x1: int, y1: int, x2: int, y2: int, observation_id: str, button: str = "left",
                       action_summary: str = "drag") -> str:
    """Drag from screenshot-space coordinates only after freshness check; verify the resulting screen."""
    if not DESKTOP_MOUSE_ENABLED:
        return json.dumps({"ok": False, "error": "Mouse input disabled hai (`DESKTOP_MOUSE_ENABLED=false`)."})
    stale = _freshness_error(observation_id)
    if stale:
        return json.dumps({"ok": False, "verified": False, "error": stale})
    if button not in {"left", "middle", "right"}:
        return json.dumps({"ok": False, "verified": False, "error": "button must be left, middle, or right"})
    reference = _LAST_OBSERVATION
    if reference is None:
        return json.dumps({"ok": False, "verified": False, "error": "Call desktop_observe first."})
    for px, py in ((x1, y1), (x2, y2)):
        if not (0 <= int(px) < reference.screenshot_size[0] and 0 <= int(py) < reference.screenshot_size[1]):
            return json.dumps({"ok": False, "verified": False, "error": "drag coordinates outside screenshot"})
    risky = _is_destructive(action_summary)
    try:
        before = await _observe_internal()
        if before.image_hash != reference.image_hash:
            return json.dumps({"ok": False, "verified": False, "error": "Screen changed; observe and re-plan the drag."})
        if risky:
            approved, reason = await _request_desktop_confirmation("drag", action_summary)
            if not approved:
                return json.dumps({"ok": False, "verified": False,
                                   "error": f"Drag cancelled; no action taken ({reason})."})
            approved_screen = await _observe_internal()
            if approved_screen.image_hash != before.image_hash:
                return json.dumps({"ok": False, "verified": False,
                                   "error": "Screen changed during approval; drag not sent."})
            before = approved_screen
        device1 = _coordinate_to_device(x1, y1, before.screenshot_size, before.device_size)
        device2 = _coordinate_to_device(x2, y2, before.screenshot_size, before.device_size)
        sent = await _call_backend("drag", *device1, *device2, button)
        if sent is False:
            return json.dumps({"ok": False, "verified": False, "error": "Drag backend refused the action."})
        after = await _observe_internal()
        changed = after.image_hash != before.image_hash
        return json.dumps({"ok": True, "verified": changed,
                           "message": "Drag sent and visible state changed." if changed else
                           "Drag was sent; resulting UI change is not confirmed.",
                           "observation_id": after.observation_id,
                           "from": {"x": device1[0], "y": device1[1]},
                           "to": {"x": device2[0], "y": device2[1]},
                           "approval": "approved" if risky else "not required"}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "verified": False,
                           "error": f"Drag unavailable ({type(exc).__name__}): {str(exc)[:140]}"})


async def _key_action(key: str, observation_id: str, down: bool) -> dict[str, Any]:
    if not DESKTOP_KEYBOARD_ENABLED:
        return {"ok": False, "verified": False,
                "error": "Keyboard input disabled hai (`DESKTOP_KEYBOARD_ENABLED=false`)."}
    stale = _freshness_error(observation_id)
    if stale:
        return {"ok": False, "verified": False, "error": stale}
    reference = _LAST_OBSERVATION
    try:
        before = await _observe_internal()
        if reference is None or before.image_hash != reference.image_hash:
            return {"ok": False, "verified": False, "error": "Screen changed; observe again before key input."}
        await _call_backend("key_down" if down else "key_up", str(key))
        after = await _observe_internal()
        return {"ok": True, "verified": True, "key": key, "pressed": down,
                "observation_id": after.observation_id,
                "visible_change": after.image_hash != before.image_hash}
    except Exception as exc:
        return {"ok": False, "verified": False,
                "error": f"Key {'down' if down else 'up'} unavailable ({type(exc).__name__}): {str(exc)[:120]}"}


@tool
async def key_down(key: str, observation_id: str) -> str:
    """Press and hold one keyboard key after fresh-screen verification; pair with key_up."""
    return json.dumps(await _key_action(key, observation_id, True), ensure_ascii=False)


@tool
async def key_up(key: str, observation_id: str) -> str:
    """Release a previously held key after fresh-screen verification."""
    return json.dumps(await _key_action(key, observation_id, False), ensure_ascii=False)


@tool
async def hold(key: str, duration: float = 1.0, observation_id: str = "") -> str:
    """Hold one key for a bounded duration (0–10 seconds) and always attempt release."""
    if not DESKTOP_KEYBOARD_ENABLED:
        return json.dumps({"ok": False, "error": "Keyboard input disabled hai (`DESKTOP_KEYBOARD_ENABLED=false`)."})
    if not 0 <= float(duration) <= 10:
        return json.dumps({"ok": False, "verified": False, "error": "duration must be between 0 and 10 seconds"})
    stale = _freshness_error(observation_id)
    if stale:
        return json.dumps({"ok": False, "verified": False, "error": stale})
    reference = _LAST_OBSERVATION
    down = False
    try:
        before = await _observe_internal()
        if reference is None or before.image_hash != reference.image_hash:
            return json.dumps({"ok": False, "verified": False, "error": "Screen changed; observe again before holding a key."})
        await _call_backend("key_down", str(key))
        down = True
        await asyncio.sleep(float(duration))
        await _call_backend("key_up", str(key))
        down = False
        after = await _observe_internal()
        return json.dumps({"ok": True, "verified": True, "key": key, "duration": float(duration),
                           "observation_id": after.observation_id,
                           "visible_change": after.image_hash != before.image_hash}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "verified": False,
                           "error": f"Key hold unavailable ({type(exc).__name__}): {str(exc)[:120]}"})
    finally:
        if down:
            try:
                await _call_backend("key_up", str(key))
            except Exception:
                pass


@tool
async def desktop_windows() -> str:
    """List visible application windows with friendly headless errors."""
    try:
        windows = await _call_backend("list_windows")
        if isinstance(windows, str):
            return windows[:5000]
        return json.dumps({"ok": True, "windows": list(windows or [])[:60]}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"Window enumeration unavailable ({type(exc).__name__})"})


@tool
async def desktop_focus(window_title: str) -> str:
    """Focus an existing window by title; reports false when there is no match."""
    try:
        focused = bool(await _call_backend("focus_window", str(window_title)))
        return json.dumps({"ok": focused, "window": window_title,
                           "message": "Window focused." if focused else "No matching focusable window; nothing launched."})
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"Window focus unavailable ({type(exc).__name__})"})


@tool
async def desktop_clipboard_read() -> str:
    """Read clipboard text (only use when the owner explicitly asks to inspect it)."""
    try:
        value = await _call_backend("clipboard_read")
        return json.dumps({"ok": True, "text": str(value or "")[:20000]}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"Clipboard read unavailable ({type(exc).__name__})"})


@tool
async def desktop_clipboard_write(text: str) -> str:
    """Write plain text to the desktop clipboard."""
    if not DESKTOP_KEYBOARD_ENABLED:
        return json.dumps({"ok": False, "error": "Keyboard input disabled hai (`DESKTOP_KEYBOARD_ENABLED=false`)."})
    try:
        ok = await _call_backend("clipboard_write", str(text))
        return json.dumps({"ok": bool(ok), "characters": len(str(text)),
                           "message": "Clipboard updated." if ok else "Clipboard update was not confirmed."})
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"Clipboard write unavailable ({type(exc).__name__})"})


DESKTOP_CONTROL_TOOLS = [
    desktop_observe, desktop_ocr, desktop_click, desktop_move, desktop_scroll, desktop_drag,
    click_text, wait_for_text, find_image, describe_screen, key_down, key_up, hold,
    desktop_type, desktop_press, desktop_windows, desktop_focus,
    desktop_clipboard_read, desktop_clipboard_write,
]
