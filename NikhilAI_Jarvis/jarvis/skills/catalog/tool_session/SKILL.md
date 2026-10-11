---
name: tool_session
description: "Short-term session state — current workspace, scratchpad notes, multi-step pending steps, progress updates"
keywords: [workspace, project folder, context, session, scratchpad, pending, steps, progress, status, abhi kya, current task, folder set, ab is project par, kaam kahan tak]
tools: [session_context, set_workspace, session_note, set_pending_steps, jarvis_progress]
---

# Session tools

- session_context() — current workspace, recent tasks, last error, pending steps dekho. Multi-step kaam shuru karne se pehle.
- set_workspace(path) — "ab is project par kaam karo" ke baad working folder set karo.
- session_note(note) — short-term scratchpad; agla step ise dekhega.
- set_pending_steps(steps) — multi-step plan save karo (loop se bachne ke liye).
- jarvis_progress(note) — owner ko progress update.

## Rules
- Lamba kaam: pehle set_pending_steps, har step ke baad session_note, end me pending steps clear.
