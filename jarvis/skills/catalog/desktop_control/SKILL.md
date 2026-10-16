---
name: desktop_control
description: "Desktop ko screenshot, OCR aur typed input tools se dekhkar safely operate karna"
keywords: [desktop, screen, screenshot, click, type, keyboard, mouse, window, clipboard, gui, application]
tools: [desktop_observe, desktop_ocr, desktop_click, desktop_move, desktop_scroll, desktop_type, desktop_press, desktop_windows, desktop_focus, desktop_clipboard_read, desktop_clipboard_write]
---

# Desktop observe → act → verify

1. `desktop_observe` se fresh screenshot, OCR, active window aur observation ID lo. Click ke coordinates isi screenshot ke pixel space se hone chahiye; purane screenshot ke coordinates reuse mat karo.
2. `desktop_windows` / `desktop_focus` tab use karo jab target window ko pehle focus karna ho. Screen observe karke confirm karo ki intended window active hai.
3. Ek samay par ek chhota action chalao: `desktop_click`, `desktop_type`, `desktop_press`, `desktop_move`, ya `desktop_scroll`. Action me observation ID do.
4. Destructive ya externally visible click (delete, send, purchase, submit) ke liye human Gate approval zaroori hai. Approval ke baad screen dobara validate hoti hai; mismatch par action cancel hota hai.
5. Har action ke baad `desktop_observe`/`desktop_ocr` se visible change verify karo. Change na dikhe to ek bounded retry hi; phir honest failure report karo.

## Examples

- App me text dhoondhna: observe → screenshot-derived click → type search → observe.
- Form draft: focus window → observe → type fields → observe; submit se pehle owner se confirm.
- Copy/paste: `desktop_clipboard_read` se content read; write karne se pehle exact text owner ko dikhana.
- Scroll result list: observe → `desktop_scroll` → observe; relevant row ka fresh coordinate nikaalo.
- File delete dialog: observe → exact target verify → human-approved destructive click → verify deletion; unclear target par cancel.

Headless/no-display environment me tool friendly unavailable status deta hai; result ko success mat bolo.
