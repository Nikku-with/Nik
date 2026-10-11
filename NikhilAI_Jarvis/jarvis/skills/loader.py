"""
jarvis/skills/loader.py — SKILL.md instruction packs (OpenClaw se seekha hua)
============================================================================

OpenClaw me "skill" = ek `SKILL.md` file jo agent ko **workflow sikhati hai** (tool nahi).
Humne wahi pattern apnaya:

    skills/
      gst_invoice/SKILL.md      → "GST invoice banate waqt ye steps follow karo..."
      youtube_upload/SKILL.md   → "video upload karne ka exact sequence..."
      client_onboarding/SKILL.md

Kaise chalta hai:
  1. Har SKILL.md me frontmatter: name, description, keywords (+ optional tools)
  2. Supervisor/planner ko sirf **relevant** skills inject hoti hain (keyword + fuzzy match,
     max `JARVIS_SKILLS_MAX_INJECT`) — poora catalog prompt me nahi jaata (token bachta hai,
     decision fatigue kam — OpenClaw ka Tool Search/Code Mode wala idea)
  3. Agent skill follow karta hai, tools wahi purane
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import SKILLS_MAX_INJECT, SKILLS_DIR

FRONTMATTER_RX = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


@dataclass
class Skill:
    name: str
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    body: str = ""
    path: str = ""

    def render(self, max_chars: int = 1800) -> str:
        return f"### SKILL: {self.name}\n{self.description}\n\n{self.body[:max_chars]}"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    meta: dict = {}
    m = FRONTMATTER_RX.match(text)
    if not m:
        return meta, text
    raw, body = m.group(1), text[m.end():]
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip().strip('"\'')
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip().strip('"\'') for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key] = value
    return meta, body


_CACHE: dict[str, object] = {"ts": 0.0, "skills": []}
CACHE_TTL_S = 300


def clear_cache() -> None:
    """Skill cache clear karo (refresh system ke liye)."""
    try:
        _CACHE["ts"] = 0
        _CACHE["items"] = []
    except Exception:
        pass


def load_skills(force: bool = False) -> list[Skill]:
    """skills/ (ya JARVIS_SKILLS_DIR) ke saare SKILL.md load karo."""
    import time

    now = time.time()
    if not force and _CACHE["skills"] and now - float(_CACHE["ts"]) < CACHE_TTL_S:
        return list(_CACHE["skills"])  # type: ignore[arg-type]

    skills: list[Skill] = []
    try:
        for path in sorted(SKILLS_DIR.rglob("SKILL.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            meta, body = _parse_frontmatter(text)
            skills.append(
                Skill(
                    name=str(meta.get("name") or path.parent.name),
                    description=str(meta.get("description") or ""),
                    keywords=[str(k).lower() for k in (meta.get("keywords") or [])],
                    tools=[str(t) for t in (meta.get("tools") or [])],
                    body=body.strip(),
                    path=str(path),
                )
            )
    except Exception:
        pass

    _CACHE.update({"ts": now, "skills": skills})
    return skills


def matching_skills(request: str, limit: int = SKILLS_MAX_INJECT) -> list[Skill]:
    """Request se relevant skills chuno (keyword hit → +3, description overlap → +1)."""
    req = (request or "").lower()
    if not req.strip():
        return []
    scored: list[tuple[float, Skill]] = []
    for skill in load_skills():
        score = 0.0
        for kw in skill.keywords:
            if kw and kw in req:
                score += 3.0
        for word in re.findall(r"[a-z\u0900-\u097F]{4,}", req):
            if word in skill.description.lower() or word in skill.name.lower():
                score += 1.0
        if score > 0:
            scored.append((score, skill))
    scored.sort(key=lambda x: -x[0])
    return [s for _score, s in scored[:limit]]


def skills_prompt_block(request: str, limit: int = SKILLS_MAX_INJECT) -> str:
    """Planner/supervisor prompt me daalne wala block (khali string agar koi skill match nahi)."""
    skills = matching_skills(request, limit)
    if not skills:
        return ""
    parts = ["## Relevant skills (owner ke bataye hue workflows — inhe follow karo)"]
    for s in skills:
        parts.append(s.render())
    return "\n\n".join(parts)


def catalog() -> str:
    skills = load_skills()
    if not skills:
        return f"(koi skill nahi — `{SKILLS_DIR}/<naam>/SKILL.md` me add karo)"
    return "\n".join(f"- {s.name}: {s.description[:100]}  [{s.path}]" for s in skills)


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
try:
    from langchain_core.tools import tool
except Exception:  # pragma: no cover
    tool = None  # type: ignore

if tool is not None:

    @tool
    async def list_skills() -> str:
        """Saved skills (SKILL.md workflow packs) ki list dikhao."""
        return "🧩 Skills:\n" + catalog()

    @tool
    async def read_skill(name: str) -> str:
        """Kisi skill ka poora content padho (naam se partial match bhi chalega)."""
        low = (name or "").lower()
        for s in load_skills():
            if low in s.name.lower() or low in s.path.lower():
                return s.render(max_chars=6000)
        return f"❌ '{name}' skill nahi mili. Available:\n" + catalog()

    SKILL_TOOLS = [list_skills, read_skill]
else:  # pragma: no cover
    SKILL_TOOLS = []
