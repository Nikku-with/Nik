---
name: tool_desktop_apps
description: "Desktop apps kholna/band karna, windows dekhna, typing/hotkey/volume/screenshot, installed apps dhoondhna"
keywords: [open, khol, kholo, app, apps, chrome, notepad, vscode, close, band, window, type, likho, hotkey, shortcut, volume, awaaz, screenshot, screen, installed, launch, desktop]
tools: [open_app, close_app, list_windows, type_text, press_hotkey, control_volume, take_screenshot, open_any_app, find_apps_tool, list_apps, refresh_app_index, system_info]
---

# Desktop & apps tools (SYSTEM level)

- open_app(app_title) — known app kholo (notepad, chrome, vscode, calculator, vlc, terminal).
- open_any_app(query) — koi bhi installed app approximate naam se kholo (search + launch). Pehle yahi try karo agar naam common nahi.
- find_apps_tool(query) / list_apps(query) — installed apps dhoondo / list karo. Debug ke liye list_apps.
- refresh_app_index() — naya app install hone ke baad chalao.
- close_app(window_title) — window/app band karo ("notepad band karo").
- list_windows() — open windows dekho; close/type se pehle naam confirm karne ke liye.
- type_text(text, press_enter) — focused window me type karo. Pehle us window ko open/focus karo.
- press_hotkey(keys) — shortcut: 'ctrl+s', 'ctrl+shift+t', 'alt+f4', 'win+d'.
- control_volume(action, steps) — action: up | down | mute.
- take_screenshot(name) — screenshot workspace me save hota hai.
- system_info() — OS, CPU, disk, workspace path.

## Steps
1. open_app/open_any_app → 2. list_windows se window confirm → 3. type_text/press_hotkey → 4. take_screenshot ya list_windows se result verify karo.
Windows/macOS pe typing/hotkey zero-install hai; Linux pe xdotool/wtype chahiye.
