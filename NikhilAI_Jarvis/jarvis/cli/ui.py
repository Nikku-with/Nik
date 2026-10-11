"""
Terminal UI — Rich panels, tables, spinners
==========================================

Ek hi jagah se poora look-and-feel: banner, status panel, activity log, tables.
Na noisy, na flat — bas itna ki owner ko saaf dikhe: brain kaun sa hai, kaun se tools on hain,
aur agent abhi kya kar raha hai.
"""
from __future__ import annotations

import time
from typing import Any

try:
    from rich.console import Console  # type: ignore
    from rich.live import Live  # type: ignore
    from rich.markdown import Markdown  # type: ignore
    from rich.panel import Panel  # type: ignore
    from rich.table import Table  # type: ignore
    from rich.text import Text  # type: ignore

    HAS_RICH = True
except Exception:  # rich na ho to plain fallback
    HAS_RICH = False

console = Console() if HAS_RICH else None

ACCENT = "bright_cyan"
OK = "green"
WARN = "yellow"
BAD = "red"
DIM = "grey62"

BANNER = r"""
  _   _ _ _    _         _    ___
 | \ | (_) | _| |__     / \  |_ _|
 |  \| | | |/ / '_ \   / _ \  | |
 | |\  | |   <| | | | / ___ \ | |
 |_| \_|_|_|\_\_| |_|/_/   \_\___|
"""


def cprint(text: str = "", style: str = "") -> None:
    """Markup-aware print — jab text me `[bold]…[/]` jaisa markup ho."""
    if HAS_RICH:
        console.print(text, style=style)
    else:
        print(strip_markup(text))


def cprint_safe(text: str = "", style: str = "") -> None:
    """Arbitrary content (file paths, tool output, file contents) ke liye.

    Rich square-bracket ko markup samajh leta hai — user ke content me `[path]`
    jaisa kuch hone se crash ho jata tha, isliye yahan escape kar dete hain.
    """
    if HAS_RICH:
        from rich.markup import escape

        console.print(escape(str(text or "")), style=style)
    else:
        print(text)


def strip_markup(text: str) -> str:
    import re

    return re.sub(r"\[/?[a-zA-Z0-9 _.#=]{1,30}\]", "", text or "")


def banner(assistant_name: str, owner: str, version: str = "2.3") -> None:
    if not HAS_RICH:
        print(BANNER)
        print(f"{assistant_name} v{version} — owner: {owner}")
        return
    art = Text(BANNER, style=f"bold {ACCENT}")
    console.print(art)
    console.print(
        f"[bold {ACCENT}]{assistant_name}[/] [dim]v{version}[/]  •  "
        f"owner: [bold]{owner}[/]  •  [dim]LangGraph + LangChain + Browser-Use + DSPy[/]"
    )
    console.print()


def status_panel(data: dict[str, Any], title: str = "NIKHILAI") -> None:
    """Main status panel — module 16 me jo maanga gaya tha wahi fields."""
    if not HAS_RICH:
        for key, val in data.items():
            print(f"{key}: {val}")
        return
    table = Table.grid(padding=(0, 2))
    table.add_column(style=DIM, justify="right")
    table.add_column(style="bold")

    def dot(flag: bool) -> str:
        return f"[{OK}]●[/] online" if flag else f"[{BAD}]●[/] offline"

    def put(key: str, value: str) -> None:
        table.add_row(key, value)

    put("Status", f"[{OK}]●[/] {data.get('status', 'ONLINE')}")
    put("Brain", str(data.get("provider", "—")))
    put("Model", str(data.get("model", "—")))
    put("Mode", str(data.get("mode", "TEXT")).upper())
    put("Memory", str(data.get("memory", "—")))
    put("Tools", str(data.get("tools", "—")))
    put("Browser", dot(bool(data.get("browser"))))
    put("LiveKit", dot(bool(data.get("livekit"))))
    put("Telegram", dot(bool(data.get("telegram"))))
    put("Automations", str(data.get("automations", "—")))
    put("Workspace", str(data.get("workspace", "—")))
    console.print(Panel(table, title=f"[bold {ACCENT}]{title}[/]", border_style=ACCENT, expand=False))


