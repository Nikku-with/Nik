---
name: browser_supervision
description: "Browser tasks ko progress, budget, stuck detection aur registered tools ke saath supervise karna"
keywords: [browser, browsing, web page, page, website, click, form, login, research]
tools: [browser_task, browser_research, browser_screenshot, browser_cli, browser_doctor, google_search, deep_research, fetch_page, save_note]
---

# Browser workflow

1. Simple page reading ke liye pehle `fetch_page` / `deep_research` try karo; browser tabhi use karo jab JavaScript ya interaction zaroori ho.
2. `browser_task` ko ek explicit task, relevant URL, small output-field list aur bounded step/time budget do. Progress events se user ko stage/status batate raho.
3. Browser agent ke required external actions ke liye registered `jarvis_request_tool` route use hona chahiye; arbitrary/unregistered tool requests reject hon. Dangerous browser actions par human Gate chahiye.
4. Repeated identical page state/stuck loop par task pause/stop karo. Ek site ka failure parallel research ke baaki sites ko cancel nahi karna chahiye.
5. `browser_research` ke results ko normalize karo aur source URL/status ko retain karo. Sites par returned value ko “independently verified” mat bolo.
6. Owner ke kahe par `save_note` se report save karo. Existing non-empty note overwrite Gate se approve hona chahiye.

## Rules

- Passwords, session cookies, tokens, ya credential values report/progress me mat dikhana.
- Purchase, message-send, account/delete, ya final submit actions se pehle explicit owner confirmation lo.
- Failure, budget exhaustion, missing browser package, ya Chromium setup error ko exact offline/unavailable reason ke saath report karo.
