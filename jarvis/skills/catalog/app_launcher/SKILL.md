---
name: app_launcher
description: "App ko memory-aware search se kholna aur verified launch ke baad mapping yaad rakhna"
keywords: [app, application, launch, kholo, start, chrome, browser, editor, notepad, open]
tools: [list_apps, find_apps_tool, open_app, open_any_app, manage_apps]
---

# App launch workflow

1. `list_apps` se saved mappings dekho. Agar request se mapping match hoti hai to pehle wahi app/path use karo; search tabhi karo jab mapping missing ho.
2. Mapping stale ya launch fail ho to usko stale result samjho, chupchaap success mat dikhao. `find_apps_tool` / `open_any_app` se available candidates search karo.
3. Ambiguous candidates hon to owner se app choose karwao. `open_app` / `open_any_app` result aur visible process/window evidence se launch verify karo.
4. Mapping sirf verified successful launch ke baad save/refresh karo via `manage_apps`. Failed attempt, guess, ya nonzero exit par mapping update mat karo.
5. Owner ko actual app name, launch status, aur failure reason (agar ho) batao.

## Rules

- Mapping sirf app title/path/alias ke open-app lookup ke liye hai; arbitrary commands ya app contents store nahi karne.
- Uninstall, file deletion, account changes, ya system settings ko app launch request na samjho; alag request aur Gate chahiye.
