"""
jarvis/memory/memory_tools.py — Memory ko LangChain tools banado
===============================================================

Owner ke baare me facts yaad rakhna, recall karna, conversation history dekhna,
aur DB compact karna — sab tools ke roop me (supervisor + voice agent dono use karte hain).
"""
from __future__ import annotations

from langchain_core.tools import tool

from ..config import OWNER_NAME
from .store import get_memory


@tool
async def remember_fact(fact: str, key: str = "", tags: str = "") -> str:
    """Owner ke baare me koi important fact permanently yaad rakho (SQLite memory).

    Use jab owner bole: "yaad rakhna ...", "remember karo ...", ya koi personal detail de.
    key: chhota label (e.g. 'office_time', 'favourite_stack') — same key se overwrite ho jaata hai.
    """
    fid = get_memory().remember_fact(fact, key=key, tags=tags or "owner")
    return f"🧠 Yaad rakh liya (id {fid[:8]}): {fact[:200]}"


@tool
async def recall_facts(query: str = "", limit: int = 10) -> str:
    """Long-term memory se facts/conversations recall karo (FTS5 search, Hinglish supported)."""
    mem = get_memory()
    if query:
        hits = mem.search(query, limit=limit)
        if not hits:
            return f"🧠 '{query}' par memory me kuch nahi mila."
        return "🧠 Mil gaya:\n" + "\n".join(
            f"- [{h.kind} • {round(h.score, 2)}] {h.text[:220]}" for h in hits
        )
    facts = mem.facts(limit=limit)
    if not facts:
        return f"🧠 {OWNER_NAME} ke baare me abhi tak kuch store nahi hua."
    return "🧠 Known facts:\n" + "\n".join(f"- {f['value'][:200]}" for f in facts)


@tool
async def conversation_history(limit: int = 10) -> str:
    """Pichhli baat-cheet ka summary dekho (recent turns)."""
    return "🗂️ Recent conversation:\n" + get_memory().context_block(limit=limit)


@tool
async def memory_stats() -> str:
    """Memory DB ka stats (facts / episodes / notes / tasks / size)."""
    s = get_memory().stats()
    return (
        f"🧠 Memory stats — facts: {s['facts']}, episodes: {s['episodes']}, "
        f"notes: {s['notes']}, tasks: {s['tasks']}, size: {s['db_kb']} KB"
    )


@tool
async def memory_compact(keep_last: int = 400) -> str:
    """Purane conversation turns ko summary me collapse karo (DB halka rakho)."""
    if isinstance(keep_last, bool) or not isinstance(keep_last, int) or keep_last < 1:
        return "❌ `keep_last` kam se kam 1 hona chahiye; memory history nahi badli."
    try:
        removed = get_memory().compact(keep_last=keep_last)
    except ValueError as exc:
        return f"❌ Memory compact nahi hui: {exc}"
    return f"🧹 {removed} old turns compact kar diye (last {keep_last} rakhe)."


MEMORY_TOOLS = [remember_fact, recall_facts, conversation_history, memory_stats, memory_compact]
