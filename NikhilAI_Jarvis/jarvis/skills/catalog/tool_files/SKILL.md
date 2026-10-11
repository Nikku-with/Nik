---
name: tool_files
description: "Files dhoondhna, padhna, likhna, folders manage karna, file ke andar search karna"
keywords: [file, files, folder, dhoondo, dhundo, find, read, padho, pdf, word, excel, csv, search, grep, likho, write, list, directory, recent, aaj, rename, copy, move, delete, play, music, video, gana]
tools: [find_files, find_file, read_file, search_inside_files, recent_files, list_dir, write_file, folder_file, open_path, play_file]
---

# File tools

- find_file(name, kind) — fuzzy naam se ek best file/folder ka path. Shortcut.
- find_files(...) — zyada filters ke saath file search (typo-proof).
- read_file(path, max_chars, pages, rows, member) — PDF, Word, Excel, CSV, JSON, notebook, PPT, image, zip, code, text — sab padho. PDF ke liye pages="1-3" jaisa use karo.
- search_inside_files(query, ext, limit) — files ke ANDAR text dhoondo ("kahan likha tha ye?").
- recent_files(days, ext) — hal hi me badli files ("aaj kis file par kaam hua").
- list_dir(path, pattern) — folder listing; pattern jaise '*.py'. Default: workspace.
- write_file(path, content) — text file likho (parent folders auto). System paths blocked.
- folder_file(command) — natural language folder ops: banao, rename, delete, copy, kholo. Delete se pehle owner se confirm.
- open_path(path, app) — file/folder default app me kholo; app="code" jaise.
- play_file(name) — media file dhoondh kar play.

## Tips
- Path guess mat karo: pehle find_file / list_dir se exact path lo, phir read/open.
- Bade file ke liye read_file me max_chars/pages limit do.
- Files workspace me hi likho jab tak owner na bole.