def activity(event: str, detail: str = "", kind: str = "info") -> None:
    icons = {"info": "•", "plan": "🧭", "tool": "🔧", "search": "🔍", "browser": "🌐", "code": "💻",
             "verify": "✅", "warn": "⚠️", "error": "✖", "memory": "🧠", "done": "🏁", "voice": "🎙️"}
    color = {"warn": WARN, "error": BAD, "done": OK, "verify": OK}.get(kind, ACCENT)
    icon = icons.get(kind, "•")
    stamp = time.strftime("%H:%M:%S")
    if HAS_RICH:
        console.print(f"[{DIM}]{stamp}[/] {icon} [{color}]{event}[/]" + (f" [dim]{detail}[/]" if detail else ""))
    else:
        print(f"{stamp} {icon} {event} {detail}")


def event_to_activity(node: str, events: list[dict[str, Any]]) -> None:
    """Graph ke events ko readable activity lines me badlo."""
    mapping = {
        "route": ("plan", "Route decide hua"), "plan": ("plan", "Plan banaya"),
        "research_worker": ("search", "Research chal rahi hai"), "browser_worker": ("browser", "Browser chal raha hai"),
        "code_worker": ("code", "Code likha/chala rahe hain"), "desktop_worker": ("tool", "Desktop tools"),
        "memory_worker": ("memory", "Memory"), "synthesize": ("info", "Answer ban raha hai"),
        "critic": ("verify", "Verify kar rahe hain"), "refine": ("warn", "Improve kar rahe hain"),
        "respond": ("done", "Done"),
    }
    kind, label = mapping.get(node, ("info", node))
    detail = ""
    for ev in events or []:
        msg = ev.get("message") or ev.get("detail") or ""
        if msg:
            detail = str(msg)[:110]
            break
    if node in ("respond",):
        return
    activity(label, detail, kind)


def answer_panel(text: str, meta: str = "") -> None:
    if not HAS_RICH:
        print("\n" + text + (f"\n({meta})" if meta else ""))
        return
    body = Markdown(text) if any(m in text for m in ("**", "```", "#", "- ")) else Text(text)
    console.print(Panel(body, title=f"[bold {OK}]NikhilAI[/]", border_style=OK, subtitle=f"[dim]{meta}[/]"))


def error_panel(message: str, context: list[str] | None = None) -> None:
    """Errors ko chhupao mat — panel me dikhao, saath me last steps ka trail."""
    body = strip_markup(message or "(no message)")
    if context:
        body += "\n\n[h]Progress trail[/]\n" + "\n".join(strip_markup(c) for c in context)
    if HAS_RICH:
        console.print(Panel(body, title="[bold]⚠️  Errors[/]", border_style=BAD, padding=(0, 1)))
        return
    print(f"\n!! {body}\n")


def panel_text(text: str, title: str = "") -> None:
    """Generic output panel (tool output, raw text) — markdown-safe."""
    if HAS_RICH:
        try:
            console.print(Panel(Markdown(text or "(khali)"), title=title or None,
                                border_style=ACCENT, padding=(0, 1)))
            return
        except Exception:
            console.print(Panel(strip_markup(text or "(khali)"), title=title or None,
                                border_style=ACCENT, padding=(0, 1)))
            return
    print(f"\n== {title} ==\n{strip_markup(text)}")


def reasoning_panel(lines: list[str], title: str = "Agent reasoning (summary)") -> None:
    if not lines:
        return
    if not HAS_RICH:
        print("\n".join(lines))
        return
    from rich.markup import escape

    prefixes = ("PLAN:", "DECISION:", "ACTION:", "RESULT:", "VERIFICATION:", "NEXT:", "ERRORS:", "ITERATIONS:")
    body = "\n".join(f"[{DIM}]{escape(line)}[/]" if line.startswith(prefixes) else escape(line)
                     for line in lines)
    console.print(Panel(body, title=f"[{DIM}]{escape(title)}[/]", border_style=DIM, expand=False))


def settings_table(rows: list[tuple[str, str, str]]) -> None:
    if not HAS_RICH:
        for key, val, desc in rows:
            print(f"{key:22s} {val:28s} {desc}")
        return
    table = Table(title="Settings", border_style=ACCENT, show_lines=False)
    table.add_column("Key", style="bold")
    table.add_column("Value", style=OK)
    table.add_column("Kya karta hai", style=DIM)
    for key, val, desc in rows:
        table.add_row(key, str(val)[:40], desc)
    console.print(table)
    console.print(f"[{DIM}]Badlo: /settings <key> <value>   •   reset: /settings reset <key>[/]")


