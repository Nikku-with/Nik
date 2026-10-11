---
name: tool_automation
description: "Scheduled/recurring automations banana, dekhna, on/off, turant chalana, cancel karna"
keywords: [automation, schedule, scheduled, daily, roz, har subah, every, cron, recurring, repeat, timer, remind, reminder, baad me, automatic, task schedule, 8 baje]
tools: [create_automation, list_automations, toggle_automation, run_automation, cancel_automation]
---

# Automation tools

- create_automation(task, schedule, enable) — recurring task banao ("har subah 8 baje mausam batao").
- list_automations(only_enabled) — saare automations + next run time.
- toggle_automation(ident, enabled) — ON/OFF; ident = id ka shuru ya task ka hissa.
- run_automation(ident) — abhi chalao, schedule ka wait mat karo.
- cancel_automation(ident) — hamesha ke liye delete; owner se confirm karo.

## Rules
- Schedule ko clear time ke saath likho (IST default).
- Create ke baad list_automations se confirm karo.
