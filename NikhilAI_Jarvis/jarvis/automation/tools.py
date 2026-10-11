"""
Automation tools — agent inhe LangChain tools ki tarah call karta hai
====================================================================

    "har subah 8 baje mausam batao"  → create_automation(task, schedule)
    "mere automations dikhao"        → list_automations()
    "wo automation band kar do"      → toggle_automation(ident, False)
    "abhi chala do"                  → run_automation(ident)
"""
from __future__ import annotations

from langchain_core.tools import tool

from . import scheduler as sch


def _fmt(items: list[dict]) -> str:
    if not items:
        return "📋 Koi automation set nahi hai. Bolo: \"har subah 8 baje mausam batao\""
    lines = [f"⏰ {len(items)} automation(s):"]
    for a in items:
        state = "🟢 ON " if a["enabled"] else "🔴 OFF"
        lines.append(
            f"{state} #{a['id'][:8]}  {a['schedule']:<20} next: {a.get('next_run_str', '-')}\n"
            f"    task: {a['task'][:90]}\n"
            f"    runs: {a['runs']} • errors: {a['errors']}"
            + (f" • last: {a['last_result'][:70]}" if a.get("last_result") else "")
        )
    return "\n".join(lines)


@tool
def create_automation(task: str, schedule: str, enable: bool = True) -> str:
    """Naya scheduled/recurring automation banao.

    task: kya karna hai (poori Hinglish request, jaise "aaj ka mausam batao")
    schedule: "har subah 8 baje", "daily 18:30", "every 2h", "kal 6 PM", "har 30 minute"
    enable: False karo to save hoga par chale ga nahi.
    """
    res = sch.get_store().create(task, schedule, enabled=enable)
    if not res.get("ok"):
        return f"❌ {res['message']}"
    auto = res["automation"]
    return (f"✅ Automation set: #{auto['id'][:8]}\n"
            f"• Kab: {auto['schedule']} (next: {auto['next_run_str']})\n"
            f"• Kya: {auto['task']}\n"
            f"Manage: /automation list | /automation off {auto['id'][:8]}")


@tool
def list_automations(only_enabled: bool = False) -> str:
    """Saare automations dikhao (next run time ke saath)."""
    return _fmt(sch.get_store().list(enabled_only=only_enabled))


@tool
def toggle_automation(ident: str, enabled: bool = True) -> str:
    """Automation ON/OFF karo (ident = id ka shuru ya task ka hissa)."""
    res = sch.get_store().set_enabled(ident, enabled)
    return ("✅ " if res.get("ok") else "❌ ") + res["message"]


@tool
def run_automation(ident: str) -> str:
    """Automation ko turant chalao (schedule ka wait mat karo)."""
    res = sch.run_now(ident)
    return ("▶️ " if res.get("ok") else "❌ ") + res["message"]


@tool
def cancel_automation(ident: str) -> str:
    """Automation hamesha ke liye delete karo."""
    res = sch.get_store().cancel(ident)
    return ("🗑️ " if res.get("ok") else "❌ ") + res["message"]


AUTOMATION_TOOLS = [create_automation, list_automations, toggle_automation,
                    run_automation, cancel_automation]
