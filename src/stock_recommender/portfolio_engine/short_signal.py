"""Interpretation of short-side portfolio signals."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import ClassVar, Iterable, Mapping

from .config import ShortPolicy
from .contracts import (
    EventCalendar,
    PositionSide,
    SignalCandidate,
    SignalRow,
)


MINIMUM_DAILY_USD_TURNOVER = 20_000_000.0
REQUESTED_SHORT_WEIGHT_PCT = 5.0
MAXIMUM_SHORT_SIGNALS = 10

_RANKING_FIELDS = (
    "negative_momentum_persistence",
    "below_ma60_distance",
    "inverse_volatility",
    "liquidity",
)


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _date_value(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _policy_value(policy: object, field_name: str, default: object) -> object:
    if isinstance(policy, Mapping):
        return policy.get(field_name, default)
    return getattr(policy, field_name, default)


def _normalized_values(
    rows: list[dict[str, object]], field_name: str
) -> dict[int, float]:
    values = [float(row[field_name]) for row in rows]
    low = min(values)
    high = max(values)
    if high == low:
        return {id(row): 0.5 for row in rows}
    width = high - low
    return {id(row): (float(row[field_name]) - low) / width for row in rows}


@dataclass(frozen=True)
class ShortTrendBreakdownV1:
    policy: ShortPolicy | Mapping[str, object] = field(default_factory=ShortPolicy)

    model_id: ClassVar[str] = "short_trend_breakdown_v1"
    side: ClassVar[PositionSide] = PositionSide.SHORT

    def evaluate(
        self,
        rows: Iterable[SignalRow],
        event_calendar: EventCalendar,
    ) -> tuple[SignalCandidate, ...]:
        admitted = self._admitted_rows(rows, event_calendar)
        if not admitted:
            return ()

        normalized = {
            field_name: _normalized_values(admitted, field_name)
            for field_name in _RANKING_FIELDS
        }
        ranked: list[tuple[float, str, dict[str, object], dict[str, float]]] = []
        for row in admitted:
            components = {
                field_name: round(normalized[field_name][id(row)], 6)
                for field_name in _RANKING_FIELDS
            }
            score = round(
                sum(components.values()) / len(_RANKING_FIELDS),
                6,
            )
            ranked.append((score, str(row["symbol"]), row, components))

        ranked.sort(key=lambda item: (-item[0], item[1]))
        candidates: list[SignalCandidate] = []
        used_symbols: set[str] = set()
        for score, symbol, row, components in ranked:
            if symbol in used_symbols:
                continue
            used_symbols.add(symbol)
            cutoff_date = row["cutoff_date"]
            thesis_id = self._thesis_id(symbol, str(cutoff_date))
            candidates.append(
                SignalCandidate(
                    symbol=symbol,
                    side=self.side,
                    score=score,
                    requested_weight_pct=REQUESTED_SHORT_WEIGHT_PCT,
                    model_id=self.model_id,
                    thesis_id=thesis_id,
                    facts={
                        "cutoff_date": cutoff_date,
                        "momentum20": row["momentum20"],
                        "momentum60": row["momentum60"],
                        "price": row["price"],
                        "ma20": row["ma20"],
                        "ma60": row["ma60"],
                        "volatility20": row["volatility20"],
                        "turnover": row["turnover"],
                        "one_day_return": row["one_day_return"],
                        "event_sessions": row["event_sessions"],
                        "ranking_score": score,
                        "ranking_components": components,
                    },
                )
            )
            if len(candidates) >= MAXIMUM_SHORT_SIGNALS:
                break
        return tuple(candidates)

    def _admitted_rows(
        self,
        rows: Iterable[SignalRow],
        event_calendar: EventCalendar,
    ) -> list[dict[str, object]]:
        if not isinstance(event_calendar, Mapping):
            return []
        maximum_volatility_pct = _finite_number(
            _policy_value(
                self.policy,
                "maximum_volatility_20d_pct",
                ShortPolicy().maximum_volatility_20d_pct,
            )
        )
        blackout_sessions = _policy_value(
            self.policy,
            "event_blackout_sessions",
            ShortPolicy().event_blackout_sessions,
        )
        if (
            maximum_volatility_pct is None
            or maximum_volatility_pct < 0
            or isinstance(blackout_sessions, bool)
            or not isinstance(blackout_sessions, int)
            or blackout_sessions < 0
        ):
            return []
        maximum_volatility = maximum_volatility_pct / 100.0

        admitted: list[dict[str, object]] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            symbol = str(raw.get("symbol") or "").strip()
            if not symbol or symbol not in event_calendar:
                continue
            event_sessions = event_calendar[symbol]
            if event_sessions is not None:
                if (
                    isinstance(event_sessions, bool)
                    or not isinstance(event_sessions, int)
                    or event_sessions <= blackout_sessions
                ):
                    continue

            values = {
                field_name: _finite_number(raw.get(field_name))
                for field_name in (
                    "momentum20",
                    "momentum60",
                    "price",
                    "ma20",
                    "ma60",
                    "volatility20",
                    "turnover",
                    "one_day_return",
                )
            }
            if any(value is None for value in values.values()):
                continue
            momentum20 = float(values["momentum20"])
            momentum60 = float(values["momentum60"])
            price = float(values["price"])
            ma20 = float(values["ma20"])
            ma60 = float(values["ma60"])
            volatility20 = float(values["volatility20"])
            turnover = float(values["turnover"])
            one_day_return = float(values["one_day_return"])
            cutoff = _date_value(raw.get("cutoff_date", raw.get("as_of")))
            if cutoff is None:
                continue
            if not (
                momentum20 < 0
                and momentum60 < 0
                and price > 0
                and ma20 > 0
                and ma60 > 0
                and price < ma20
                and price < ma60
                and 0 <= volatility20 <= maximum_volatility
                and turnover >= MINIMUM_DAILY_USD_TURNOVER
                and one_day_return > -0.10
            ):
                continue
            admitted.append(
                {
                    "symbol": symbol,
                    "cutoff_date": cutoff.isoformat(),
                    "momentum20": momentum20,
                    "momentum60": momentum60,
                    "price": price,
                    "ma20": ma20,
                    "ma60": ma60,
                    "volatility20": volatility20,
                    "turnover": turnover,
                    "one_day_return": one_day_return,
                    "event_sessions": event_sessions,
                    "negative_momentum_persistence": (
                        -momentum20 - momentum60
                    )
                    / 2.0,
                    "below_ma60_distance": (ma60 - price) / ma60,
                    "inverse_volatility": 1.0 / (1.0 + volatility20),
                    "liquidity": math.log1p(
                        turnover / MINIMUM_DAILY_USD_TURNOVER
                    ),
                }
            )
        return admitted

    def _thesis_id(self, symbol: str, cutoff_date: str) -> str:
        source = f"{self.model_id}|{symbol}|{cutoff_date}"
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
        return f"{self.model_id}:{symbol}:{cutoff_date}:{digest}"
