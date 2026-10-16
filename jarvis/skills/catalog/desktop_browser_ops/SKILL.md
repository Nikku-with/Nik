---
name: desktop_browser_ops
description: "Desktop ya browser ko per-step observe–act–verify discipline ke saath operate karna"
keywords: [desktop, browser, gui, click, type, window, form, page, "screen control", "browser me"]
tools: [open_any_app, desktop_observe, desktop_focus, desktop_click, desktop_type, desktop_press, wait_for_text, browser_task, browser_screenshot]
---

# Desktop and browser operations

1. Task ko small ordered actions me todo. Pehle sasta read-only route (`fetch_page` when appropriate) prefer karo; UI interaction sach me zaroori ho tab browser/desktop control use karo.
2. Desktop ke liye `desktop_observe` se fresh screenshot, active-window context aur observation ID lo. Wrong app/window ho to `desktop_focus` ya `open_any_app` se target locate/focus karo, phir dobara observe karke verify karo.
3. Ek action ek baar chalao: `desktop_click`, `desktop_type`, ya `desktop_press` ko current observation ID ke saath use karo. Har action ke baad fresh `desktop_observe` ya `wait_for_text` se expected visible change verify karo; stale coordinates/observation IDs reuse mat karo.
4. Browser interaction ke liye `browser_task` ko exact URL/task aur bounded steps ke saath do. Output ka result `browser_screenshot` ya returned page evidence se inspect karo; another site ko same browser state se implicitly access mat samjho.
5. Login, CAPTCHA, unclear target, policy refusal, Gate request, timeout, ya missing browser/display dependency aaye to stop karke exact blocker batao. Payment, message-send, irreversible submit/delete ke liye owner ka explicit approval lo; existing permission surface aur Dangerous-Command Gate ko bypass mat karo.
6. Successful result ko tool output/evidence se confirm karo. Failed action ko ek baar hi targeted retry karo after re-focus/re-observation; successful action repeat mat karo.

## Rules

- Mouse/keyboard env defaults on hone se surface permission ya Gate approval replace nahi hota.
- Passwords/tokens ko type, log, ya report mat karo jab tak owner ne zaroori na kaha ho; secrets ko progress text me expose mat karo.
- Tool ne success verify na kiya ho to “ho gaya” mat bolo. Report actual visible outcome, status, aur next needed owner step.
