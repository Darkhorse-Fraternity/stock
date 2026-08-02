"""Domain contracts for portfolio engine inputs and outputs."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, TypeAlias

from ..markets import strict_strategy_market
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


def _require_position_risk_tracking(
    side: PositionSide,
    peak_price: object,
    trough_price: object,
    trailing_active: object,
    position_mode: object,
) -> None:
    if peak_price is not None:
        _require_positive_finite_number(peak_price, "peak_price")
    if trough_price is not None:
        _require_positive_finite_number(trough_price, "trough_price")
    if type(trailing_active) is not bool:
        raise TypeError("trailing_active must be a bool")
    if position_mode not in {"NORMAL", "COVER_ONLY"}:
        raise ValueError(f"unsupported position_mode: {position_mode}")
    if side is PositionSide.LONG:
        if trough_price is not None:
            raise ValueError("LONG position risk state must not have trough_price")
        trailing_anchor = peak_price
        anchor_name = "peak_price"
    else:
        if peak_price is not None:
            raise ValueError("SHORT position risk state must not have peak_price")
        trailing_anchor = trough_price
        anchor_name = "trough_price"
    if trailing_active and trailing_anchor is None:
        raise ValueError(f"active {side.value} trailing state requires {anchor_name}")


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
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


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
class AccrualLifecycle(_DeeplyImmutable):
    id: str
    started_on: date

    def __post_init__(self) -> None:
        _require_string(self.id, "id")
        if type(self.started_on) is not date:
            raise TypeError("started_on must be a date")


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
    sellable_quantity: int | None = None
    sellable_on: date | None = None
    borrow_lifecycle: AccrualLifecycle | None = None

    def __post_init__(self) -> None:
        _require_string(self.symbol, "symbol")
        _require_enum(self.side, PositionSide, "side")
        _require_positive_quantity(self.quantity)
        _require_positive_finite_number(self.average_cost, "average_cost")
        if self.current_price is not None:
            _require_positive_finite_number(self.current_price, "current_price")
        _require_position_risk_tracking(
            self.side,
            self.peak_price,
            self.trough_price,
            self.trailing_active,
            self.position_mode,
        )
        if self.sellable_quantity is not None:
            _require_integer(self.sellable_quantity, "sellable_quantity")
            if not 0 <= self.sellable_quantity <= self.quantity:
                raise ValueError(
                    "sellable_quantity must be between zero and quantity"
                )
        if self.sellable_on is not None and type(self.sellable_on) is not date:
            raise TypeError("sellable_on must be a date or None")
        if self.side is PositionSide.SHORT and (
            self.sellable_quantity is not None or self.sellable_on is not None
        ):
            raise ValueError("SHORT position must not carry long T+1 state")
        if self.borrow_lifecycle is not None:
            if type(self.borrow_lifecycle) is not AccrualLifecycle:
                raise TypeError("borrow_lifecycle must be AccrualLifecycle or None")
            if self.side is not PositionSide.SHORT:
                raise ValueError("only SHORT positions may have borrow_lifecycle")

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
class PositionRiskUpdate(_DeeplyImmutable):
    symbol: str
    side: PositionSide
    peak_price: float | None
    trough_price: float | None
    trailing_active: bool
    position_mode: str

    def __post_init__(self) -> None:
        _require_string(self.symbol, "symbol")
        _require_enum(self.side, PositionSide, "side")
        _require_position_risk_tracking(
            self.side,
            self.peak_price,
            self.trough_price,
            self.trailing_active,
            self.position_mode,
        )

    @classmethod
    def from_position(cls, position: PositionSnapshot) -> PositionRiskUpdate:
        if type(position) is not PositionSnapshot:
            raise TypeError("position must be PositionSnapshot")
        return cls(
            symbol=position.symbol,
            side=position.side,
            peak_price=position.peak_price,
            trough_price=position.trough_price,
            trailing_active=position.trailing_active,
            position_mode=position.position_mode,
        )


@dataclass(frozen=True)
class PositionSettlementUpdate(_DeeplyImmutable):
    """Minimal execution-owned position settlement state.

    Prices and risk fields are intentionally absent: fills prove prices and only
    ``PositionRiskUpdate`` may change risk tracking.
    """

    symbol: str
    side: PositionSide
    quantity: int
    sellable_quantity: int | None
    sellable_on: date | None

    def __post_init__(self) -> None:
        _require_string(self.symbol, "symbol")
        _require_enum(self.side, PositionSide, "side")
        _require_positive_quantity(self.quantity)
        if self.sellable_quantity is not None:
            _require_integer(self.sellable_quantity, "sellable_quantity")
            if not 0 <= self.sellable_quantity <= self.quantity:
                raise ValueError(
                    "sellable_quantity must be between zero and quantity"
                )
        if self.sellable_on is not None and type(self.sellable_on) is not date:
            raise TypeError("sellable_on must be a date or None")
        if self.side is PositionSide.SHORT and (
            self.sellable_quantity is not None or self.sellable_on is not None
        ):
            raise ValueError("SHORT settlement update must not carry long T+1 state")

    @classmethod
    def from_position(
        cls, position: PositionSnapshot
    ) -> PositionSettlementUpdate:
        if type(position) is not PositionSnapshot:
            raise TypeError("position must be PositionSnapshot")
        return cls(
            symbol=position.symbol,
            side=position.side,
            quantity=position.quantity,
            sellable_quantity=position.sellable_quantity,
            sellable_on=position.sellable_on,
        )


class CarryCostType(str, Enum):
    FINANCING = "FINANCING"
    BORROW = "BORROW"


@dataclass(frozen=True)
class CarryAccrualRecord(_DeeplyImmutable):
    account_id: str
    cost_type: CarryCostType
    accrual_date: date
    elapsed_days: int
    amount: float
    lifecycle_id: str
    symbol: str | None = None

    def __post_init__(self) -> None:
        _require_string(self.account_id, "account_id")
        _require_enum(self.cost_type, CarryCostType, "cost_type")
        if type(self.accrual_date) is not date:
            raise TypeError("accrual_date must be a date")
        _require_integer(self.elapsed_days, "elapsed_days")
        if self.elapsed_days < 0:
            raise ValueError("elapsed_days must be nonnegative")
        _require_nonnegative_finite_number(self.amount, "amount")
        _require_string(self.lifecycle_id, "lifecycle_id")
        if self.cost_type is CarryCostType.FINANCING:
            if self.symbol is not None:
                raise ValueError("FINANCING accrual must not have a symbol")
        else:
            _require_string(self.symbol, "symbol")

    @property
    def idempotency_key(
        self,
    ) -> tuple[str, CarryCostType, str, date, str | None]:
        return (
            self.account_id,
            self.cost_type,
            self.lifecycle_id,
            self.accrual_date,
            self.symbol,
        )


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
    carry_accruals: tuple[CarryAccrualRecord, ...] = ()
    financing_lifecycle: AccrualLifecycle | None = None
    reserved_cash: float = 0.0
    snapshot_id: str | None = None

    def __post_init__(self) -> None:
        _require_string(self.id, "id")
        _require_string(self.strategy_id, "strategy_id")
        _require_integer(self.strategy_revision, "strategy_revision")
        _require_datetime(self.occurred_at, "occurred_at")
        _require_finite_number(self.available_cash, "available_cash")
        _require_nonnegative_finite_number(self.reserved_cash, "reserved_cash")
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
        carry_accruals = _typed_tuple(
            self.carry_accruals,
            CarryAccrualRecord,
            "carry_accruals",
        )
        if self.financing_lifecycle is not None:
            if type(self.financing_lifecycle) is not AccrualLifecycle:
                raise TypeError(
                    "financing_lifecycle must be AccrualLifecycle or None"
                )
            if self.margin_loan == 0:
                raise ValueError(
                    "financing_lifecycle requires a positive margin_loan"
                )
        if self.snapshot_id is not None:
            _require_string(self.snapshot_id, "snapshot_id")
        keys: set[tuple[str, CarryCostType, str, date, str | None]] = set()
        for record in carry_accruals:
            if record.account_id != self.id:
                raise ValueError("carry accrual account_id must match account id")
            if record.idempotency_key in keys:
                raise ValueError("carry_accruals must have unique idempotency keys")
            keys.add(record.idempotency_key)
        seen_symbols: set[str] = set()
        for position in positions:
            if position.symbol in seen_symbols:
                raise ValueError(
                    "positions must not contain duplicate or opposite-side "
                    f"symbol: {position.symbol}"
                )
            seen_symbols.add(position.symbol)
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "carry_accruals", carry_accruals)


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
            "sellable_quantity",
            "sellable_on",
            "borrow_lifecycle",
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
    created_market_at: datetime

    def __post_init__(self) -> None:
        _require_string(self.id, "id")
        _require_string(self.symbol, "symbol")
        _require_enum(self.position_side, PositionSide, "position_side")
        _require_enum(self.order_side, OrderSide, "order_side")
        _require_enum(self.position_effect, PositionEffect, "position_effect")
        _require_positive_quantity(self.quantity)
        _require_string(self.reason, "reason")
        _require_string(self.created_snapshot_id, "created_snapshot_id")
        _require_datetime(self.created_market_at, "created_market_at")

    @property
    def increases_risk(self) -> bool:
        return self.position_effect in {
            PositionEffect.OPEN,
            PositionEffect.INCREASE,
        }

    def semantic_tuple(self) -> tuple[str, str, str]:
        """Return the complete direction/order/effect execution meaning."""

        return (
            self.position_side.value,
            self.order_side.value,
            self.position_effect.value,
        )


def stable_execution_intent_id(
    *,
    symbol: str,
    position_side: PositionSide,
    order_side: OrderSide,
    position_effect: PositionEffect,
    quantity: int,
    reason: str,
    created_snapshot_id: str,
    created_market_at: datetime,
) -> str:
    """Bind an execution-generated ID to every executable intent semantic."""

    _require_datetime(created_market_at, "created_market_at")
    material = "|".join(
        (
            created_snapshot_id,
            created_market_at.isoformat(),
            symbol,
            position_side.value,
            order_side.value,
            position_effect.value,
            str(quantity),
            reason,
        )
    )
    return "exec-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def stable_risk_intent_id(
    snapshot_id: str,
    position: PositionSnapshot,
    reason: str,
    *,
    created_market_at: datetime,
) -> str:
    """Return the established stable ID for a position-level risk exit."""

    _require_datetime(created_market_at, "created_market_at")
    material = "|".join(
        (
            snapshot_id,
            created_market_at.isoformat(),
            position.symbol,
            position.side.value,
            str(position.quantity),
            format(position.average_cost, ".17g"),
            reason,
        )
    )
    return "risk-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def verify_order_intent_id(
    intent: OrderIntent,
    existing_position: PositionSnapshot | None = None,
) -> bool:
    """Verify execution or risk intent identity without trusting mutable context."""

    if type(intent) is not OrderIntent:
        raise TypeError("intent must be OrderIntent")
    if (
        existing_position is not None
        and type(existing_position) is not PositionSnapshot
    ):
        raise TypeError("existing_position must be PositionSnapshot or None")
    if intent.id.startswith("exec-"):
        return intent.id == stable_execution_intent_id(
            symbol=intent.symbol,
            position_side=intent.position_side,
            order_side=intent.order_side,
            position_effect=intent.position_effect,
            quantity=intent.quantity,
            reason=intent.reason,
            created_snapshot_id=intent.created_snapshot_id,
            created_market_at=intent.created_market_at,
        )
    if not intent.id.startswith("risk-") or existing_position is None:
        return False
    expected_order_side = (
        OrderSide.SELL
        if existing_position.side is PositionSide.LONG
        else OrderSide.BUY
    )
    return (
        existing_position.symbol == intent.symbol
        and existing_position.side is intent.position_side
        and intent.order_side is expected_order_side
        and intent.position_effect is PositionEffect.CLOSE
        and intent.quantity == existing_position.quantity
        and intent.id
        == stable_risk_intent_id(
            intent.created_snapshot_id,
            existing_position,
            intent.reason,
            created_market_at=intent.created_market_at,
        )
    )


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
        _require_positive_finite_number(self.price, "price")
        _require_nonnegative_finite_number(self.fees, "fees")
        _require_string(self.status, "status")
        if self.status not in {"FILLED", "PARTIAL"}:
            raise ValueError("status must be FILLED or PARTIAL")


def stable_execution_progress_fill_id(
    *,
    intent_id: str,
    symbol: str,
    position_side: PositionSide,
    order_side: OrderSide,
    snapshot_id: str,
    occurred_at: datetime,
    quantity: int,
    price: float,
    fees: float,
    commission: float,
    status: str,
) -> str:
    material = "|".join(
        (
            intent_id,
            symbol,
            position_side.value,
            order_side.value,
            snapshot_id,
            occurred_at.isoformat(),
            str(quantity),
            format(price, ".17g"),
            format(fees, ".17g"),
            format(commission, ".17g"),
            status,
        )
    )
    return "progress-fill-" + hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()[:24]


@dataclass(frozen=True)
class ExecutionProgressFill(_DeeplyImmutable):
    id: str
    intent_id: str
    symbol: str
    position_side: PositionSide
    order_side: OrderSide
    snapshot_id: str
    occurred_at: datetime
    quantity: int
    price: float
    fees: float
    commission: float
    status: str

    def __post_init__(self) -> None:
        _require_string(self.id, "id")
        _require_string(self.intent_id, "intent_id")
        _require_string(self.symbol, "symbol")
        _require_enum(self.position_side, PositionSide, "position_side")
        _require_enum(self.order_side, OrderSide, "order_side")
        _require_string(self.snapshot_id, "snapshot_id")
        _require_datetime(self.occurred_at, "occurred_at")
        _require_positive_quantity(self.quantity)
        _require_positive_finite_number(self.price, "price")
        _require_nonnegative_finite_number(self.fees, "fees")
        _require_nonnegative_finite_number(
            self.commission,
            "commission",
        )
        if self.commission > self.fees:
            raise ValueError("commission must not exceed fees")
        if self.status not in {"PARTIAL", "FILLED"}:
            raise ValueError("status must be PARTIAL or FILLED")
        expected_id = stable_execution_progress_fill_id(
            intent_id=self.intent_id,
            symbol=self.symbol,
            position_side=self.position_side,
            order_side=self.order_side,
            snapshot_id=self.snapshot_id,
            occurred_at=self.occurred_at,
            quantity=self.quantity,
            price=self.price,
            fees=self.fees,
            commission=self.commission,
            status=self.status,
        )
        if self.id != expected_id:
            raise ValueError("execution progress fill ID does not match its facts")


@dataclass(frozen=True)
class OrderExecutionProgress(_DeeplyImmutable):
    intent_id: str
    symbol: str
    position_side: PositionSide
    order_side: OrderSide
    intent_quantity: int
    execution_policy_fingerprint: str
    fills: tuple[ExecutionProgressFill, ...]
    position_average_cost: float | None = None

    def __post_init__(self) -> None:
        _require_string(self.intent_id, "intent_id")
        _require_string(self.symbol, "symbol")
        _require_enum(self.position_side, PositionSide, "position_side")
        _require_enum(self.order_side, OrderSide, "order_side")
        _require_positive_quantity(self.intent_quantity)
        _require_string(
            self.execution_policy_fingerprint,
            "execution_policy_fingerprint",
        )
        fills = _typed_tuple(self.fills, ExecutionProgressFill, "fills")
        if not fills:
            raise ValueError("fills must not be empty")
        if len({item.snapshot_id for item in fills}) != len(fills):
            raise ValueError("fills must not repeat a market snapshot")
        if any(
            later.occurred_at <= earlier.occurred_at
            for earlier, later in zip(fills, fills[1:])
        ):
            raise ValueError("fills must be in strictly increasing time order")
        for fill in fills:
            if (
                fill.intent_id != self.intent_id
                or fill.symbol != self.symbol
                or fill.position_side is not self.position_side
                or fill.order_side is not self.order_side
            ):
                raise ValueError("fill identity must match execution progress")
        filled_quantity = sum((item.quantity for item in fills), 0)
        if filled_quantity > self.intent_quantity:
            raise ValueError("fills must not exceed intent_quantity")
        if any(item.status != "PARTIAL" for item in fills[:-1]):
            raise ValueError("only the final progress fill may be FILLED")
        expected_status = (
            "FILLED" if filled_quantity == self.intent_quantity else "PARTIAL"
        )
        if fills[-1].status != expected_status:
            raise ValueError("final fill status is inconsistent with total quantity")
        _finite_derived_value(
            sum((item.quantity * item.price for item in fills), 0.0),
            "filled_notional",
        )
        _finite_derived_value(
            sum((item.commission for item in fills), 0.0),
            "commission_charged",
        )
        _finite_derived_value(
            sum((item.fees for item in fills), 0.0),
            "fees_charged",
        )
        if self.position_average_cost is not None:
            _require_positive_finite_number(
                self.position_average_cost,
                "position_average_cost",
            )
        if self.intent_id.startswith("risk-"):
            if self.position_average_cost is None:
                raise ValueError(
                    "risk progress requires position_average_cost"
                )
        elif self.position_average_cost is not None:
            raise ValueError(
                "execution progress must not have position_average_cost"
            )
        object.__setattr__(self, "fills", fills)

    @property
    def filled_quantity(self) -> int:
        return sum((item.quantity for item in self.fills), 0)

    @property
    def filled_notional(self) -> float:
        value = sum((item.quantity * item.price for item in self.fills), 0.0)
        return _finite_derived_value(value, "filled_notional")

    @property
    def commission_charged(self) -> float:
        value = sum((item.commission for item in self.fills), 0.0)
        return _finite_derived_value(value, "commission_charged")

    @property
    def fees_charged(self) -> float:
        value = sum((item.fees for item in self.fills), 0.0)
        return _finite_derived_value(value, "fees_charged")

    @property
    def status(self) -> str:
        return self.fills[-1].status

    @property
    def last_snapshot_id(self) -> str:
        return self.fills[-1].snapshot_id


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
    request_fingerprint: str | None = None
    intents: tuple[OrderIntent, ...] = ()
    fills: tuple[ExecutionFill, ...] = ()
    events: tuple[PortfolioEvent, ...] = ()
    diagnostics: tuple[Mapping[str, Any], ...] = ()
    stage_outputs: tuple[StageOutput, ...] = ()
    execution_progress: tuple[OrderExecutionProgress, ...] = ()
    position_risk_updates: tuple[PositionRiskUpdate, ...] = ()
    position_settlement_updates: tuple[PositionSettlementUpdate, ...] = ()
    carry_accruals: tuple[CarryAccrualRecord, ...] = ()

    def __post_init__(self) -> None:
        _require_string(self.run_key, "run_key")
        _require_string(self.strategy_id, "strategy_id")
        _require_integer(self.strategy_revision, "strategy_revision")
        _require_string(self.portfolio_snapshot_id, "portfolio_snapshot_id")
        _require_string(self.market_snapshot_id, "market_snapshot_id")
        if self.request_fingerprint is not None:
            _require_string(self.request_fingerprint, "request_fingerprint")
        intents = _typed_tuple(self.intents, OrderIntent, "intents")
        fills = _typed_tuple(self.fills, ExecutionFill, "fills")
        events = _typed_tuple(self.events, PortfolioEvent, "events")
        execution_progress = _typed_tuple(
            self.execution_progress,
            OrderExecutionProgress,
            "execution_progress",
        )
        position_risk_updates = _typed_tuple(
            self.position_risk_updates,
            PositionRiskUpdate,
            "position_risk_updates",
        )
        position_settlement_updates = _typed_tuple(
            self.position_settlement_updates,
            PositionSettlementUpdate,
            "position_settlement_updates",
        )
        carry_accruals = _typed_tuple(
            self.carry_accruals,
            CarryAccrualRecord,
            "carry_accruals",
        )
        diagnostics = _mapping_tuple(self.diagnostics, "diagnostics")
        stage_outputs = _typed_tuple(
            self.stage_outputs,
            StageOutput,
            "stage_outputs",
        )
        object.__setattr__(self, "intents", intents)
        object.__setattr__(self, "fills", fills)
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "execution_progress", execution_progress)
        object.__setattr__(self, "position_risk_updates", position_risk_updates)
        object.__setattr__(
            self,
            "position_settlement_updates",
            position_settlement_updates,
        )
        object.__setattr__(self, "carry_accruals", carry_accruals)
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


def _require_finite_graph(value: object, field_name: str) -> None:
    """Reject non-finite numbers anywhere in a plain immutable input graph."""

    if type(value) in (int, float):
        _require_finite_number(value, field_name)
        return
    if (
        value is None
        or type(value) in (bool, str, bytes, date, datetime)
        or isinstance(value, Enum)
    ):
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _require_finite_graph(key, f"{field_name}.key")
            _require_finite_graph(item, f"{field_name}[{key!r}]")
        return
    if isinstance(value, (tuple, list, set, frozenset)):
        for index, item in enumerate(value):
            _require_finite_graph(item, f"{field_name}[{index}]")


def _require_v6_strategy_identity(
    strategy: Mapping[str, Any],
    account: AccountSnapshot,
) -> None:
    if type(strategy.get("version")) is not int or strategy.get("version") != 6:
        raise ValueError("strategy.version must be strict schema v6")
    _require_string(strategy.get("id"), "strategy.id")
    revision = strategy.get("revision")
    _require_integer(revision, "strategy.revision")
    if revision < 1:  # type: ignore[operator]
        raise ValueError("strategy.revision must be positive")
    if strategy["id"] != account.strategy_id:
        raise ValueError("strategy.id must match account.strategy_id")
    if revision != account.strategy_revision:
        raise ValueError("strategy.revision must match account.strategy_revision")
    for section in ("exposure_policy", "margin_policy", "short_policy"):
        if not isinstance(strategy.get(section), Mapping):
            raise ValueError(f"strategy.{section} must be explicit in schema v6")


def _freeze_analyzed_rows(
    rows: object,
    *,
    market_name: str,
) -> tuple[Mapping[str, Any], ...]:
    items = _mapping_tuple(rows, "analyzed_rows")
    frozen: list[Mapping[str, Any]] = []
    for index, item in enumerate(items):
        _require_finite_graph(item, f"analyzed_rows[{index}]")
        row_market = item.get("market")
        if row_market is not None and row_market != market_name:
            raise ValueError("analyzed row market must match strategy market")
        frozen.append(_deep_freeze(item))
    return tuple(frozen)


def _freeze_event_calendar(
    calendar: object,
) -> Mapping[str, int | None]:
    _require_mapping(calendar, "event_calendar")
    frozen: dict[str, int | None] = {}
    for symbol, sessions in calendar.items():  # type: ignore[union-attr]
        _require_string(symbol, "event_calendar symbol")
        if sessions is not None:
            _require_integer(sessions, f"event_calendar[{symbol!r}]")
            if sessions < 0:
                raise ValueError("event calendar sessions must be nonnegative")
        frozen[symbol] = sessions
    return MappingProxyType(frozen)


@dataclass(frozen=True)
class RevisionTransition(_DeeplyImmutable):
    """One explicit CAS transaction that advances a strategy revision."""

    id: str
    strategy_id: str
    expected_snapshot_id: str
    from_revision: int
    to_revision: int
    occurred_at: datetime

    def __post_init__(self) -> None:
        _require_string(self.id, "id")
        _require_string(self.strategy_id, "strategy_id")
        _require_string(self.expected_snapshot_id, "expected_snapshot_id")
        for value, field_name in (
            (self.from_revision, "from_revision"),
            (self.to_revision, "to_revision"),
        ):
            if type(value) is not int:
                raise TypeError(f"{field_name} must be an integer")
            if value < 1:
                raise ValueError(f"{field_name} must be positive")
        if self.to_revision <= self.from_revision:
            raise ValueError("revision transition must strictly increase; downgrade forbidden")
        _require_datetime(self.occurred_at, "occurred_at")


@dataclass(frozen=True)
class PlanRequest(_DeeplyImmutable):
    run_key: str
    strategy: Mapping[str, Any]
    account: AccountSnapshot
    analyzed_rows: tuple[Mapping[str, Any], ...]
    market: MarketSnapshot
    borrow: Any
    event_calendar: Mapping[str, int | None]

    def __post_init__(self) -> None:
        from .borrow import BorrowSnapshot

        _require_string(self.run_key, "run_key")
        _require_mapping(self.strategy, "strategy")
        if type(self.account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        if type(self.market) is not MarketSnapshot:
            raise TypeError("market must be MarketSnapshot")
        _require_datetime(self.market.occurred_at, "market.occurred_at")
        if type(self.borrow) is not BorrowSnapshot:
            raise TypeError("borrow must be BorrowSnapshot")
        if not self.account.snapshot_id:
            raise ValueError("account must have an explicit snapshot_id")
        strategy = _deep_freeze(self.strategy)
        _require_v6_strategy_identity(strategy, self.account)
        market_name = strict_strategy_market(strategy)
        _require_finite_graph(self.market.quotes, "market.quotes")
        rows = _freeze_analyzed_rows(self.analyzed_rows, market_name=market_name)
        calendar = _freeze_event_calendar(self.event_calendar)
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "analyzed_rows", rows)
        object.__setattr__(self, "event_calendar", calendar)

    @property
    def market_name(self) -> str:
        return strict_strategy_market(self.strategy)


@dataclass(frozen=True)
class ProcessRequest(_DeeplyImmutable):
    run_key: str
    strategy: Mapping[str, Any]
    account: AccountSnapshot
    market: MarketSnapshot
    borrow: Any
    cost_multiplier: float = 1.0

    def __post_init__(self) -> None:
        from .borrow import BorrowSnapshot

        _require_string(self.run_key, "run_key")
        _require_mapping(self.strategy, "strategy")
        if type(self.account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        if type(self.market) is not MarketSnapshot:
            raise TypeError("market must be MarketSnapshot")
        _require_datetime(self.market.occurred_at, "market.occurred_at")
        if type(self.borrow) is not BorrowSnapshot:
            raise TypeError("borrow must be BorrowSnapshot")
        _require_nonnegative_finite_number(
            self.cost_multiplier,
            "cost_multiplier",
        )
        if not self.account.snapshot_id:
            raise ValueError("account must have an explicit snapshot_id")
        strategy = _deep_freeze(self.strategy)
        _require_v6_strategy_identity(strategy, self.account)
        strict_strategy_market(strategy)
        _require_finite_graph(self.market.quotes, "market.quotes")
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "cost_multiplier", float(self.cost_multiplier))

    @property
    def market_name(self) -> str:
        return strict_strategy_market(self.strategy)


@dataclass(frozen=True)
class PortfolioSnapshot(_DeeplyImmutable):
    account: AccountSnapshot
    metrics: PortfolioMetrics
    positions: tuple[PositionSnapshot, ...]
    open_intents: tuple[OrderIntent, ...]
    recent_events: tuple[PortfolioEvent, ...]

    def __post_init__(self) -> None:
        if type(self.account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        if type(self.metrics) is not PortfolioMetrics:
            raise TypeError("metrics must be PortfolioMetrics")
        positions = _typed_tuple(self.positions, PositionSnapshot, "positions")
        intents = _typed_tuple(self.open_intents, OrderIntent, "open_intents")
        events = _typed_tuple(self.recent_events, PortfolioEvent, "recent_events")
        if positions != self.account.positions:
            raise ValueError("portfolio positions must fully match account.positions")
        if any(position.market_value is None for position in positions):
            raise ValueError("portfolio positions require current prices")
        long_market_value = sum(
            position.market_value or 0.0
            for position in positions
            if position.side is PositionSide.LONG
        )
        short_liability = sum(
            position.market_value or 0.0
            for position in positions
            if position.side is PositionSide.SHORT
        )
        account_metric_values = {
            "available_cash": self.account.available_cash,
            "restricted_short_proceeds": self.account.restricted_short_proceeds,
            "margin_loan": self.account.margin_loan,
            "accrued_financing_cost": self.account.accrued_financing_cost,
            "accrued_borrow_cost": self.account.accrued_borrow_cost,
            "long_market_value": long_market_value,
            "short_liability": short_liability,
        }
        if any(
            not _metric_matches(getattr(self.metrics, field_name), expected)
            for field_name, expected in account_metric_values.items()
        ):
            raise ValueError("portfolio metrics must be derived from the same account")
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "open_intents", intents)
        object.__setattr__(self, "recent_events", events)


@dataclass(frozen=True)
class PortfolioLedgerView(_DeeplyImmutable):
    """Typed read model for service workflows; never exposes persisted JSON."""

    account: AccountSnapshot
    open_intents: tuple[OrderIntent, ...] = ()
    execution_progress: tuple[OrderExecutionProgress, ...] = ()
    recent_events: tuple[PortfolioEvent, ...] = ()

    def __post_init__(self) -> None:
        if type(self.account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        intents = _typed_tuple(self.open_intents, OrderIntent, "open_intents")
        progress = _typed_tuple(
            self.execution_progress,
            OrderExecutionProgress,
            "execution_progress",
        )
        events = _typed_tuple(self.recent_events, PortfolioEvent, "recent_events")
        intent_ids = {item.id for item in intents}
        if any(item.intent_id not in intent_ids for item in progress):
            raise ValueError("execution progress must refer to an open intent")
        object.__setattr__(self, "open_intents", intents)
        object.__setattr__(self, "execution_progress", progress)
        object.__setattr__(self, "recent_events", events)


@dataclass(frozen=True)
class PortfolioPerformanceLedgerView(_DeeplyImmutable):
    """Canonical lifecycle facts exposed only to read-only performance services."""

    account: AccountSnapshot
    intents: tuple[OrderIntent, ...] = ()
    execution_progress: tuple[OrderExecutionProgress, ...] = ()
    events: tuple[PortfolioEvent, ...] = ()
    batches: tuple[DecisionBatch, ...] = ()
    lifecycle_complete: bool = True
    lifecycle_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        intents = _typed_tuple(self.intents, OrderIntent, "intents")
        progress = _typed_tuple(
            self.execution_progress,
            OrderExecutionProgress,
            "execution_progress",
        )
        events = _typed_tuple(self.events, PortfolioEvent, "events")
        batches = _typed_tuple(self.batches, DecisionBatch, "batches")
        if type(self.lifecycle_complete) is not bool:
            raise TypeError("lifecycle_complete must be a boolean")
        if self.lifecycle_reason is not None:
            _require_string(self.lifecycle_reason, "lifecycle_reason")
        intent_ids = {item.id for item in intents}
        if len(intent_ids) != len(intents):
            raise ValueError("performance intents must have unique IDs")
        if self.lifecycle_complete and any(
            item.intent_id not in intent_ids for item in progress
        ):
            raise ValueError("performance progress must refer to a known intent")
        object.__setattr__(self, "intents", intents)
        object.__setattr__(self, "execution_progress", progress)
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "batches", batches)


@dataclass(frozen=True)
class PerformanceHistoryAvailability(_DeeplyImmutable):
    complete: bool
    source: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.complete) is not bool:
            raise TypeError("complete must be a boolean")
        _require_string(self.source, "source")
        if self.reason is not None:
            _require_string(self.reason, "reason")


@dataclass(frozen=True)
class PerformanceHistoryStatus(_DeeplyImmutable):
    nav: PerformanceHistoryAvailability
    lifecycle: PerformanceHistoryAvailability

    def __post_init__(self) -> None:
        if type(self.nav) is not PerformanceHistoryAvailability:
            raise TypeError("nav must be PerformanceHistoryAvailability")
        if type(self.lifecycle) is not PerformanceHistoryAvailability:
            raise TypeError("lifecycle must be PerformanceHistoryAvailability")


@dataclass(frozen=True)
class PerformanceStrategySource(_DeeplyImmutable):
    """Validated strategy/report metadata supplied explicitly to the engine."""

    id: str
    name: str
    revision: int
    stage: str
    market: str
    market_label: str
    currency: str
    currency_symbol: str
    initial_cash: float
    max_positions: int
    signal_model: str | None = None
    signal_time: str | None = None
    signal_data_cutoff: str | None = None
    allocation_model: str | None = None
    benchmark_symbol: str | None = None
    benchmark_name: str | None = None
    market_regime: Mapping[str, Any] | None = None
    risk_level: str | None = None
    trading_mode: str | None = None
    target_exposure_pct: float | None = None
    exposure_policy: Mapping[str, Any] = field(default_factory=dict)
    margin_policy: Mapping[str, Any] = field(default_factory=dict)
    short_policy: Mapping[str, Any] = field(default_factory=dict)
    config: Mapping[str, Any] = field(default_factory=dict)
    allocation: Mapping[str, Any] = field(default_factory=dict)
    symbol_names: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "id",
            "name",
            "stage",
            "market",
            "market_label",
            "currency",
            "currency_symbol",
        ):
            _require_string(getattr(self, field_name), field_name)
        _require_integer(self.revision, "revision")
        if self.revision <= 0:
            raise ValueError("revision must be positive")
        _require_positive_finite_number(self.initial_cash, "initial_cash")
        _require_integer(self.max_positions, "max_positions")
        if self.max_positions <= 0:
            raise ValueError("max_positions must be positive")
        for field_name in (
            "signal_model",
            "signal_time",
            "signal_data_cutoff",
            "allocation_model",
            "benchmark_symbol",
            "benchmark_name",
            "risk_level",
            "trading_mode",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_string(value, field_name)
        if self.target_exposure_pct is not None:
            _require_finite_number(self.target_exposure_pct, "target_exposure_pct")
        if self.market_regime is not None:
            _require_mapping(self.market_regime, "market_regime")
            object.__setattr__(self, "market_regime", _deep_freeze(self.market_regime))
        for field_name in (
            "exposure_policy",
            "margin_policy",
            "short_policy",
            "config",
            "allocation",
            "symbol_names",
        ):
            value = getattr(self, field_name)
            _require_mapping(value, field_name)
            object.__setattr__(self, field_name, _deep_freeze(value))


@dataclass(frozen=True)
class PerformanceProjectionRequest(_DeeplyImmutable):
    strategy: PerformanceStrategySource
    market: MarketSnapshot
    generated_at: datetime
    valuation_source: str
    quote_error: str | None = None

    def __post_init__(self) -> None:
        if type(self.strategy) is not PerformanceStrategySource:
            raise TypeError("strategy must be PerformanceStrategySource")
        if type(self.market) is not MarketSnapshot:
            raise TypeError("market must be MarketSnapshot")
        _require_datetime(self.generated_at, "generated_at")
        _require_string(self.valuation_source, "valuation_source")
        if self.quote_error is not None:
            _require_string(self.quote_error, "quote_error")


@dataclass(frozen=True)
class PerformanceSummary(_DeeplyImmutable):
    initial_cash: float
    nav: float
    cash: float
    reserved_cash: float
    market_value: float
    long_market_value: float
    short_liability: float
    gross_exposure_pct: float | None
    net_exposure_pct: float | None
    margin_rate_pct: float | None
    buying_power: float
    margin_loan: float
    financing_cost: float
    borrow_cost: float
    cumulative_return_pct: float
    maximum_drawdown_pct: float | None
    realized_pnl: float | None
    unrealized_pnl: float
    position_count: int
    max_positions: int
    target_exposure_pct: float | None
    closed_trade_count: int | None
    win_rate_pct: float | None

    def __post_init__(self) -> None:
        _require_positive_finite_number(self.initial_cash, "initial_cash")
        _require_nonnegative_finite_number(self.nav, "nav")
        _require_finite_number(self.cash, "cash")
        for field_name in (
            "reserved_cash",
            "market_value",
            "long_market_value",
            "short_liability",
            "buying_power",
            "margin_loan",
            "financing_cost",
            "borrow_cost",
        ):
            _require_nonnegative_finite_number(getattr(self, field_name), field_name)
        if self.gross_exposure_pct is not None:
            _require_nonnegative_finite_number(
                self.gross_exposure_pct,
                "gross_exposure_pct",
            )
        if self.net_exposure_pct is not None:
            _require_finite_number(self.net_exposure_pct, "net_exposure_pct")
        if self.margin_rate_pct is not None:
            _require_nonnegative_finite_number(self.margin_rate_pct, "margin_rate_pct")
        _require_finite_number(self.cumulative_return_pct, "cumulative_return_pct")
        if self.maximum_drawdown_pct is not None:
            _require_nonnegative_finite_number(
                self.maximum_drawdown_pct,
                "maximum_drawdown_pct",
            )
        if self.realized_pnl is not None:
            _require_finite_number(self.realized_pnl, "realized_pnl")
        _require_finite_number(self.unrealized_pnl, "unrealized_pnl")
        for field_name in ("position_count", "max_positions"):
            value = getattr(self, field_name)
            _require_integer(value, field_name)
            if value < 0:
                raise ValueError(f"{field_name} must be nonnegative")
        if self.max_positions == 0:
            raise ValueError("max_positions must be positive")
        if self.position_count > self.max_positions:
            raise ValueError("position_count must not exceed max_positions")
        if self.target_exposure_pct is not None:
            _require_finite_number(self.target_exposure_pct, "target_exposure_pct")
        if self.closed_trade_count is not None:
            _require_integer(self.closed_trade_count, "closed_trade_count")
            if self.closed_trade_count < 0:
                raise ValueError("closed_trade_count must be nonnegative")
        if self.win_rate_pct is not None:
            _require_nonnegative_finite_number(self.win_rate_pct, "win_rate_pct")
            if self.win_rate_pct > 100:
                raise ValueError("win_rate_pct must not exceed 100")


@dataclass(frozen=True)
class PerformanceRuntime(_DeeplyImmutable):
    last_successful_pipeline_at: datetime | None = None
    last_successful_pipeline_run_id: str | None = None
    last_pipeline_admitted: int | None = None
    last_pipeline_stages: tuple[Mapping[str, Any], ...] | None = None
    last_pipeline_market_regime: Mapping[str, Any] | None = None
    last_pipeline_data_quality: Mapping[str, Any] | None = None
    availability: PerformanceHistoryAvailability = field(
        default_factory=lambda: PerformanceHistoryAvailability(
            complete=True,
            source="v2_ledger",
        )
    )

    def __post_init__(self) -> None:
        if self.last_successful_pipeline_at is not None:
            _require_datetime(self.last_successful_pipeline_at, "last_successful_pipeline_at")
        if self.last_successful_pipeline_run_id is not None:
            _require_string(self.last_successful_pipeline_run_id, "last_successful_pipeline_run_id")
        if self.last_pipeline_admitted is not None:
            _require_integer(self.last_pipeline_admitted, "last_pipeline_admitted")
            if self.last_pipeline_admitted < 0:
                raise ValueError("last_pipeline_admitted must be nonnegative")
        if self.last_pipeline_stages is not None:
            stages = _mapping_tuple(self.last_pipeline_stages, "last_pipeline_stages")
            object.__setattr__(
                self,
                "last_pipeline_stages",
                tuple(_deep_freeze(item) for item in stages),
            )
        for field_name in (
            "last_pipeline_market_regime",
            "last_pipeline_data_quality",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_mapping(value, field_name)
                object.__setattr__(self, field_name, _deep_freeze(value))
        if type(self.availability) is not PerformanceHistoryAvailability:
            raise TypeError("availability must be PerformanceHistoryAvailability")


@dataclass(frozen=True)
class PerformanceNavPoint(_DeeplyImmutable):
    at: datetime
    nav: float
    cash: float
    market_value: float
    cumulative_return_pct: float
    drawdown_pct: float | None
    risk_level: str | None
    trading_mode: str | None
    source: str

    def __post_init__(self) -> None:
        _require_datetime(self.at, "at")
        _require_nonnegative_finite_number(self.nav, "nav")
        _require_finite_number(self.cash, "cash")
        _require_nonnegative_finite_number(self.market_value, "market_value")
        _require_finite_number(self.cumulative_return_pct, "cumulative_return_pct")
        if self.drawdown_pct is not None:
            _require_nonnegative_finite_number(self.drawdown_pct, "drawdown_pct")
        for field_name in ("risk_level", "trading_mode"):
            value = getattr(self, field_name)
            if value is not None:
                _require_string(value, field_name)
        _require_string(self.source, "source")


@dataclass(frozen=True)
class PerformancePosition(_DeeplyImmutable):
    slot_id: int
    name: str
    symbol: str
    first_entry_price: float | None
    first_entry_at: datetime | None
    current_price: float
    day_change_pct: float | None
    return_pct: float
    unrealized_pnl: float
    weight_pct: float
    quantity: int
    sellable_quantity: int | None
    trailing_active: bool
    signal_invalid_days: int | None
    exit_distance_pct: float | None
    market_value: float
    average_cost: float
    position_side: str
    side: str
    position_mode: str
    borrow_rate_pct: float | None
    borrow_rate_source: str
    borrow_rate_estimated: bool
    margin_used: float

    def __post_init__(self) -> None:
        _require_integer(self.slot_id, "slot_id")
        if self.slot_id <= 0:
            raise ValueError("slot_id must be positive")
        _require_string(self.name, "name")
        _require_string(self.symbol, "symbol")
        if (self.first_entry_price is None) != (self.first_entry_at is None):
            raise ValueError("first_entry_price and first_entry_at must be present together")
        if self.first_entry_price is not None:
            _require_positive_finite_number(self.first_entry_price, "first_entry_price")
            _require_datetime(self.first_entry_at, "first_entry_at")
        _require_positive_finite_number(self.current_price, "current_price")
        for field_name in (
            "day_change_pct",
            "exit_distance_pct",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_finite_number(value, field_name)
        for field_name in ("return_pct", "unrealized_pnl", "weight_pct"):
            _require_finite_number(getattr(self, field_name), field_name)
        _require_integer(self.quantity, "quantity")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.sellable_quantity is not None:
            _require_integer(self.sellable_quantity, "sellable_quantity")
            if not 0 <= self.sellable_quantity <= self.quantity:
                raise ValueError("sellable_quantity must be between zero and quantity")
        if type(self.trailing_active) is not bool:
            raise TypeError("trailing_active must be a boolean")
        if self.signal_invalid_days is not None:
            _require_integer(self.signal_invalid_days, "signal_invalid_days")
            if self.signal_invalid_days < 0:
                raise ValueError("signal_invalid_days must be nonnegative")
        _require_nonnegative_finite_number(self.market_value, "market_value")
        _require_positive_finite_number(self.average_cost, "average_cost")
        _require_string(self.position_side, "position_side")
        if self.position_side not in {item.value for item in PositionSide}:
            raise ValueError("position_side must be LONG or SHORT")
        _require_string(self.side, "side")
        if self.side != self.position_side:
            raise ValueError("side must equal position_side")
        _require_string(self.position_mode, "position_mode")
        if self.borrow_rate_pct is not None:
            _require_nonnegative_finite_number(self.borrow_rate_pct, "borrow_rate_pct")
        _require_string(self.borrow_rate_source, "borrow_rate_source")
        if self.borrow_rate_source not in {"strategy_estimate", "unavailable"}:
            raise ValueError(
                "borrow_rate_source must be strategy_estimate or unavailable"
            )
        if type(self.borrow_rate_estimated) is not bool:
            raise TypeError("borrow_rate_estimated must be a boolean")
        if self.position_side == PositionSide.LONG.value and self.borrow_rate_pct is not None:
            raise ValueError("LONG position must not expose a borrow rate")
        if self.position_side == PositionSide.LONG.value and (
            self.borrow_rate_source != "unavailable" or self.borrow_rate_estimated
        ):
            raise ValueError("LONG position borrow rate must be unavailable")
        if self.position_side == PositionSide.SHORT.value and (
            self.borrow_rate_pct is None
            or self.borrow_rate_source != "strategy_estimate"
            or not self.borrow_rate_estimated
        ):
            raise ValueError("SHORT position borrow rate must be a strategy estimate")
        _require_nonnegative_finite_number(self.margin_used, "margin_used")


@dataclass(frozen=True)
class PerformanceOrder(_DeeplyImmutable):
    id: str
    side: str
    symbol: str
    name: str
    quantity: int
    filled_quantity: int
    status: str
    reason: str
    created_at: datetime
    updated_at: datetime
    filled_notional: float
    commission_charged: float
    fees_charged: float
    strategy_revision: int | None
    position_side: str
    position_effect: str
    key: str | None = None
    control_epoch: int | None = None
    purpose: str | None = None
    slot_id: int | None = None
    signal_price: float | None = None
    score: float | None = None
    reserved_cash: float | None = None
    valid_date: str | None = None
    valid_session_date: str | None = None
    cancel_reason: str | None = None
    replacement_candidate: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for field_name in ("id", "symbol", "name", "reason"):
            _require_string(getattr(self, field_name), field_name)
        _require_string(self.side, "side")
        if self.side not in {item.value for item in OrderSide}:
            raise ValueError("side must be BUY or SELL")
        _require_integer(self.quantity, "quantity")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        _require_integer(self.filled_quantity, "filled_quantity")
        if not 0 <= self.filled_quantity <= self.quantity:
            raise ValueError("filled_quantity must be between zero and quantity")
        _require_string(self.status, "status")
        if self.status not in {"INTENDED", "PARTIAL", "FILLED", "CANCELLED", "EXPIRED"}:
            raise ValueError("unsupported order status")
        if self.status == "INTENDED" and self.filled_quantity != 0:
            raise ValueError("INTENDED order must not have fills")
        if self.status == "PARTIAL" and not 0 < self.filled_quantity < self.quantity:
            raise ValueError("PARTIAL order must be partially filled")
        if self.status == "FILLED" and self.filled_quantity != self.quantity:
            raise ValueError("FILLED order quantity must be complete")
        _require_datetime(self.created_at, "created_at")
        _require_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        for field_name in (
            "filled_notional",
            "commission_charged",
            "fees_charged",
        ):
            _require_nonnegative_finite_number(getattr(self, field_name), field_name)
        if self.commission_charged > self.fees_charged:
            raise ValueError("commission_charged must not exceed fees_charged")
        if self.strategy_revision is not None:
            _require_integer(self.strategy_revision, "strategy_revision")
            if self.strategy_revision <= 0:
                raise ValueError("strategy_revision must be positive")
        _require_string(self.position_side, "position_side")
        if self.position_side not in {item.value for item in PositionSide}:
            raise ValueError("position_side must be LONG or SHORT")
        _require_string(self.position_effect, "position_effect")
        if self.position_effect not in {item.value for item in PositionEffect}:
            raise ValueError("unsupported position_effect")
        for field_name in ("key", "purpose", "valid_date", "valid_session_date", "cancel_reason"):
            value = getattr(self, field_name)
            if value is not None:
                _require_string(value, field_name)
        if self.purpose is not None and self.purpose not in {"ENTRY", "EXIT"}:
            raise ValueError("purpose must be ENTRY or EXIT")
        for field_name in ("control_epoch", "slot_id"):
            value = getattr(self, field_name)
            if value is not None:
                _require_integer(value, field_name)
                if value <= 0:
                    raise ValueError(f"{field_name} must be positive")
        if self.signal_price is not None:
            _require_positive_finite_number(self.signal_price, "signal_price")
        if self.score is not None:
            _require_finite_number(self.score, "score")
        if self.reserved_cash is not None:
            _require_nonnegative_finite_number(self.reserved_cash, "reserved_cash")
        if self.replacement_candidate is not None:
            _require_mapping(self.replacement_candidate, "replacement_candidate")
            object.__setattr__(
                self,
                "replacement_candidate",
                _deep_freeze(self.replacement_candidate),
            )


@dataclass(frozen=True)
class PerformanceClosedTrade(_DeeplyImmutable):
    id: str
    name: str
    symbol: str
    entry_price: float
    exit_price: float
    quantity: int
    realized_pnl: float
    return_pct: float
    reason: str
    closed_at: datetime
    strategy_revision: int
    position_side: str

    def __post_init__(self) -> None:
        for field_name in ("id", "name", "symbol", "reason"):
            _require_string(getattr(self, field_name), field_name)
        _require_positive_finite_number(self.entry_price, "entry_price")
        _require_positive_finite_number(self.exit_price, "exit_price")
        _require_integer(self.quantity, "quantity")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        _require_finite_number(self.realized_pnl, "realized_pnl")
        _require_finite_number(self.return_pct, "return_pct")
        _require_datetime(self.closed_at, "closed_at")
        _require_integer(self.strategy_revision, "strategy_revision")
        if self.strategy_revision <= 0:
            raise ValueError("strategy_revision must be positive")
        _require_string(self.position_side, "position_side")
        if self.position_side not in {item.value for item in PositionSide}:
            raise ValueError("position_side must be LONG or SHORT")


@dataclass(frozen=True)
class PerformanceEventView(_DeeplyImmutable):
    id: str
    type: str
    occurred_at: datetime
    message: str
    strategy_revision: int | None
    key: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("id", "type", "message"):
            _require_string(getattr(self, field_name), field_name)
        _require_datetime(self.occurred_at, "occurred_at")
        if self.strategy_revision is not None:
            _require_integer(self.strategy_revision, "strategy_revision")
            if self.strategy_revision <= 0:
                raise ValueError("strategy_revision must be positive")
        if self.key is not None:
            _require_string(self.key, "key")
        _require_mapping(self.data, "data")
        object.__setattr__(self, "data", _deep_freeze(self.data))


@dataclass(frozen=True)
class StrategyPerformanceProjection(_DeeplyImmutable):
    generated_at: datetime
    quote_error: str | None
    strategy: PerformanceStrategySource
    summary: PerformanceSummary
    runtime: PerformanceRuntime
    nav_history: tuple[PerformanceNavPoint, ...]
    positions: tuple[PerformancePosition, ...]
    orders: tuple[PerformanceOrder, ...]
    closed_trades: tuple[PerformanceClosedTrade, ...]
    events: tuple[PerformanceEventView, ...]
    history_availability: PerformanceHistoryStatus

    def __post_init__(self) -> None:
        _require_datetime(self.generated_at, "generated_at")
        if self.quote_error is not None:
            _require_string(self.quote_error, "quote_error")
        if type(self.strategy) is not PerformanceStrategySource:
            raise TypeError("strategy must be PerformanceStrategySource")
        if type(self.summary) is not PerformanceSummary:
            raise TypeError("summary must be PerformanceSummary")
        if type(self.runtime) is not PerformanceRuntime:
            raise TypeError("runtime must be PerformanceRuntime")
        if type(self.history_availability) is not PerformanceHistoryStatus:
            raise TypeError("history_availability must be PerformanceHistoryStatus")
        object.__setattr__(
            self,
            "nav_history",
            _typed_tuple(self.nav_history, PerformanceNavPoint, "nav_history"),
        )
        object.__setattr__(
            self,
            "positions",
            _typed_tuple(self.positions, PerformancePosition, "positions"),
        )
        object.__setattr__(
            self,
            "orders",
            _typed_tuple(self.orders, PerformanceOrder, "orders"),
        )
        object.__setattr__(
            self,
            "closed_trades",
            _typed_tuple(self.closed_trades, PerformanceClosedTrade, "closed_trades"),
        )
        object.__setattr__(
            self,
            "events",
            _typed_tuple(self.events, PerformanceEventView, "events"),
        )


_DEEPLY_IMMUTABLE_TYPES = (
    AccrualLifecycle,
    PositionSnapshot,
    PositionRiskUpdate,
    PositionSettlementUpdate,
    CarryAccrualRecord,
    AccountSnapshot,
    PortfolioMetrics,
    ValuationResult,
    SignalCandidate,
    TargetPosition,
    OrderIntent,
    MarketSnapshot,
    ExecutionFill,
    ExecutionProgressFill,
    OrderExecutionProgress,
    PortfolioEvent,
    DecisionBatch,
    RevisionTransition,
    PlanRequest,
    ProcessRequest,
    PortfolioSnapshot,
    PortfolioLedgerView,
    PortfolioPerformanceLedgerView,
    PerformanceHistoryAvailability,
    PerformanceHistoryStatus,
    PerformanceStrategySource,
    PerformanceProjectionRequest,
    PerformanceSummary,
    PerformanceRuntime,
    PerformanceNavPoint,
    PerformancePosition,
    PerformanceOrder,
    PerformanceClosedTrade,
    PerformanceEventView,
    StrategyPerformanceProjection,
)
