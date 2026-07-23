from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .utils import beijing_now


DEFAULT_PUBLISH_HOURS = (9, 10, 11, 13, 14, 15)
MORNING_SESSION = (9 * 60 + 30, 11 * 60 + 30)
AFTERNOON_SESSION = (13 * 60, 15 * 60)


def parse_publish_hours(values: str | Iterable[object] | None) -> tuple[int, ...]:
    if values is None or values == "":
        return DEFAULT_PUBLISH_HOURS
    raw_values = values.replace("，", ",").split(",") if isinstance(values, str) else values
    hours: list[int] = []
    seen: set[int] = set()
    for value in raw_values:
        text = str(value).strip()
        if not text:
            continue
        try:
            hour = int(text)
        except ValueError as exc:
            raise ValueError(f"无效的发布时间：{value!r}") from exc
        if not 0 <= hour <= 23:
            raise ValueError(f"发布时间必须在 0-23 点之间：{hour}")
        if hour not in seen:
            seen.add(hour)
            hours.append(hour)
    if not hours:
        raise ValueError("发布时间不能为空")
    return tuple(sorted(hours))


def is_weekday(now: datetime | None = None) -> bool:
    return beijing_now(now).weekday() < 5


def is_market_open(now: datetime | None = None) -> bool:
    current = beijing_now(now)
    if current.weekday() >= 5:
        return False
    minute = current.hour * 60 + current.minute
    return MORNING_SESSION[0] <= minute <= MORNING_SESSION[1] or AFTERNOON_SESSION[0] <= minute <= AFTERNOON_SESSION[1]


def should_publish_now(
    now: datetime | None = None,
    *,
    publish_hours: str | Iterable[object] | None = None,
) -> bool:
    current = beijing_now(now)
    return is_market_open(current) and current.hour in parse_publish_hours(publish_hours)
