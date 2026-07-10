from __future__ import annotations

from datetime import datetime

from .config import BEIJING_TZ


def beijing_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(BEIJING_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=BEIJING_TZ)
    return now.astimezone(BEIJING_TZ)


def number(value, default: float = 0.0) -> float:
    if value in (None, "-", ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
