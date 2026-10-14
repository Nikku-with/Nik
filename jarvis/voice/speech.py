"""
Speech text cleanup — jo bola jaye wo bolne layak ho
==================================================

Problem: LLM ka text chat ke liye hota hai — usme emojis, markdown, URLs, code blocks, tables sab
aata hai. TTS usse literally padh deta hai ("white heavy check mark", "asterisk asterisk", "https
colon slash slash..."). Ye module us text ko **natural spoken Hinglish** me convert karta hai.

    "Great! ✅ File saved successfully."     → "Great. File saved successfully."
    "**3 files** mili — [link](https://x.com)" → "3 files mili, link bhej diya"
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# emoji → spoken text (ya poora hata do)
# ---------------------------------------------------------------------------
_EMOJI_SPOKEN: dict[str, str] = {
    "✅": " ho gaya.", "❌": " fail hua.", "⚠️": " dhyan do,", "🔥": "", "🚀": "",
    "😊": "", "🙌": "", "👍": "", "🎉": " badhiya!", "🧠": "", "💡": "",
    "📄": "", "📁": "", "🔍": "", "🔎": "", "🖥️": "", "⚙️": "", "🧩": "", "🧹": "",
    "⏰": "", "📋": "", "🟢": "", "🔴": "", "▶️": "", "🗑️": "", "♻️": "", "🎙️": "",
    "🧯": "", "🔄": "", "🛠️": "", "📌": "", "🔐": "", "🧐": "", "🕒": "", "📝": "",
}

_EMOJI_RX = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # symbols & pictographs, supplemental
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F680-\U0001F6FF"   # transport
    "\U0001F1E0-\U0001F1FF"   # flags
    "\U00002600-\U000027BF"   # misc symbols / dingbats
    "\U0001F900-\U0001F9FF"   # supplemental symbols
    "\U00002190-\U000021FF"   # arrows
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U00002B00-\U00002BFF"
    "]+",
    flags=re.UNICODE,
)

_CODE_BLOCK_RX = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_INLINE_CODE_RX = re.compile(r"`([^`]+)`")
_MD_LINK_RX = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
_URL_RX = re.compile(r"https?://\S+|www\.\S+")
_MD_HEADING_RX = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_BOLD_RX = re.compile(r"(\*\*|__)(.*?)\1")
_MD_ITALIC_RX = re.compile(r"(?<!\w)(\*|_)(?=\S)(.*?)(?<=\S)\1(?!\w)")
_MD_STRIKE_RX = re.compile(r"~~(.*?)~~")
_TABLE_RX = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_BULLET_RX = re.compile(r"^\s*(?:[-*•+]|\d+[.)])\s+", re.MULTILINE)
_HR_RX = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
_MULTI_SPACE_RX = re.compile(r"[ \t]{2,}")
_MULTI_PUNCT_RX = re.compile(r"([!?.,])\1{1,}")
_EMPTY_LINES_RX = re.compile(r"\n{3,}")

# code/symbol-heavy tokens jo bolne layak nahi
_PATHY_RX = re.compile(r"\b(?:[\w.-]+/){2,}[\w.-]+\b")
_LONG_ID_RX = re.compile(r"\b[0-9a-f]{16,}\b", re.I)


def strip_emoji(text: str, spoken: bool = True) -> str:
    """Emoji hatao. spoken=True par kuch emojis ke words daal do (✅ → 'ho gaya')."""
    if not text:
        return ""
    if spoken:
        for emoji, words in _EMOJI_SPOKEN.items():
            if emoji not in text:
                continue
            # mapped word already text me maujood hai (e.g. "Ho gaya boss ✅") → repeat mat karo
            core = words.strip(" .,!").lower()
            if core and core in text.lower():
                text = text.replace(emoji, "")
            else:
                text = text.replace(emoji, words)
    return _EMOJI_RX.sub("", text)


def for_speech(text: str, max_chars: int = 1200, keep_links: bool = False) -> str:
    """Chat text → bolne layak text. Voice/TTS se pehle yahi call karo."""
    if not text:
        return ""
    out = text

    # 1) code blocks: poora code bolna bekaar — chhota reference
    out = _CODE_BLOCK_RX.sub(" (code file me hai)", out)
    out = _INLINE_CODE_RX.sub(r"\1", out)

    # 2) links
    if keep_links:
        out = _MD_LINK_RX.sub(r"\1", out)
    else:
        out = _MD_LINK_RX.sub(r"\1", out)
        out = _URL_RX.sub("", out)

    # 3) markdown structure
    out = _TABLE_RX.sub("", out)
    out = _HR_RX.sub("", out)
    out = _MD_HEADING_RX.sub("", out)
    out = _MD_STRIKE_RX.sub(r"\1", out)
    out = _MD_BOLD_RX.sub(r"\2", out)
    out = _MD_ITALIC_RX.sub(r"\2", out)
    out = _BULLET_RX.sub("", out)

    # 4) paths / ids bolna useless
    out = _PATHY_RX.sub("file path", out)
    out = _LONG_ID_RX.sub(" id", out)

    # 5) emoji + whitespace + punctuation
    out = strip_emoji(out, spoken=True)
    # emoji ka mapped word pehle se text me ho to repeat na karo
    # ("Ho gaya boss ✅" → "Ho gaya boss." not "Ho gaya boss ho gaya.")
    for filler in ("ho gaya", "fail hua", "dhyan do"):
        out = re.sub(rf"\b({re.escape(filler)})\b(\W*)\b{re.escape(filler)}\b", r"\1", out, flags=re.I)
    out = _MULTI_PUNCT_RX.sub(r"\1", out)
    out = re.sub(r"\*\*|\*|__|`", "", out)
    out = _MULTI_SPACE_RX.sub(" ", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = _EMPTY_LINES_RX.sub("\n\n", out).strip()

    # 6) headings/labels ko natural banao
    out = re.sub(r"^(Evidence|Findings|Plan|Result|Answer)\s*:\s*$", "", out, flags=re.I | re.M)
    out = re.sub(r"\bWorkers jo chale\b", "kaam kisne kiya", out, flags=re.I).strip()

    if len(out) > max_chars:
        cut = out[:max_chars]
        for sep in (". ", "! ", "? ", "\n"):
            idx = cut.rfind(sep)
            if idx > max_chars * 0.5:
                cut = cut[: idx + 1]
                break
        out = cut.rstrip() + "… aur detail chat me bhej di hai."
    return out.strip()


def is_speakable(text: str) -> bool:
    """Bolne layak kuch bacha hai ya nahi."""
    return len(re.sub(r"[^\w\s]", "", for_speech(text))) > 1


def summary_for_speech(answer: str, max_sentences: int = 3) -> str:
    """Lamba jawab → 2-3 line spoken summary (voice me chhota jawab natural lagta hai)."""
    clean = for_speech(answer)
    if not clean:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", clean)
    out = " ".join(parts[:max_sentences]).strip()
    return out if out else clean[:280]


if __name__ == "__main__":  # quick manual check
    samples = [
        "Great! ✅ File saved successfully at /home/user/notes/a.md",
        "**3 files** mili — [report](https://example.com/x.pdf) aur 🔥 2 aur",
        "```python\nprint('hi')\n```\nYe code chal gaya 👌",
    ]
    for s in samples:
        print("IN :", s.replace("\n", " | "))
        print("OUT:", for_speech(s))
        print()
