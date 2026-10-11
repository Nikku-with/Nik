"""Pro CLI dashboard — live "coder-style" terminal UI.

User ki demand: "CLI mode ka UI ekdam professional coder wala banao — bahut saare buttons
and valid tools features ke saath."

Isme kya hai:
    * `Dashboard` — Rich Live dashboard: header bar (brain/model/mode/latency), live
      thinking→plan→tool stream, side panel (plan steps + tools used), footer hotkeys.
    * `key_menu()` — keyboard se chalne wala menu (↑/↓ + Enter, ya seedha hotkey letter).
      Terminal me yehi "buttons" hain. Windows (msvcrt) + Linux/macOS (termios) dono.
    * `TOOL_MENU` / palette — tools browse karo aur seedha CLI se run karo.
    * TTY na ho (pipe/pytest) to sab plain-text fallback — kuch nahi tootta.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Callable

from ..runtime.progress import ProgressEvent

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    HAS_RICH = True
except Exception:  # pragma: no cover
    HAS_RICH = False
    Console = Group = Live = Panel = Table = Text = None  # type: ignore

ACCENT = "bright_cyan"
OK = "green"
WARN = "yellow"
BAD = "red"
DIM = "grey62"

KIND_STYLE = {
    "start": ACCENT, "thinking": "bright_blue", "plan": "magenta", "tool_start": ACCENT,
    "tool_end": OK, "verify": "bright_magenta", "warn": WARN, "error": BAD, "done": OK,
}
KIND_ICON = {
    "start": "🚀", "thinking": "🧠", "plan": "📋", "tool_start": "🛠️", "tool_end": "✅",
    "verify": "🔎", "warn": "⚠️", "error": "❌", "done": "🏁",
}


def is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# live dashboard
# ---------------------------------------------------------------------------
class Dashboard:
    """Ek request ke dauraan live render hone wala dashboard."""

    def __init__(self, header: dict[str, Any] | None = None, max_events: int = 12,
                 show_reasoning: bool = True):
        self.header = header or {}
        self.events: list[ProgressEvent] = []
        self.max_events = max_events
        self.show_reasoning = show_reasoning
        self.plan: list[str] = []
        self.tools_used: list[tuple[str, str, int]] = []   # (tool, status, ms)
        self.current = "shuru kar raha hoon…"
        self.started = 0.0
        self.answer_text: str = ""
        self._console = Console() if HAS_RICH else None

    # ---- agent callback (sync) ----
    def on_event(self, ev: ProgressEvent) -> None:
        import time as _t

        if not self.started:
            self.started = _t.time()
        self.events.append(ev)
        if len(self.events) > 400:
            del self.events[:200]
        if ev.kind == "plan" and ev.detail:
            self.plan = [p for p in str(ev.detail).replace(" → ", "|").split("|") if p.strip()]
        if ev.kind == "tool_start":
            self.current = f"{ev.tool or 'tool'} chal raha hai…"
        elif ev.kind == "tool_end":
            self.tools_used.append((ev.tool or "tool", ev.status or "ok", ev.ms))
            self.current = "agla step…"
        elif ev.kind == "thinking":
            self.current = ev.text or "soch raha hoon…"
        elif ev.kind == "error":
            self.current = f"error: {ev.text[:70]}"
        elif ev.kind == "done":
            self.current = "kaam khatam"

    def feed_line(self, text: str, style: str = DIM) -> None:
        self.events.append(ProgressEvent(kind="thinking", text=text))
        self.current = text

    # ---- rendering ----
    def _header_bar(self) -> Any:
        t = Table.grid(expand=True)
        t.add_column(justify="left")
        t.add_column(justify="center")
        t.add_column(justify="right")
        import time as _t

        elapsed = f"{_t.time() - self.started:.1f}s" if self.started else "0.0s"
        left = Text.from_markup(f"[bold {ACCENT}]◆ {self.header.get('name', 'NIKHILAI')}[/] "
                                f"[{DIM}]v{self.header.get('version', '2.3')}[/]")
        mid = Text.from_markup(f"[{DIM}]brain[/] [bold]{self.header.get('model', '-')}[/]"
                               f" [{DIM}]•[/] mode [bold]{self.header.get('mode', 'text')}[/]"
                               f" [{DIM}]•[/] tools [bold]{self.header.get('tools', '?')}[/]"
                               f" [{DIM}]•[/] mem [bold]{self.header.get('memory', '?')}[/]")
        right = Text.from_markup(f"[{DIM}]{self.header.get('provider', '-')}[/] "
                                 f"[bold {OK if self.header.get('live') else WARN}]"
                                 f"{'LIVE' if self.header.get('live') else 'DEMO'}[/] [{DIM}]• {elapsed}[/]")
        t.add_row(left, mid, right)
        return Panel(t, border_style=ACCENT, padding=(0, 1))

    def _stream(self) -> Any:
        lines: list[Any] = []
        spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(__import__("time").time() * 10) % 10]
        lines.append(Text.from_markup(f"[bold {ACCENT}]{spin} {self.current}[/]"))
        lines.append(Text(""))
        for ev in self.events[-self.max_events:]:
            icon = KIND_ICON.get(ev.kind, "•")
            style = KIND_STYLE.get(ev.kind, DIM)
            text = (ev.text or ev.tool or "")[:96]
            ms = f" [{DIM}]{ev.ms}ms[/]" if ev.ms else ""
            lines.append(Text.from_markup(f"{icon} [{style}]{text}[/]{ms}"))
        if not self.events:
            lines.append(Text.from_markup(f"[{DIM}]…[/]"))
        return Panel(Group(*lines), title=f"[bold]⚡ Live activity[/]", border_style="blue",
                     padding=(0, 1))

    def _side(self) -> Any:
        t = Table.grid(expand=True)
        t.add_column()
        if self.plan:
            t.add_row(Text.from_markup(f"[bold]📋 Plan[/] [{DIM}]({len(self.plan)})[/]"))
            for i, step in enumerate(self.plan[:6], 1):
                t.add_row(Text.from_markup(f"[{DIM}]{i}.[/] {str(step)[:40]}"))
            t.add_row(Text(""))
        if self.tools_used:
            t.add_row(Text.from_markup(f"[bold]🛠️ Tools[/] [{DIM}]({len(self.tools_used)})[/]"))
            for name, status, ms in self.tools_used[-6:]:
                icon = "✅" if status != "fail" else "❌"
                t.add_row(Text.from_markup(f"{icon} {name[:26]} [{DIM}]{ms}ms[/]"))
        if not self.plan and not self.tools_used:
            t.add_row(Text.from_markup(f"[{DIM}]plan/tools yahan dikhenge[/]"))
        return Panel(t, title="[bold]🧭 Context[/]", border_style="magenta", padding=(0, 1))

    def render(self) -> Any:
        if not HAS_RICH:
            return Text(self.current)
        body = Table.grid(expand=True)
        body.add_column(ratio=3)
        body.add_column(ratio=2)
        body.add_row(self._stream(), self._side())
        return Group(self._header_bar(), body,
                     Text.from_markup(f"[{DIM}]Ctrl+C = cancel  •  /help = commands  •  "
                                      f"/menu = tool browser[/]"))

    def events_trace(self, limit: int = 10) -> list[str]:
        return [e.line() for e in self.events[-limit:]]


class live_run:
    """Context manager: `with live_run(dashboard) as run:` → agent chalao, live render."""

    def __init__(self, dashboard: Dashboard | None, enabled: bool = True, refresh: float = 8.0):
        self.dash = dashboard
        self.enabled = enabled and HAS_RICH and is_tty() and dashboard is not None
        self.refresh = refresh
        self._live = None

    def __enter__(self) -> "live_run":
        if self.enabled and self.dash is not None:
            self._live = Live(self.dash.render(), console=self.dash._console, refresh_per_second=self.refresh,
                              transient=False, vertical_overflow="visible")
            self._live.__enter__()
        return self

    def tick(self) -> None:
        if self._live is not None and self.dash is not None:
            try:
                self._live.update(self.dash.render())
            except Exception:
                pass

    def __exit__(self, *exc: Any) -> None:
        if self._live is not None:
            try:
                self._live.__exit__(*exc)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# keyboard menu ("buttons")
# ---------------------------------------------------------------------------
def _read_key() -> str:
    """Ek keypad press padho: up/down/left/right/enter/esc/char. Cross-platform."""
    try:
        if os.name == "nt":  # Windows PowerShell / cmd
            import msvcrt

            ch = msvcrt.getch()
            if ch in (b"\x00", b"\xe0"):
                return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(
                    msvcrt.getch().decode(errors="ignore"), "")
            if ch in (b"\r", b"\n"):
                return "enter"
            if ch == b"\x1b":
                return "esc"
            if ch == b"\x03":
                return "ctrl-c"
            return ch.decode(errors="ignore").lower()
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                nxt = sys.stdin.read(1)
                if nxt == "[":
                    return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(
                        sys.stdin.read(1), "esc")
                return "esc"
            if ch in ("\r", "\n"):
                return "enter"
            if ch == "\x03":
                return "ctrl-c"
            return ch.lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return "esc"


def key_menu(title: str, items: list[tuple[str, str, Callable]], subtitle: str = "") -> bool:
    """Keyboard menu — ↑/↓ se choose, Enter se run, hotkey letter se seedha run, Esc = wapas.

    items: (hotkey, label, fn). TTY na ho to False (caller fallback dikhaye).
    """
    if not (HAS_RICH and is_tty()):
        return False
    from rich.console import Console as _C

    console = _C()
    idx = 0
    while True:
        body = Table.grid(expand=True)
        body.add_column()
        for i, (hot, label, _fn) in enumerate(items):
            marker = f"[bold {ACCENT}]▶[/]" if i == idx else " "
            style = f"bold {ACCENT}" if i == idx else ""
            body.add_row(Text.from_markup(f" {marker} [{style}][{hot}] {label}[/]"))
        panel = Panel(body, title=f"[bold]{title}[/]", subtitle=subtitle or "[esc] wapas",
                      border_style=ACCENT, padding=(0, 1))
        console.print(panel)
        key = _read_key()
        if key in ("esc", "ctrl-c"):
            return True
        if key == "up":
            idx = (idx - 1) % len(items)
        elif key == "down":
            idx = (idx + 1) % len(items)
        elif key == "enter":
            _hot, label, fn = items[idx]
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                console.print(f"[{BAD}]⚠️ {label} fail: {type(exc).__name__}: {exc}[/]")
            return True
        else:
            for hot, label, fn in items:
                if key == hot.lower():
                    try:
                        fn()
                    except Exception as exc:  # noqa: BLE001
                        console.print(f"[{BAD}]⚠️ {label} fail: {exc}[/]")
                    return True


# ---------------------------------------------------------------------------
# tool browser + direct runner
# ---------------------------------------------------------------------------
def tool_groups() -> dict[str, list[str]]:
    """group → sorted tool names (silent fail nahi — warna UI khali dikhta hai)."""
    try:
        from ..tools import _TOOL_GROUPS

        def name_of(x):
            return getattr(x, "name", None) or str(x)

        return {k: sorted((name_of(x) for x in v)) for k, v in _TOOL_GROUPS.items()}
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("jarvis.cli").warning("tool_groups fail: %s", exc)
        return {}


async def run_tool_async(name: str, args: dict[str, Any]) -> str:
    from ..tools import ALL_TOOLS

    tool = next((t for t in ALL_TOOLS if getattr(t, "name", "") == name), None)
    if tool is None:
        return f"❌ tool '{name}' nahi mila"
    out = await tool.ainvoke(args)
    return str(out)


def tool_schema(name: str) -> dict[str, Any]:
    from ..tools import ALL_TOOLS

    tool = next((t for t in ALL_TOOLS if getattr(t, "name", "") == name), None)
    if tool is None or not getattr(tool, "args_schema", None):
        return {}
    try:
        return tool.args_schema.model_json_schema().get("properties", {})
    except Exception:
        return {}


def tools_table(group: str | None = None) -> Any:
    """Tools table — group filter ke saath."""
    groups = tool_groups()
    if not HAS_RICH:
        return Text("\n".join(f"{g}: {', '.join(names)}" for g, names in groups.items()))
    t = Table(title=f"🧰 Tools — {sum(len(v) for v in groups.values())} total",
              border_style=ACCENT, header_style=f"bold {ACCENT}", expand=True)
    t.add_column("group", style=ACCENT, no_wrap=True)
    t.add_column("count", justify="right", style=DIM, no_wrap=True)
    t.add_column("tools", overflow="fold")
    for g, names in sorted(groups.items()):
        if group and g != group:
            continue
        if not group and g == "all":       # 'all' = baaki sab ka union, dobara dikhane ka matlab nahi
            continue
        t.add_row(g, str(len(names)), ", ".join(names))
    return t
