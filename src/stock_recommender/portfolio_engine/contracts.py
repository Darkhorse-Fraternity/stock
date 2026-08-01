"""Domain contracts for portfolio engine inputs and outputs."""

from __future__ import annotations

import hashlib
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


def _strategy_market_name(strategy: Mapping[str, Any]) -> str:
    direct = strategy.get("market")
    parameters = strategy.get("parameters")
    nested = None
    if isinstance(parameters, Mapping):
        market_parameter = parameters.get("market")
        if isinstance(market_parameter, Mapping):
            nested = market_parameter.get("value")
    values = [str(item).strip().lower() for item in (direct, nested) if item]
    if not values or any(item not in {"cn", "us"} for item in values):
        raise ValueError("strategy market must be explicitly cn or us")
    if len(set(values)) != 1:
        raise ValueError("strategy market identities conflict")
    return values[0]


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
        market_name = _strategy_market_name(strategy)
        _require_finite_graph(self.market.quotes, "market.quotes")
        rows = _freeze_analyzed_rows(self.analyzed_rows, market_name=market_name)
        calendar = _freeze_event_calendar(self.event_calendar)
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "analyzed_rows", rows)
        object.__setattr__(self, "event_calendar", calendar)

    @property
    def market_name(self) -> str:
        return _strategy_market_name(self.strategy)


@dataclass(frozen=True)
class ProcessRequest(_DeeplyImmutable):
    run_key: str
    strategy: Mapping[str, Any]
    account: AccountSnapshot
    market: MarketSnapshot
    borrow: Any

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
        _strategy_market_name(strategy)
        _require_finite_graph(self.market.quotes, "market.quotes")
        object.__setattr__(self, "strategy", strategy)

    @property
    def market_name(self) -> str:
        return _strategy_market_name(self.strategy)


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
)
