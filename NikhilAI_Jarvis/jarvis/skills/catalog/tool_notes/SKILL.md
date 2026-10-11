---
name: tool_notes
description: "Notes save, append, read, list, search karna (disk + memory index)"
keywords: [note, notes, notepad, save, likh, likhke, rakh, record, register, log, append, list notes, search notes, diary, todo, checklist]
tools: [save_note, append_note, read_note, list_notes, search_notes, jarvis_save_note]
---

# Notes tools (WRITE level)

- save_note(name, content, open_in_notepad, as_txt, tags) — naya note save (memory me bhi index). Naam clear rakho: "invoice_register".
- append_note(name, content) — existing note me add karo; nahi hai to naya bana deta hai. Register/log ke liye append wala flow use karo.
- read_note(name) — note ka content padho.
- list_notes(limit) — saare notes.
- search_notes(query) — notes ke andar keyword search (Hinglish bhi).
- jarvis_save_note(name, content) — browser flow se note save (same storage).

## Rules
- Pehle search_notes/list_notes se check karo ki note already hai ya nahi, phir append vs save decide karo.
- Har nayi entry me date aur source likho.
