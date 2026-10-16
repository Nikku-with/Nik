"""Rule-first gates for answering with live clock context only when requested."""
from __future__ import annotations

import re

_TIME_QUESTION = re.compile(
    r"\b(?:"
    r"what\s+(?:is|s)\s+(?:the\s+)?(?:current\s+)?(?:time|date|day)|"
    r"what\s+time\s+is\s+it|what\s+date\s+is\s+it|what\s+day\s+is\s+it|"
    r"which\s+day\s+is\s+it|current\s+(?:time|date)|time\s+now|date\s+today|"
    r"today(?:'s|\s+is)?\s+date|tell\s+me\s+(?:the\s+)?(?:time|date|day)|"
    r"aaj\s+(?:ka|ki)\s+(?:time|samay|date|tareekh|tarikh|din)|"
    r"abhi\s+(?:kya\s+time|kitne\s+baje|samay)|kitne\s+baje|samay\s+kya|time\s+kya|"
    r"kal\s+(?:ki|ka)\s+(?:date|tareekh|tarikh|din)|parso\s+(?:ki|ka)\s+(?:date|tareekh|tarikh|din)|"
    r"beeta\s+kal\s+(?:ki|ka)\s+(?:date|tareekh|tarikh|din)|"
    r"(?:current\s+)?(?:utc\s+)?time\s+in\s+[\w /-]+|"
    r"(?:utc|timezone|time\s+zone|clock|ghadi)\s+(?:now|kya|kitna|batao|bataiye)|"
    r"what(?:'s|\s+is)\s+the\s+utc\s+time"
    r")\b",
    re.IGNORECASE,
)

_TIME_TASK = re.compile(
    r"\b(?:schedule|scheduling|remind(?:er)?|automation|deadline|due\s+date|calendar|"
    r"by\s+(?:today|tomorrow|tonight|next\s+\w+)|every\s+(?:day|week|month|morning|hour)|"
    r"daily|weekly|har\s+(?:subah|shaam|sham|raat|din|ghante?)|"
    r"tomorrow|yesterday|day\s+after\s+tomorrow|next\s+week|next\s+month|"
    r"kal|parso|beeta\s+kal|bita\s+kal|guzra\s+kal|aaj\s+ki\s+(?:date|tareekh|tarikh)|"
    r"due\s+(?:on|by)|at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|baje)?)\b",
    re.IGNORECASE,
)


def is_explicit_time_question(text: str) -> bool:
    """True only for a direct date/time/day/clock question, not casual date words."""
    value = " ".join((text or "").split())
    return len(value.split()) <= 18 and bool(_TIME_QUESTION.search(value))


def needs_clock_context(text: str) -> bool:
    """Whether a planner needs local time to resolve an explicit schedule/deadline/date ask."""
    value = " ".join((text or "").split())
    return is_explicit_time_question(value) or bool(_TIME_TASK.search(value))
