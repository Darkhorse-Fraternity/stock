"""Ports for supplying investment signals to the portfolio engine."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Protocol, TypeAlias

from ..signal_engine import SIGNAL_MODEL_ID
from .contracts import (
    EventCalendar,
    PositionSide,
    SignalCandidate,
    SignalRow,
    normalize_cutoff_date,
)


class SignalModel(Protocol):
    model_id: str
    side: PositionSide

    def evaluate(
        self,
        rows: Iterable[SignalRow],
        event_calendar: EventCalendar,
    ) -> tuple[SignalCandidate, ...]: ...


@dataclass(frozen=True)
class SignalModelFactory:
    """Create an immutable model bound to one strategy's policy snapshot."""

    model_id: str
    side: PositionSide
    create: Callable[[object | None], SignalModel]

    def bind(self, policy: object | None) -> SignalModel:
        model = self.create(policy)
        if model.model_id != self.model_id or model.side is not self.side:
            raise ValueError("signal model factory returned a mismatched model")
        return model


SignalRegistryEntry: TypeAlias = SignalModel | SignalModelFactory
SIGNAL_MODELS: dict[str, SignalRegistryEntry] = {}


def register_signal_model(model: SignalModel) -> None:
    if model.model_id in SIGNAL_MODELS:
        raise ValueError(f"重复信号模型：{model.model_id}")
    SIGNAL_MODELS[model.model_id] = model


def register_signal_model_factory(factory: SignalModelFactory) -> None:
    if type(factory) is not SignalModelFactory:
        raise TypeError("factory must be SignalModelFactory")
    if factory.model_id in SIGNAL_MODELS:
        raise ValueError(f"重复信号模型：{factory.model_id}")
    SIGNAL_MODELS[factory.model_id] = factory


def resolve_signal_model(
    registry: Mapping[str, SignalRegistryEntry],
    model_id: str,
    *,
    policy: object | None = None,
) -> SignalModel:
    try:
        registered = registry[model_id]
    except KeyError:
        raise KeyError(f"未注册信号模型：{model_id}") from None
    if type(registered) is SignalModelFactory:
        return registered.bind(policy)
    return registered


def get_signal_model(model_id: str, *, policy: object | None = None) -> SignalModel:
    return resolve_signal_model(SIGNAL_MODELS, model_id, policy=policy)


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _selection_cutoff_date(row: Mapping[str, object]) -> str | None:
    values = [row.get("cutoff_date"), row.get("as_of")]
    signal_features = row.get("signal_features")
    if isinstance(signal_features, Mapping):
        values.append(signal_features.get("history_latest_date"))
    for value in values:
        cutoff = normalize_cutoff_date(value)
        if cutoff is not None:
            return cutoff
    return None


class FactorRankLongAdapter:
    """Adapt already-selected ``factor_rank_v1`` rows without reranking them."""

    model_id = SIGNAL_MODEL_ID
    side = PositionSide.LONG

    def __init__(self, requested_weight_pct: float = 10.0) -> None:
        self.requested_weight_pct = requested_weight_pct

    def evaluate(
        self,
        rows: Iterable[SignalRow],
        event_calendar: EventCalendar,
    ) -> tuple[SignalCandidate, ...]:
        del event_calendar
        candidates: list[SignalCandidate] = []
        for raw in rows:
            try:
                row = dict(raw)
            except (TypeError, ValueError):
                continue
            selected_for_long = row.get("selected_for_long", True)
            if type(selected_for_long) is not bool or not selected_for_long:
                continue
            symbol = str(row.get("symbol") or "").strip()
            score = _finite_number(row.get("score", row.get("signal_score")))
            requested_weight = _finite_number(
                row.get("requested_weight_pct", self.requested_weight_pct)
            )
            if not symbol or score is None or requested_weight is None:
                continue
            thesis_id = str(row.get("thesis_id") or "").strip()
            if not thesis_id:
                cutoff = _selection_cutoff_date(row)
                if cutoff is None:
                    continue
                thesis_id = f"{self.model_id}:{symbol}:{cutoff}"
            candidates.append(
                SignalCandidate(
                    symbol=symbol,
                    side=self.side,
                    score=score,
                    requested_weight_pct=requested_weight,
                    model_id=self.model_id,
                    thesis_id=thesis_id,
                    facts=row,
                )
            )
        return tuple(candidates)


register_signal_model(FactorRankLongAdapter())

# Import the built-in implementation only after the registry API is complete.
# This gives direct ``signal_ports`` imports the same deterministic defaults as
# package imports while keeping duplicate registration explicit.
from .short_signal import ShortTrendBreakdownV1  # noqa: E402


def _create_short_trend_model(policy: object | None) -> SignalModel:
    return ShortTrendBreakdownV1() if policy is None else ShortTrendBreakdownV1(policy=policy)


register_signal_model_factory(
    SignalModelFactory(
        model_id=ShortTrendBreakdownV1.model_id,
        side=ShortTrendBreakdownV1.side,
        create=_create_short_trend_model,
    )
)
