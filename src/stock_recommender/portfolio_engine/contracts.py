"""Domain contracts for portfolio engine inputs and outputs."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, TypeAlias

from ..pipeline import StageOutput


SignalRow: TypeAlias = Mapping[str, Any]
EventCalendar: TypeAlias = Mapping[str, int | None]
_DEEPLY_IMMUTABLE_TYPES: tuple[type[Any], ...] = ()


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


def freeze_immutable(value: Any) -> Any:
    """Defensively copy a supported plain value into an immutable graph."""

    return _deep_freeze(value)


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
    if value is None or type(value) in {
        bool,
        int,
        float,
        str,
        bytes,
        date,
        datetime,
    } or type(value) in _DEEPLY_IMMUTABLE_TYPES:
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


def _require_string(value: object, field_name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    if not value:
        raise ValueError(f"{field_name} must not be empty")


def _require_enum(value: object, enum_type: type[Enum], field_name: str) -> None:
    if type(value) is not enum_type:
        raise TypeError(f"{field_name} must be {enum_type.__name__}")


def _require_finite_number(value: object, field_name: str) -> None:
    if type(value) not in (int, float):
        raise TypeError(f"{field_name} must be an int or float")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")


def _require_positive_finite_number(value: object, field_name: str) -> None:
    _require_finite_number(value, field_name)
    if value <= 0:  # type: ignore[operator]
        raise ValueError(f"{field_name} must be positive")


def _require_nonnegative_finite_number(value: object, field_name: str) -> None:
    _require_finite_number(value, field_name)
    if value < 0:  # type: ignore[operator]
        raise ValueError(f"{field_name} must be nonnegative")


def _require_derived_metric(value: object, field_name: str) -> None:
    if type(value) not in (int, float):
        raise TypeError(f"{field_name} must be an int or float")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must not be NaN") from exc
    if math.isnan(number):
        raise ValueError(f"{field_name} must not be NaN")


def _finite_derived_value(value: object, field_name: str) -> float:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    if number == 0:
        return 0.0
    return number


def _expected_ratio(numerator: float, denominator: float, field_name: str) -> float:
    try:
        ratio = numerator / denominator * 100.0
    except (OverflowError, ZeroDivisionError) as exc:
        raise ValueError(f"{field_name} calculation overflowed") from exc
    return _finite_derived_value(ratio, field_name)


def _metric_matches(actual: int | float, expected: float) -> bool:
    if math.isinf(expected):
        return actual == expected
    return math.isclose(
        float(actual),
        expected,
        rel_tol=1e-12,
        abs_tol=0.0,
    )


def _require_integer(value: object, field_name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer")


def _require_datetime(value: object, field_name: str) -> None:
    if type(value) is not datetime:
        raise TypeError(f"{field_name} must be a datetime")


def _require_mapping(value: object, field_name: str) -> None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")


def _typed_tuple(
    value: object,
    item_type: type[Any],
    field_name: str,
) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise TypeError(f"{field_name} must be an iterable of {item_type.__name__}")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError(
            f"{field_name} must be an iterable of {item_type.__name__}"
        ) from exc
    for item in items:
        if type(item) is not item_type:
            raise TypeError(
                f"{field_name} items must be {item_type.__name__}, got "
                f"{type(item).__name__}"
            )
    return items


def _mapping_tuple(value: object, field_name: str) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise TypeError(f"{field_name} must be an iterable of mappings")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError(f"{field_name} must be an iterable of mappings") from exc
    for item in items:
        if not isinstance(item, Mapping):
            raise TypeError(
                f"{field_name} items must be mappings, got {type(item).__name__}"
            )
    return items


def _freeze_stage_output(output: StageOutput) -> StageOutput:
    if type(output) is not StageOutput:
        raise TypeError(
            "stage_outputs items must be StageOutput, got "
            f"{type(output).__name__}"
        )
    _require_string(output.stage, "stage_output.stage")
    _require_string(output.component_version, "stage_output.component_version")
    _require_integer(output.schema_version, "stage_output.schema_version")
    facts = _mapping_tuple(output.facts, "stage_output.facts")
    diagnostics = _mapping_tuple(
        output.diagnostics,
        "stage_output.diagnostics",
    )
    return StageOutput(
        stage=output.stage,
        component_version=output.component_version,
        schema_version=output.schema_version,
        facts=tuple(_deep_freeze(item) for item in facts),
        diagnostics=tuple(_deep_freeze(item) for item in diagnostics),
    )


class _DeeplyImmutable:
    """Mark recursively frozen domain values as safe to reuse on deepcopy."""

    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, object]) -> _DeeplyImmutable:
        memo[id(self)] = self
        return self


@dataclass(frozen=True)
class PositionSnapshot(_DeeplyImmutable):
    symbol: str
    side: PositionSide
    quantity: int
    average_cost: float
    current_price: float | None = None
    peak_price: float | None = None
    trough_price: float | None = None
    trailing_active: bool = False
    position_mode: str = "NORMAL"

    def __post_init__(self) -> None:
        _require_string(self.symbol, "symbol")
        _require_enum(self.side, PositionSide, "side")
        _require_positive_quantity(self.quantity)
        _require_positive_finite_number(self.average_cost, "average_cost")
        if self.current_price is not None:
            _require_positive_finite_number(self.current_price, "current_price")
        if self.peak_price is not None:
            _require_positive_finite_number(self.peak_price, "peak_price")
        if self.trough_price is not None:
            _require_positive_finite_number(self.trough_price, "trough_price")
        if type(self.trailing_active) is not bool:
            raise TypeError("trailing_active must be a bool")
        if self.position_mode not in {"NORMAL", "COVER_ONLY"}:
            raise ValueError(f"unsupported position_mode: {self.position_mode}")

    @property
    def market_value(self) -> float | None:
        if self.current_price is None:
            return None
        try:
            value = self.quantity * self.current_price
        except OverflowError as exc:
            raise ValueError("market_value calculation overflowed") from exc
        return _finite_derived_value(value, "market_value")

    @property
    def unrealized_pnl(self) -> float | None:
        if self.current_price is None:
            return None
        direction = 1.0 if self.side is PositionSide.LONG else -1.0
        try:
            pnl = (
                direction
                * (self.current_price - self.average_cost)
                * self.quantity
            )
        except OverflowError as exc:
            raise ValueError("unrealized_pnl calculation overflowed") from exc
        return _finite_derived_value(pnl, "unrealized_pnl")


@dataclass(frozen=True)
class AccountSnapshot(_DeeplyImmutable):
    id: str
    strategy_id: str
    strategy_revision: int
    occurred_at: datetime
    available_cash: float
    restricted_short_proceeds: float = 0.0
    margin_loan: float = 0.0
    accrued_financing_cost: float = 0.0
    accrued_borrow_cost: float = 0.0
    positions: tuple[PositionSnapshot, ...] = ()

    def __post_init__(self) -> None:
        _require_string(self.id, "id")
        _require_string(self.strategy_id, "strategy_id")
        _require_integer(self.strategy_revision, "strategy_revision")
        _require_datetime(self.occurred_at, "occurred_at")
        _require_finite_number(self.available_cash, "available_cash")
        _require_nonnegative_finite_number(
            self.restricted_short_proceeds,
            "restricted_short_proceeds",
        )
        _require_nonnegative_finite_number(self.margin_loan, "margin_loan")
        _require_nonnegative_finite_number(
            self.accrued_financing_cost,
            "accrued_financing_cost",
        )
        _require_nonnegative_finite_number(
            self.accrued_borrow_cost,
            "accrued_borrow_cost",
        )
        positions = _typed_tuple(self.positions, PositionSnapshot, "positions")
        seen_symbols: set[str] = set()
        for position in positions:
            if position.symbol in seen_symbols:
                raise ValueError(
                    "positions must not contain duplicate or opposite-side "
                    f"symbol: {position.symbol}"
                )
            seen_symbols.add(position.symbol)
        object.__setattr__(self, "positions", positions)


@dataclass(frozen=True)
class PortfolioMetrics(_DeeplyImmutable):
    available_cash: float
    restricted_short_proceeds: float
    margin_loan: float
    accrued_financing_cost: float
    accrued_borrow_cost: float
    long_market_value: float
    short_liability: float
    equity: float
    long_exposure_pct: float
    short_exposure_pct: float
    gross_exposure_pct: float
    net_exposure_pct: float
    margin_rate_pct: float

    def __post_init__(self) -> None:
        _require_finite_number(self.available_cash, "available_cash")
        for field_name in (
            "restricted_short_proceeds",
            "margin_loan",
            "accrued_financing_cost",
            "accrued_borrow_cost",
            "long_market_value",
            "short_liability",
        ):
            _require_nonnegative_finite_number(
                getattr(self, field_name),
                field_name,
            )
        _require_finite_number(self.equity, "equity")
        for field_name in (
            "long_exposure_pct",
            "short_exposure_pct",
            "gross_exposure_pct",
            "net_exposure_pct",
            "margin_rate_pct",
        ):
            _require_derived_metric(getattr(self, field_name), field_name)

        available_cash = float(self.available_cash)
        restricted_proceeds = float(self.restricted_short_proceeds)
        long_value = float(self.long_market_value)
        short_value = float(self.short_liability)
        margin_loan = float(self.margin_loan)
        try:
            expected_equity = (
                available_cash
                + restricted_proceeds
                + long_value
                - short_value
                - margin_loan
            )
            gross_value = long_value + short_value
        except OverflowError as exc:
            raise ValueError("portfolio metric base calculation overflowed") from exc
        expected_equity = _finite_derived_value(expected_equity, "equity")
        gross_value = _finite_derived_value(gross_value, "gross_market_value")
        if not _metric_matches(self.equity, expected_equity):
            raise ValueError("equity is inconsistent with account balances")

        if gross_value == 0:
            expected_ratios = (0.0, 0.0, 0.0, 0.0, math.inf)
        elif expected_equity == 0:
            net_value = long_value - short_value
            expected_ratios = (
                math.inf if long_value > 0 else 0.0,
                math.inf if short_value > 0 else 0.0,
                math.inf,
                (
                    math.inf
                    if net_value > 0
                    else -math.inf
                    if net_value < 0
                    else 0.0
                ),
                0.0,
            )
        else:
            expected_ratios = (
                _expected_ratio(long_value, expected_equity, "long_exposure_pct"),
                _expected_ratio(short_value, expected_equity, "short_exposure_pct"),
                _expected_ratio(gross_value, expected_equity, "gross_exposure_pct"),
                _expected_ratio(
                    long_value - short_value,
                    expected_equity,
                    "net_exposure_pct",
                ),
                _expected_ratio(expected_equity, gross_value, "margin_rate_pct"),
            )
        ratio_fields = (
            "long_exposure_pct",
            "short_exposure_pct",
            "gross_exposure_pct",
            "net_exposure_pct",
            "margin_rate_pct",
        )
        for field_name, expected in zip(ratio_fields, expected_ratios, strict=True):
            if not _metric_matches(getattr(self, field_name), expected):
                raise ValueError(f"{field_name} is inconsistent with portfolio values")

        for field_name in self.__dataclass_fields__:
            if getattr(self, field_name) == 0:
                object.__setattr__(self, field_name, 0.0)


@dataclass(frozen=True)
class ValuationResult(_DeeplyImmutable):
    account: AccountSnapshot
    metrics: PortfolioMetrics
    positions: tuple[PositionSnapshot, ...] = ()

    def __post_init__(self) -> None:
        if type(self.account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        if type(self.metrics) is not PortfolioMetrics:
            raise TypeError("metrics must be PortfolioMetrics")
        positions = tuple(
            position
            for position in _typed_tuple(
                self.positions,
                PositionSnapshot,
                "positions",
            )
        )
        object.__setattr__(self, "positions", positions)

        balance_fields = (
            "available_cash",
            "restricted_short_proceeds",
            "margin_loan",
            "accrued_financing_cost",
            "accrued_borrow_cost",
        )
        for field_name in balance_fields:
            if getattr(self.account, field_name) != getattr(self.metrics, field_name):
                raise ValueError(
                    f"account.{field_name} does not match metrics.{field_name}"
                )

        if len(positions) != len(self.account.positions):
            raise ValueError("positions length does not match account.positions")
        identity_fields = (
            "symbol",
            "side",
            "quantity",
            "average_cost",
            "peak_price",
            "trough_price",
            "trailing_active",
            "position_mode",
        )
        for index, (account_position, valued_position) in enumerate(
            zip(self.account.positions, positions, strict=True)
        ):
            if valued_position.current_price is None:
                raise ValueError(f"positions[{index}].current_price must be valued")
            for field_name in identity_fields:
                if getattr(account_position, field_name) != getattr(
                    valued_position,
                    field_name,
                ):
                    raise ValueError(
                        f"positions[{index}].{field_name} does not match "
                        f"account.positions[{index}].{field_name}"
                    )

        long_values: list[float] = []
        short_values: list[float] = []
        for index, position in enumerate(positions):
            market_value = position.market_value
            if market_value is None:
                raise ValueError(f"positions[{index}].market_value is unavailable")
            if position.side is PositionSide.LONG:
                long_values.append(market_value)
            else:
                short_values.append(market_value)
        long_market_value = _finite_derived_value(
            sum(long_values, 0.0),
            "long_market_value",
        )
        short_liability = _finite_derived_value(
            sum(short_values, 0.0),
            "short_liability",
        )
        if not _metric_matches(
            self.metrics.long_market_value,
            long_market_value,
        ):
            raise ValueError(
                "positions long_market_value does not match metrics.long_market_value"
            )
        if not _metric_matches(self.metrics.short_liability, short_liability):
            raise ValueError(
                "positions short_liability does not match metrics.short_liability"
            )


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
        _require_string(self.symbol, "symbol")
        _require_enum(self.side, PositionSide, "side")
        _require_finite_number(self.score, "score")
        _require_finite_number(
            self.requested_weight_pct,
            "requested_weight_pct",
        )
        _require_string(self.model_id, "model_id")
        _require_string(self.thesis_id, "thesis_id")
        _require_mapping(self.facts, "facts")
        object.__setattr__(self, "facts", _deep_freeze(self.facts))


@dataclass(frozen=True)
class TargetPosition(_DeeplyImmutable):
    symbol: str
    side: PositionSide
    target_weight_pct: float
    signal_score: float
    model_id: str
    thesis_id: str

    def __post_init__(self) -> None:
        _require_string(self.symbol, "symbol")
        _require_enum(self.side, PositionSide, "side")
        _require_finite_number(self.target_weight_pct, "target_weight_pct")
        _require_finite_number(self.signal_score, "signal_score")
        _require_string(self.model_id, "model_id")
        _require_string(self.thesis_id, "thesis_id")


def _require_positive_quantity(quantity: object) -> None:
    if type(quantity) is not int:
        raise TypeError("quantity must be an integer")
    if quantity <= 0:
        raise ValueError("quantity must be positive")


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
        _require_string(self.id, "id")
        _require_string(self.symbol, "symbol")
        _require_enum(self.position_side, PositionSide, "position_side")
        _require_enum(self.order_side, OrderSide, "order_side")
        _require_enum(self.position_effect, PositionEffect, "position_effect")
        _require_positive_quantity(self.quantity)
        _require_string(self.reason, "reason")
        _require_string(self.created_snapshot_id, "created_snapshot_id")

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
        _require_string(self.id, "id")
        _require_datetime(self.occurred_at, "occurred_at")
        _require_mapping(self.quotes, "quotes")
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
        _require_string(self.intent_id, "intent_id")
        _require_string(self.symbol, "symbol")
        _require_positive_quantity(self.quantity)
        _require_finite_number(self.price, "price")
        _require_finite_number(self.fees, "fees")
        _require_string(self.status, "status")


@dataclass(frozen=True)
class PortfolioEvent(_DeeplyImmutable):
    id: str
    type: str
    occurred_at: datetime
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        _require_string(self.id, "id")
        _require_string(self.type, "type")
        _require_datetime(self.occurred_at, "occurred_at")
        _require_mapping(self.data, "data")
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
        _require_string(self.run_key, "run_key")
        _require_string(self.strategy_id, "strategy_id")
        _require_integer(self.strategy_revision, "strategy_revision")
        _require_string(self.portfolio_snapshot_id, "portfolio_snapshot_id")
        _require_string(self.market_snapshot_id, "market_snapshot_id")
        intents = _typed_tuple(self.intents, OrderIntent, "intents")
        fills = _typed_tuple(self.fills, ExecutionFill, "fills")
        events = _typed_tuple(self.events, PortfolioEvent, "events")
        diagnostics = _mapping_tuple(self.diagnostics, "diagnostics")
        stage_outputs = _typed_tuple(
            self.stage_outputs,
            StageOutput,
            "stage_outputs",
        )
        object.__setattr__(self, "intents", intents)
        object.__setattr__(self, "fills", fills)
        object.__setattr__(self, "events", events)
        object.__setattr__(
            self,
            "diagnostics",
            tuple(_deep_freeze(item) for item in diagnostics),
        )
        object.__setattr__(
            self,
            "stage_outputs",
            tuple(_freeze_stage_output(item) for item in stage_outputs),
        )

    @property
    def diagnostic_codes(self) -> tuple[str, ...]:
        return tuple(
            str(item["code"])
            for item in self.diagnostics
            if item.get("code")
        )


_DEEPLY_IMMUTABLE_TYPES = (
    PositionSnapshot,
    AccountSnapshot,
    PortfolioMetrics,
    ValuationResult,
    SignalCandidate,
    TargetPosition,
    OrderIntent,
    MarketSnapshot,
    ExecutionFill,
    PortfolioEvent,
    DecisionBatch,
)
