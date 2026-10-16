"""Shared timezone selection with a fixed IST fallback for systems without tzdata."""
from __future__ import annotations

from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from .config import TIMEZONE


def configured_timezone(name: str | None = None) -> tzinfo:
    """Resolve the configured IANA zone, then Kolkata, then fixed UTC+05:30."""
    requested = str(name or TIMEZONE or "Asia/Kolkata").strip() or "Asia/Kolkata"
    try:
        return ZoneInfo(requested)
    except Exception:
        try:
            return ZoneInfo("Asia/Kolkata")
        except Exception:
            return timezone(timedelta(hours=5, minutes=30), name="IST")
