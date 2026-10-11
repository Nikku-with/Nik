---
name: tool_memory
description: "Owner ki facts/preferences permanently yaad rakhna, purani baatein recall karna, memory stats/compact"
keywords: [yaad, remember, memory, recall, fact, prefer, preference, pasand, favourite, bhool, forget, history, pichli baat, last time, memory stats, compact, profile]
tools: [remember_fact, recall_facts, conversation_history, memory_stats, memory_compact, jarvis_remember, set_preference]
---

# Memory tools

- remember_fact(fact, key, tags) — permanent fact (SQLite). Owner ka naam, pasand, details.
- recall_facts(query, limit) — FTS5 search, Hinglish supported. Kuch yaad karne se pehle yahi.
- conversation_history(limit) — recent turns ka summary.
- memory_stats() — facts/episodes/notes/tasks/size.
- memory_compact(keep_last) — purane turns ko summary me collapse (DB halka rakhne ke liye).
- jarvis_remember(fact) — browser flow se same remember.
- set_preference(key, value) — short-term preference (language, tone, default browser).

## Rules
- Sirf wahi yaad karo jo owner ne bataya ho ya explicitly kaha ho. Guess mat karo.
- Purani fact update karni ho to remember_fact me same key use karo.
