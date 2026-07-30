from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .markets import CN_MARKET, is_market_open as market_is_open, market_now


DEFAULT_PUBLISH_HOURS = (9, 10, 11, 13, 14, 15)
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


def is_weekday(now: datetime | None = None, *, market: object = CN_MARKET) -> bool:
    return market_now(now, market).weekday() < 5


def is_market_open(now: datetime | None = None, *, market: object = CN_MARKET) -> bool:
    return market_is_open(now, market)


def should_publish_now(
    now: datetime | None = None,
    *,
    publish_hours: str | Iterable[object] | None = None,
    market: object = CN_MARKET,
) -> bool:
    current = market_now(now, market)
    return is_market_open(now, market=market) and current.hour in parse_publish_hours(publish_hours)
