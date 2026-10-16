---
name: automation_setup
description: "Natural-language schedule ko confirm karke persistent automation banana aur monitor karna"
keywords: [automation, schedule, remind, reminder, daily, every, har subah, cron, recurring]
tools: [create_automation, list_automations, toggle_automation, cancel_automation, run_automation]
---

# Automation workflow

1. Request se task, schedule, timezone aur delivery surface nikaalo. Ambiguous time par owner se clarify karo. Kota default timezone `Asia/Kolkata` hai; configured timezone ko prefer karo.
2. Schedule parser ke returned normalized schedule aur next-run time ko owner ko dikhakar confirm karo.
3. `create_automation` se persist karo; returned ID/status save karo. Persistence failure ho to automation “set” nahi hui.
4. `list_automations` se verify karo. Pause/resume ke liye `toggle_automation`; remove ke liye `cancel_automation`.
5. `run_automation` manual test ke liye sirf owner request par. Execution-time Gate stored origin surface par dobara approve karega; approval na ho to action fail/cancel hi report hoga.

## Delivery and truth

- Automation origin CLI/API/Telegram/voice se persist hota hai. Result usi origin surface ko deliver karo; scheduler result ka unique run ID mention karo.
- Coalesced/missed runs, gate-denial, provider failure, ya delivery failure ko alag status me dikhana. Success tabhi bolo jab AgentResult evidence verify kare.
