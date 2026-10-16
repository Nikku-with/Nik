---
name: memory_management
description: "Owner facts ko explicit consent se save, recall, update ya delete karna"
keywords: [memory, yaad, remember, recall, preference, profile, forget, update]
tools: [recall_facts, remember_fact, memory_stats, memory_compact, conversation_history]
---

# Memory workflow

1. Recall request par `recall_facts` / `conversation_history` se matching owner/session context dhoondo. No match ho to “nahi mila” bolo; fact invent mat karo.
2. “Yaad rakho” par sirf wahi specific fact save karo jo user ne diya; `remember_fact` ke result ko check karke success bolo.
3. Update ke liye existing record locate karo, target/value clear rakho, aur same key ke saath `remember_fact` use karo; returned result verify karo.
4. Delete/forget request me exact memory item confirm karo. Human confirmation ke bina destructive delete mat karo; cancellation/timeout par kuch delete nahi hota.
5. `memory_stats`/`memory_compact` diagnostics/maintenance ke liye hain; compact ko user facts delete karne ke roop me describe mat karo.

Owner ki memory cross-surface shared hai; guest scope alag hai. Sensitive secrets/passwords memory me store mat karo.
