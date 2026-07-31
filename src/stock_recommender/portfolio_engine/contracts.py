"""Domain contracts for portfolio engine inputs and outputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from ..pipeline import StageOutput


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionEffect(str, Enum):
    OPEN = "OPEN"
    INCREASE = "INCREASE"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


@dataclass(frozen=True)
class SignalCandidate:
    symbol: str
    side: PositionSide
    score: float
    requested_weight_pct: float
    model_id: str
    thesis_id: str
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TargetPosition:
    symbol: str
    side: PositionSide
    target_weight_pct: float
    signal_score: float
    model_id: str
    thesis_id: str


def _require_positive_quantity(quantity: int) -> None:
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("quantity must be a positive integer")


@dataclass(frozen=True)
class OrderIntent:
    id: str
    symbol: str
    position_side: PositionSide
    order_side: OrderSide
    position_effect: PositionEffect
    quantity: int
    reason: str
    created_snapshot_id: str

    def __post_init__(self) -> None:
        _require_positive_quantity(self.quantity)

    @property
    def increases_risk(self) -> bool:
        return self.position_effect in {
            PositionEffect.OPEN,
            PositionEffect.INCREASE,
        }


@dataclass(frozen=True)
class MarketSnapshot:
    id: str
    occurred_at: datetime
    quotes: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class ExecutionFill:
    intent_id: str
    symbol: str
    quantity: int
    price: float
    fees: float
    status: str

    def __post_init__(self) -> None:
        _require_positive_quantity(self.quantity)


@dataclass(frozen=True)
class PortfolioEvent:
    id: str
    type: str
    occurred_at: datetime
    data: Mapping[str, Any]


@dataclass(frozen=True)
class DecisionBatch:
    run_key: str
    strategy_id: str
    strategy_revision: int
    portfolio_snapshot_id: str
    market_snapshot_id: str
    intents: tuple[OrderIntent, ...] = ()
    fills: tuple[ExecutionFill, ...] = ()
    events: tuple[PortfolioEvent, ...] = ()
    diagnostics: tuple[Mapping[str, Any], ...] = ()
    stage_outputs: tuple[StageOutput, ...] = ()

    @property
    def diagnostic_codes(self) -> tuple[str, ...]:
        return tuple(
            str(item["code"])
            for item in self.diagnostics
            if item.get("code")
        )