def memory_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        cprint("🧠 Memory khali hai — bolo: '[bold]yaad rakh mera naam ...[/]'", WARN)
        return
    if not HAS_RICH:
        for r in rows:
            print(f"[{r.get('kind')}] {r.get('text')}  ({str(r.get('id'))[:8]})")
        return
    table = Table(title=f"Memory ({len(rows)})", border_style=ACCENT)
    table.add_column("#", style=DIM, width=3)
    table.add_column("Kind", style="magenta", width=11)
    table.add_column("Yaad", style="white")
    table.add_column("ID", style=DIM, width=10)
    for i, r in enumerate(rows, 1):
        table.add_row(str(i), str(r.get("kind", "fact")), str(r.get("text", ""))[:100], str(r.get("id", ""))[:8])
    console.print(table)


def tools_table(groups: dict[str, list[str]]) -> None:
    if not HAS_RICH:
        for name, items in groups.items():
            print(f"{name} ({len(items)}): {', '.join(items)}")
        return
    table = Table(title="Tool registry", border_style=ACCENT)
    table.add_column("Group", style="bold")
    table.add_column("Count", style=OK, justify="right")
    table.add_column("Tools", style=DIM)
    for name, items in groups.items():
        table.add_row(name, str(len(items)), ", ".join(items[:12]) + ("…" if len(items) > 12 else ""))
    console.print(table)


def automations_table(items: list[dict[str, Any]]) -> None:
    if not items:
        cprint("⏰ Koi automation nahi — bolo: '[bold]har subah 8 baje mausam batao[/]'", WARN)
        return
    if not HAS_RICH:
        for a in items:
            print(f"[{'ON' if a['enabled'] else 'OFF'}] {a['id'][:8]} {a['schedule']} → {a['task']}")
        return
    table = Table(title="Automations", border_style=ACCENT)
    for col in ("State", "ID", "Schedule", "Next run", "Task", "Runs"):
        table.add_column(col, style="bold" if col == "ID" else "")
    for a in items:
        state = f"[{OK}]ON[/]" if a["enabled"] else f"[{BAD}]OFF[/]"
        table.add_row(state, a["id"][:8], a["schedule"], a.get("next_run_str", "-"),
                      str(a["task"])[:60], f"{a['runs']} (err {a['errors']})")
    console.print(table)


def doctor_table(checks: list[tuple[str, str, str]]) -> None:
    """checks: (status, name, detail) — status: ok|warn|fail"""
    if not HAS_RICH:
        for status, name, detail in checks:
            print(f"{status:5s} {name:34s} {detail}")
        return
    table = Table(border_style=ACCENT, show_lines=False, pad_edge=False)
    table.add_column("", width=3)
    table.add_column("Check", style="bold")
    table.add_column("Detail", style=DIM)
    marks = {"ok": f"[{OK}]✅[/]", "warn": f"[{WARN}]🟡[/]", "fail": f"[{BAD}]🔴[/]"}
    for status, name, detail in checks:
        table.add_row(marks.get(status, "•"), name, str(detail)[:90])
    console.print(table)


def help_panel(commands: dict[str, str]) -> None:
    if not HAS_RICH:
        for cmd, desc in commands.items():
            print(f"{cmd:22s} {desc}")
        return
    table = Table(title="Commands", border_style=ACCENT, show_header=False)
    table.add_column("Command", style=f"bold {ACCENT}", no_wrap=True)
    table.add_column("Kya karta hai", style="white")
    for cmd, desc in commands.items():
        table.add_row(cmd, desc)
    console.print(table)
    console.print(f"[{DIM}]Tip: normal Hinglish me seedha likho — slash command optional hai.[/]")


__all__ = [
    "HAS_RICH", "console", "cprint", "banner", "status_panel", "activity", "event_to_activity",
    "answer_panel", "reasoning_panel", "settings_table", "memory_table", "tools_table",
    "automations_table", "doctor_table", "help_panel", "strip_markup", "Live",
]
