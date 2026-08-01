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


def _unsupported_immutable_type(value: object) -> TypeError:
    return TypeError(
        "unsupported mutable or unknown immutable value type: "
        f"{type(value).__name__}"
    )


def _deep_freeze(value: Any) -> Any:
    """Recursively copy supported values into a provably immutable graph."""

    return _deep_freeze_value(value, set())


def _assert_deeply_immutable_enum_value(member: Enum) -> None:
    """Reject Enum members whose original value graph contains mutable data."""

    owner_name = type(member).__name__

    def visit(value: Any, active: set[int]) -> None:
        if value is None or type(value) in {
            bool,
            int,
            float,
            str,
            bytes,
            date,
            datetime,
        }:
            return
        if isinstance(value, Enum):
            identity = id(value)
            if identity in active:
                raise TypeError(f"Enum {owner_name} contains a cyclic Enum value")
            active.add(identity)
            try:
                visit(value.value, active)
            finally:
                active.remove(identity)
            return
        if type(value) in {tuple, frozenset}:
            identity = id(value)
            if identity in active:
                raise TypeError(f"Enum {owner_name} contains a cyclic value")
            active.add(identity)
            try:
                for item in value:
                    visit(item, active)
            finally:
                active.remove(identity)
            return
        raise TypeError(
            f"Enum {owner_name} has mutable or unsupported value type: "
            f"{type(value).__name__}"
        )

    visit(member.value, {id(member)})


def _deep_freeze_value(value: Any, active: set[int]) -> Any:
    if isinstance(value, Enum):
        _assert_deeply_immutable_enum_value(value)
        return value
    if value is None or isinstance(
        value,
        (bool, int, float, str, bytes, date, datetime, _DeeplyImmutable),
    ):
        return value
    if isinstance(value, (bytearray, memoryview)):
        try:
            return bytes(value)
        except (TypeError, ValueError) as exc:
            raise _unsupported_immutable_type(value) from exc
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise TypeError(f"cyclic mutable container type: {type(value).__name__}")
        active.add(identity)
        try:
            frozen: dict[Any, Any] = {}
            for raw_key, raw_item in value.items():
                key = _deep_freeze_value(raw_key, active)
                try:
                    hash(key)
                except TypeError as exc:
                    raise TypeError(
                        "mapping key cannot be frozen as hashable: "
                        f"{type(raw_key).__name__}"
                    ) from exc
                if key in frozen:
                    raise TypeError(
                        "mapping keys collide after immutable conversion: "
                        f"{type(raw_key).__name__}"
                    )
                frozen[key] = _deep_freeze_value(raw_item, active)
            return MappingProxyType(frozen)
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise TypeError(f"cyclic mutable container type: {type(value).__name__}")
        active.add(identity)
        try:
            return tuple(_deep_freeze_value(item, active) for item in value)
        finally:
            active.remove(identity)
    if isinstance(value, (set, frozenset)):
        identity = id(value)
        if identity in active:
            raise TypeError(f"cyclic mutable container type: {type(value).__name__}")
        active.add(identity)
        try:
            frozen_items: list[Any] = []
            for raw_item in value:
                item = _deep_freeze_value(raw_item, active)
                try:
                    hash(item)
                except TypeError as exc:
                    raise TypeError(
                        "set item cannot be frozen as hashable: "
                        f"{type(raw_item).__name__}"
                    ) from exc
                frozen_items.append(item)
            return frozenset(frozen_items)
        finally:
            active.remove(identity)
    raise _unsupported_immutable_type(value)


def _deep_thaw(value: Any) -> Any:
    """Project a frozen graph into deepcopy-compatible pipeline facts."""

    if isinstance(value, Mapping):
        return {
            _deep_thaw(key): _deep_thaw(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_deep_thaw(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(_deep_thaw(item) for item in value)
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
