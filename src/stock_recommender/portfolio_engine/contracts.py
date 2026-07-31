"""Domain contracts for portfolio engine inputs and outputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, TypeAlias

from ..pipeline import StageOutput


SignalRow: TypeAlias = Mapping[str, Any]
EventCalendar: TypeAlias = Mapping[str, int | None]


def normalize_cutoff_date(value: object) -> str | None:
    """Return an input-local ISO date without consulting a clock or timezone."""

    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text).isoformat()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.date().isoformat()
    except ValueError:
        return None


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


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def _freeze_stage_output(output: StageOutput) -> StageOutput:
    return StageOutput(
        stage=output.stage,
        component_version=output.component_version,
        schema_version=output.schema_version,
        facts=tuple(_deep_freeze(item) for item in output.facts),
        diagnostics=tuple(_deep_freeze(item) for item in output.diagnostics),
    )


class _DeeplyImmutable:
    """Mark recursively frozen domain values as safe to reuse on deepcopy."""

    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, object]) -> _DeeplyImmutable:
        memo[id(self)] = self
        return self


@dataclass(frozen=True)
class SignalCandidate(_DeeplyImmutable):
    symbol: str
    side: PositionSide
    score: float
    requested_weight_pct: float
    model_id: str
    thesis_id: str
    facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", _deep_freeze(self.facts))


@dataclass(frozen=True)
class TargetPosition(_DeeplyImmutable):
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
class OrderIntent(_DeeplyImmutable):
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
class MarketSnapshot(_DeeplyImmutable):
    id: str
    occurred_at: datetime
    quotes: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "quotes", _deep_freeze(self.quotes))


@dataclass(frozen=True)
class ExecutionFill(_DeeplyImmutable):
    intent_id: str
    symbol: str
    quantity: int
    price: float
    fees: float
    status: str

    def __post_init__(self) -> None:
        _require_positive_quantity(self.quantity)


@dataclass(frozen=True)
class PortfolioEvent(_DeeplyImmutable):
    id: str
    type: str
    occurred_at: datetime
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", _deep_freeze(self.data))


@dataclass(frozen=True)
class DecisionBatch(_DeeplyImmutable):
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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "diagnostics",
            tuple(_deep_freeze(item) for item in self.diagnostics),
        )
        object.__setattr__(
            self,
            "stage_outputs",
            tuple(_freeze_stage_output(item) for item in self.stage_outputs),
        )

    @property
    def diagnostic_codes(self) -> tuple[str, ...]:
        return tuple(
            str(item["code"])
            for item in self.diagnostics
            if item.get("code")
        )
