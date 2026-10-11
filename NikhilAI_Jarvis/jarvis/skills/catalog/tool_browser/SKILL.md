---
name: tool_browser
description: "Browser automation — login/forms/JS sites par tasks, multi-site research, screenshot, browser health check"
keywords: [browser, chrome, website, site, login, form, click, scrape, portal, automation, browse, page, screenshot browser, sign in, book, fill, price, compare]
tools: [browser_task, browser_research, browser_cli, browser_screenshot, browser_close, browser_doctor]
---

# Browser tools (EXECUTE level, browser-use)

- browser_task(task) — ek website par multi-step kaam (click, type, form fill, login ke baad data nikalna).
- browser_research(task, sites, fields) — multiple sites par parallel browser agents; har site se fields nikalo (price compare jaisa).
- browser_cli(code, timeout) — browser-use CLI, direct CDP control. Advanced/fast; pehle browser_task try karo.
- browser_screenshot(name) — current page ka screenshot.
- browser_close() — session band karke RAM free karo. Kaam khatam hone par chalao.
- browser_doctor() — setup check (python API, CLI, chromium, provider). Browser fail ho to pehle ye.

## Rules
- Login/payment/form submit se pehle owner se confirm karo.
- Simple page padhne ke liye browser nahi — fetch_page (tool_web_research) use karo.
- Kaam ke end par browser_close.
