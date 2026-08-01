"""Pure, direction-aware portfolio risk decisions and deleveraging plans."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Iterator

from ..pipeline import PipelineContractError, StageInput, StageOutput
from .config import MarginPolicy, ShortPolicy
from .contracts import (
    AccountSnapshot,
    MarketSnapshot,
    OrderIntent,
    OrderSide,
    PortfolioMetrics,
    PositionEffect,
    PositionSide,
    PositionSnapshot,
    freeze_immutable,
)
from .margin import project_account_for_intent
from .valuation import ValuationError, value_account


NORMAL = "NORMAL"
COVER_ONLY = "COVER_ONLY"
WARNING = "WARNING"
DERISK = "DERISK"
MANUAL_HALT = "MANUAL_HALT"
INSOLVENT_HALT = "INSOLVENT_HALT"
REDUCE_ONLY = "REDUCE_ONLY"
MARGIN_CALL = "MARGIN_CALL"

LONG_STOP_LOSS = "LONG_STOP_LOSS"
SHORT_STOP_LOSS = "SHORT_STOP_LOSS"
LONG_TRAILING_STOP = "LONG_TRAILING_STOP"
SHORT_TRAILING_STOP = "SHORT_TRAILING_STOP"
SHORT_SQUEEZE = "SHORT_SQUEEZE"
SQUEEZE_DATA_INVALID = "SQUEEZE_DATA_INVALID"

FORCED_DELEVERAGING_COST_RATE = 0.001
_POSITION_MODES = frozenset({NORMAL, COVER_ONLY})
_PORTFOLIO_STATES = frozenset(
    {NORMAL, WARNING, DERISK, MANUAL_HALT, INSOLVENT_HALT, REDUCE_ONLY, MARGIN_CALL}
)
_RISK_REASON_SEMANTICS = {
    LONG_STOP_LOSS: frozenset(
        {
            (PositionSide.LONG, OrderSide.SELL, PositionEffect.CLOSE, mode)
            for mode in _POSITION_MODES
        }
    ),
    LONG_TRAILING_STOP: frozenset(
        {
            (PositionSide.LONG, OrderSide.SELL, PositionEffect.CLOSE, mode)
            for mode in _POSITION_MODES
        }
    ),
    SHORT_STOP_LOSS: frozenset(
        {
            (PositionSide.SHORT, OrderSide.BUY, PositionEffect.CLOSE, mode)
            for mode in _POSITION_MODES
        }
    ),
    SHORT_TRAILING_STOP: frozenset(
        {
            (PositionSide.SHORT, OrderSide.BUY, PositionEffect.CLOSE, mode)
            for mode in _POSITION_MODES
        }
    ),
    MARGIN_CALL: frozenset(
        {
            (side, order_side, PositionEffect.CLOSE, mode)
            for side, order_side in (
                (PositionSide.LONG, OrderSide.SELL),
                (PositionSide.SHORT, OrderSide.BUY),
            )
            for mode in _POSITION_MODES
        }
    ),
    SHORT_SQUEEZE: frozenset(
        {(PositionSide.SHORT, None, None, COVER_ONLY)}
    ),
    SQUEEZE_DATA_INVALID: frozenset(
        {
            (PositionSide.SHORT, None, None, mode)
            for mode in _POSITION_MODES
        }
    ),
}


class RiskError(ValueError):
    """Raised when risk cannot make a valid decision from the supplied snapshot."""


class _ImmutableRiskValue:
    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, object]):
        memo[id(self)] = self
        return self


def _finite_number(value: object, field_name: str) -> float:
    if type(value) not in (int, float):
        raise RiskError(f"{field_name} must be an int or float")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise RiskError(f"{field_name} must be finite") from exc
    if not math.isfinite(number):
        raise RiskError(f"{field_name} must be finite")
    return 0.0 if number == 0.0 else number


def _positive_number(value: object, field_name: str) -> float:
    number = _finite_number(value, field_name)
    if number <= 0:
        raise RiskError(f"{field_name} must be positive")
    return number


def _nonnegative_number(value: object, field_name: str) -> float:
    number = _finite_number(value, field_name)
    if number < 0:
        raise RiskError(f"{field_name} must be nonnegative")
    return number


def _percent_price(base: float, percentage: float, *, increase: bool) -> float:
    """Derive an exact decimal policy threshold before comparing binary quotes."""

    direction = Decimal(1) if increase else Decimal(-1)
    threshold = Decimal(str(base)) * (
        Decimal(1) + direction * Decimal(str(percentage)) / Decimal(100)
    )
    result = float(threshold)
    if not math.isfinite(result) or result <= 0:
        raise RiskError("price threshold must be finite and positive")
    return result


def _validate_position_mode(value: object, field_name: str = "position_mode") -> str:
    if value not in _POSITION_MODES:
        raise RiskError(f"{field_name} must be NORMAL or COVER_ONLY")
    return str(value)


@dataclass(frozen=True)
class PositionRiskPolicies(_ImmutableRiskValue):
    long_stop_loss_pct: float = 8.0
    long_trailing_activation_pct: float = 10.0
    long_trailing_drawdown_pct: float = 5.0
    short_stop_loss_pct: float = 6.0
    short_trailing_activation_pct: float = 8.0
    short_trailing_rebound_pct: float = 4.0

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            value = _positive_number(getattr(self, field_name), field_name)
            if value >= 100:
                raise RiskError(f"{field_name} must be below 100")
            object.__setattr__(self, field_name, value)


def default_policies() -> PositionRiskPolicies:
    """Return the immutable default long/short exit policy."""

    return PositionRiskPolicies()


def _resolve_position_policies(value: object) -> PositionRiskPolicies:
    if value is None:
        return default_policies()
    if type(value) is PositionRiskPolicies:
        return value
    if type(value) is ShortPolicy:
        return PositionRiskPolicies(
            short_stop_loss_pct=value.stop_loss_pct,
            short_trailing_activation_pct=value.trailing_activation_pct,
            short_trailing_rebound_pct=value.trailing_rebound_pct,
        )
    if isinstance(value, Mapping):
        defaults = default_policies()
        aliases = {
            "long_stop_loss_pct": "long_stop_loss_pct",
            "long_trailing_activation_pct": "long_trailing_activation_pct",
            "long_trailing_drawdown_pct": "long_trailing_drawdown_pct",
            "short_stop_loss_pct": "short_stop_loss_pct",
            "short_trailing_activation_pct": "short_trailing_activation_pct",
            "short_trailing_rebound_pct": "short_trailing_rebound_pct",
            "stop_loss_pct": "short_stop_loss_pct",
            "trailing_activation_pct": "short_trailing_activation_pct",
            "trailing_rebound_pct": "short_trailing_rebound_pct",
        }
        values = {
            name: getattr(defaults, name) for name in defaults.__dataclass_fields__
        }
        for source, target in aliases.items():
            if source in value:
                values[target] = value[source]
        return PositionRiskPolicies(**values)
    raise TypeError("policies must be PositionRiskPolicies, ShortPolicy, mapping, or None")


@dataclass(frozen=True)
class RiskDecision(_ImmutableRiskValue):
    reason: str | None
    position_effect: PositionEffect | None
    position_mode: str
    updated_position: PositionSnapshot
    intent: OrderIntent | None = None

    def __post_init__(self) -> None:
        if self.reason is not None and (type(self.reason) is not str or not self.reason):
            raise ValueError("reason must be a non-empty string or None")
        if self.position_effect is not None and type(self.position_effect) is not PositionEffect:
            raise TypeError("position_effect must be PositionEffect or None")
        _validate_position_mode(self.position_mode)
        if type(self.updated_position) is not PositionSnapshot:
            raise TypeError("updated_position must be PositionSnapshot")
        if self.intent is not None and type(self.intent) is not OrderIntent:
            raise TypeError("intent must be OrderIntent or None")
        if self.intent is None and self.position_effect is not None:
            raise ValueError("position_effect requires an intent")
        if self.intent is not None and self.position_effect is not self.intent.position_effect:
            raise ValueError("position_effect must match intent.position_effect")
        if self.intent is not None:
            expected_order_side = (
                OrderSide.SELL
                if self.updated_position.side is PositionSide.LONG
                else OrderSide.BUY
            )
            if (
                self.position_effect is not PositionEffect.CLOSE
                or self.intent.position_effect is not PositionEffect.CLOSE
                or self.intent.order_side is not expected_order_side
            ):
                raise ValueError(
                    "risk intent must be a direction-correct full close"
                )
        if self.intent is not None and (
            self.reason != self.intent.reason
            or self.intent.symbol != self.updated_position.symbol
            or self.intent.position_side is not self.updated_position.side
            or self.intent.quantity != self.updated_position.quantity
        ):
            raise ValueError("intent must match the risk decision and updated position")
        if self.intent is not None and self.intent.id != _stable_intent_id(
            self.intent.created_snapshot_id,
            self.updated_position,
            self.intent.reason,
        ):
            raise ValueError("risk intent id must match its immutable decision inputs")
        if self.reason is not None:
            allowed = _RISK_REASON_SEMANTICS.get(self.reason)
            actual = (
                self.updated_position.side,
                None if self.intent is None else self.intent.order_side,
                self.position_effect,
                self.position_mode,
            )
            if allowed is None or actual not in allowed:
                raise ValueError("reason is inconsistent with risk decision semantics")
        if self.updated_position.position_mode != self.position_mode:
            raise ValueError("updated_position mode must match position_mode")


@dataclass(frozen=True)
class PositionRiskResult(_ImmutableRiskValue):
    updated_position: PositionSnapshot
    decisions: tuple[RiskDecision, ...] = ()

    def __post_init__(self) -> None:
        if type(self.updated_position) is not PositionSnapshot:
            raise TypeError("updated_position must be PositionSnapshot")
        decisions = tuple(self.decisions)
        if any(type(item) is not RiskDecision for item in decisions):
            raise TypeError("decisions items must be RiskDecision")
        if len(decisions) > 1:
            raise ValueError("one position snapshot may emit at most one risk decision")
        if decisions and decisions[0].updated_position != self.updated_position:
            raise ValueError("decision updated_position must match result")
        object.__setattr__(self, "decisions", decisions)

    @property
    def intents(self) -> tuple[OrderIntent, ...]:
        return tuple(item.intent for item in self.decisions if item.intent is not None)

    @property
    def position_mode(self) -> str:
        return self.updated_position.position_mode

    def __iter__(self) -> Iterator[RiskDecision]:
        return iter(self.decisions)

    def __len__(self) -> int:
        return len(self.decisions)

    def __getitem__(self, index: int) -> RiskDecision:
        return self.decisions[index]


@dataclass(frozen=True)
class PositionRiskUpdate(_ImmutableRiskValue):
    symbol: str
    side: PositionSide
    peak_price: float | None
    trough_price: float | None
    trailing_active: bool
    position_mode: str

    def __post_init__(self) -> None:
        if type(self.symbol) is not str or not self.symbol:
            raise ValueError("symbol must be a non-empty string")
        if type(self.side) is not PositionSide:
            raise TypeError("side must be PositionSide")
        for field_name in ("peak_price", "trough_price"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _positive_number(value, field_name),
                )
        if type(self.trailing_active) is not bool:
            raise TypeError("trailing_active must be a bool")
        _validate_position_mode(self.position_mode)

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
class PortfolioDrawdownResult(_ImmutableRiskValue):
    state: str
    drawdown_pct: float
    equity: float
    peak_equity: float
    diagnostic: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in {NORMAL, WARNING, DERISK, MANUAL_HALT, INSOLVENT_HALT}:
            raise ValueError(f"unsupported drawdown state: {self.state}")
        for field_name in ("drawdown_pct", "equity", "peak_equity"):
            _finite_number(getattr(self, field_name), field_name)
        if not isinstance(self.diagnostic, Mapping):
            raise TypeError("diagnostic must be a mapping")
        object.__setattr__(self, "diagnostic", freeze_immutable(self.diagnostic))


@dataclass(frozen=True)
class ForcedDeleveragingCandidate(_ImmutableRiskValue):
    symbol: str
    margin_released: float
    risk_contribution: float
    estimated_transaction_cost: float
    intent: OrderIntent

    def __post_init__(self) -> None:
        if type(self.symbol) is not str or not self.symbol:
            raise ValueError("symbol must be a non-empty string")
        for field_name in (
            "margin_released",
            "risk_contribution",
            "estimated_transaction_cost",
        ):
            value = _nonnegative_number(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, value)
        if type(self.intent) is not OrderIntent:
            raise TypeError("intent must be OrderIntent")
        if self.intent.symbol != self.symbol or self.intent.increases_risk:
            raise ValueError("candidate intent must reduce risk for its symbol")

    @property
    def sort_key(self) -> tuple[float, float, float, str]:
        return (
            -self.margin_released,
            -self.risk_contribution,
            self.estimated_transaction_cost,
            self.symbol,
        )


@dataclass(frozen=True)
class ForcedDeleveragingResult(_ImmutableRiskValue):
    state: str
    intents: tuple[OrderIntent, ...] = ()
    candidates: tuple[ForcedDeleveragingCandidate, ...] = ()
    diagnostics: tuple[Mapping[str, object], ...] = ()
    missing_price_symbols: tuple[str, ...] = ()
    initial_margin_rate_pct: float = math.inf
    final_margin_rate_pct: float = math.inf

    def __post_init__(self) -> None:
        if self.state not in _PORTFOLIO_STATES:
            raise ValueError(f"unsupported forced-deleveraging state: {self.state}")
        intents = tuple(self.intents)
        if any(type(item) is not OrderIntent for item in intents):
            raise TypeError("intents items must be OrderIntent")
        if any(item.increases_risk for item in intents):
            raise ValueError("forced-deleveraging intents must not increase risk")
        candidates = tuple(self.candidates)
        if any(type(item) is not ForcedDeleveragingCandidate for item in candidates):
            raise TypeError("candidates items must be ForcedDeleveragingCandidate")
        diagnostics = tuple(self.diagnostics)
        if any(not isinstance(item, Mapping) for item in diagnostics):
            raise TypeError("diagnostics items must be mappings")
        missing = tuple(self.missing_price_symbols)
        if any(type(item) is not str or not item for item in missing):
            raise TypeError("missing_price_symbols items must be non-empty strings")
        for field_name in ("initial_margin_rate_pct", "final_margin_rate_pct"):
            value = getattr(self, field_name)
            if type(value) not in (int, float) or math.isnan(float(value)):
                raise ValueError(f"{field_name} must not be NaN")
        object.__setattr__(self, "intents", intents)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(
            self,
            "diagnostics",
            tuple(freeze_immutable(item) for item in diagnostics),
        )
        object.__setattr__(self, "missing_price_symbols", missing)


def _stable_intent_id(
    snapshot_id: str,
    position: PositionSnapshot,
    reason: str,
) -> str:
    if type(snapshot_id) is not str or not snapshot_id:
        raise RiskError("snapshot_id must be a non-empty string")
    material = "|".join(
        (
            snapshot_id,
            position.symbol,
            position.side.value,
            str(position.quantity),
            format(float(position.average_cost), ".17g"),
            reason,
        )
    )
    return "risk-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _close_intent(
    position: PositionSnapshot,
    reason: str,
    snapshot_id: str,
) -> OrderIntent:
    return OrderIntent(
        id=_stable_intent_id(snapshot_id, position, reason),
        symbol=position.symbol,
        position_side=position.side,
        order_side=(
            OrderSide.SELL if position.side is PositionSide.LONG else OrderSide.BUY
        ),
        position_effect=PositionEffect.CLOSE,
        quantity=position.quantity,
        reason=reason,
        created_snapshot_id=snapshot_id,
    )


def _validated_position_prices(
    position: PositionSnapshot,
) -> tuple[float, float, float | None, float | None]:
    if type(position) is not PositionSnapshot:
        raise TypeError("position must be PositionSnapshot")
    entry = _positive_number(position.average_cost, "average_cost")
    if position.current_price is None:
        raise RiskError(f"current_price is required for {position.symbol}")
    current = _positive_number(position.current_price, "current_price")
    peak = (
        None
        if position.peak_price is None
        else _positive_number(position.peak_price, "peak_price")
    )
    trough = (
        None
        if position.trough_price is None
        else _positive_number(position.trough_price, "trough_price")
    )
    if position.side is PositionSide.LONG:
        if position.trailing_active and peak is None:
            raise RiskError("active long trailing stop requires peak_price")
        if peak is not None and peak < entry:
            raise RiskError("long peak_price must not be below average_cost")
    else:
        if position.trailing_active and trough is None:
            raise RiskError("active short trailing stop requires trough_price")
        if trough is not None and trough > entry:
            raise RiskError("short trough_price must not exceed average_cost")
    return entry, current, peak, trough


def evaluate_position_risk(
    position: PositionSnapshot,
    policies: PositionRiskPolicies | ShortPolicy | Mapping[str, object] | None = None,
    *,
    snapshot_id: str = "risk-snapshot",
) -> PositionRiskResult:
    """Evaluate one immutable position, updating anchors without mutating it."""

    resolved = _resolve_position_policies(policies)
    entry, current, peak, trough = _validated_position_prices(position)
    _validate_position_mode(position.position_mode)

    reason: str | None = None
    if position.side is PositionSide.LONG:
        updated_peak = max(entry, current, peak if peak is not None else entry)
        activation_price = _percent_price(
            entry,
            resolved.long_trailing_activation_pct,
            increase=True,
        )
        active = position.trailing_active or updated_peak >= activation_price
        if position.trailing_active and updated_peak < activation_price:
            raise RiskError("active long trailing stop requires an activated peak_price")
        updated = replace(
            position,
            peak_price=updated_peak,
            trailing_active=active,
        )
        if current <= _percent_price(
            entry,
            resolved.long_stop_loss_pct,
            increase=False,
        ):
            reason = LONG_STOP_LOSS
        elif active and current <= _percent_price(
            updated_peak,
            resolved.long_trailing_drawdown_pct,
            increase=False,
        ):
            reason = LONG_TRAILING_STOP
    else:
        updated_trough = min(entry, current, trough if trough is not None else entry)
        activation_price = _percent_price(
            entry,
            resolved.short_trailing_activation_pct,
            increase=False,
        )
        active = position.trailing_active or updated_trough <= activation_price
        if position.trailing_active and updated_trough > activation_price:
            raise RiskError("active short trailing stop requires an activated trough_price")
        updated = replace(
            position,
            trough_price=updated_trough,
            trailing_active=active,
        )
        if current >= _percent_price(
            entry,
            resolved.short_stop_loss_pct,
            increase=True,
        ):
            reason = SHORT_STOP_LOSS
        elif active and current >= _percent_price(
            updated_trough,
            resolved.short_trailing_rebound_pct,
            increase=True,
        ):
            reason = SHORT_TRAILING_STOP

    if reason is None:
        return PositionRiskResult(updated_position=updated)
    intent = _close_intent(updated, reason, snapshot_id)
    decision = RiskDecision(
        reason=reason,
        position_effect=PositionEffect.CLOSE,
        position_mode=updated.position_mode,
        updated_position=updated,
        intent=intent,
    )
    return PositionRiskResult(updated_position=updated, decisions=(decision,))


def _short_policy(value: object) -> ShortPolicy:
    if value is None:
        return ShortPolicy()
    if type(value) is not ShortPolicy:
        raise TypeError("short_policy must be ShortPolicy or None")
    _positive_number(value.squeeze_rise_pct, "squeeze_rise_pct")
    _positive_number(value.squeeze_volume_ratio, "squeeze_volume_ratio")
    return value


def _quote_number(
    quote: Mapping[str, object],
    names: tuple[str, ...],
) -> float | None:
    for name in names:
        if name not in quote:
            continue
        raw = quote[name]
        if type(raw) not in (int, float):
            return None
        number = float(raw)
        return number if math.isfinite(number) else None
    return None


def evaluate_squeeze(
    short_position: PositionSnapshot,
    quote: Mapping[str, object],
    short_policy: ShortPolicy | None = None,
) -> RiskDecision:
    """Return a cover-only mode decision when both squeeze signals breach."""

    if type(short_position) is not PositionSnapshot:
        raise TypeError("short_position must be PositionSnapshot")
    if not isinstance(quote, Mapping):
        raise TypeError("quote must be a mapping")
    policy = _short_policy(short_policy)
    original_mode = _validate_position_mode(short_position.position_mode)
    if short_position.side is PositionSide.LONG:
        updated = replace(short_position, position_mode=original_mode)
        return RiskDecision(None, None, original_mode, updated)

    daily_rise = _quote_number(
        quote,
        ("daily_rise_pct", "percent", "change_pct", "daily_change_pct", "pct_change"),
    )
    volume_ratio = _quote_number(quote, ("volume_ratio", "relative_volume"))
    if daily_rise is None or volume_ratio is None:
        updated = replace(short_position, position_mode=original_mode)
        return RiskDecision(SQUEEZE_DATA_INVALID, None, original_mode, updated)
    triggered = (
        daily_rise >= float(policy.squeeze_rise_pct)
        and volume_ratio >= float(policy.squeeze_volume_ratio)
    )
    mode = COVER_ONLY if triggered or original_mode == COVER_ONLY else NORMAL
    updated = replace(short_position, position_mode=mode)
    return RiskDecision(SHORT_SQUEEZE if triggered else None, None, mode, updated)


def evaluate_portfolio_drawdown(
    metrics: PortfolioMetrics,
    peak_equity: object,
) -> PortfolioDrawdownResult:
    """Apply exact 12/14/15 percent drawdown gates to valuation equity."""

    if type(metrics) is not PortfolioMetrics:
        raise TypeError("metrics must be PortfolioMetrics")
    equity = _finite_number(metrics.equity, "metrics.equity")
    if equity <= 0:
        peak = (
            float(peak_equity)
            if type(peak_equity) in (int, float) and math.isfinite(float(peak_equity))
            else 0.0
        )
        diagnostic = {
            "state": INSOLVENT_HALT,
            "equity": equity,
            "peak_equity": peak,
            "drawdown_pct": 100.0,
        }
        return PortfolioDrawdownResult(
            INSOLVENT_HALT,
            100.0,
            equity,
            peak,
            diagnostic,
        )
    peak = _positive_number(peak_equity, "peak_equity")
    drawdown_decimal = (
        (Decimal(str(peak)) - Decimal(str(equity)))
        / Decimal(str(peak))
        * Decimal(100)
    )
    drawdown_pct = float(drawdown_decimal)
    if not math.isfinite(drawdown_pct):
        raise RiskError("drawdown_pct must be finite")
    if drawdown_decimal >= Decimal(15):
        state = MANUAL_HALT
    elif drawdown_decimal >= Decimal(14):
        state = DERISK
    elif drawdown_decimal >= Decimal(12):
        state = WARNING
    else:
        state = NORMAL
    diagnostic = {
        "state": state,
        "equity": equity,
        "peak_equity": peak,
        "drawdown_pct": drawdown_pct,
    }
    return PortfolioDrawdownResult(state, drawdown_pct, equity, peak, diagnostic)


def _raw_prices(
    market_or_prices: MarketSnapshot | Mapping[str, object],
) -> Mapping[str, object]:
    if type(market_or_prices) is MarketSnapshot:
        return market_or_prices.quotes
    if not isinstance(market_or_prices, Mapping):
        raise TypeError("prices must be a mapping or MarketSnapshot")
    return market_or_prices


def _market_price(raw_prices: Mapping[str, object], symbol: str) -> float | None:
    if symbol not in raw_prices:
        return None
    raw = raw_prices[symbol]
    if isinstance(raw, Mapping):
        if "price" not in raw:
            return None
        raw = raw["price"]
    if type(raw) not in (int, float):
        return None
    price = float(raw)
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def _valuation_context(
    account: AccountSnapshot,
    raw_prices: Mapping[str, object],
) -> tuple[AccountSnapshot, dict[str, float], tuple[str, ...], bool]:
    valuation_prices: dict[str, float] = {}
    unvalued: list[str] = []
    missing_quotes: list[str] = []
    for held in account.positions:
        price = _market_price(raw_prices, held.symbol)
        if price is None:
            missing_quotes.append(held.symbol)
            if held.current_price is not None:
                try:
                    price = _positive_number(held.current_price, "current_price")
                except RiskError:
                    price = None
        if price is None:
            unvalued.append(held.symbol)
        else:
            valuation_prices[held.symbol] = price
    complete = not unvalued
    if complete:
        return account, valuation_prices, tuple(sorted(missing_quotes)), True
    valued_positions = tuple(
        held for held in account.positions if held.symbol in valuation_prices
    )
    return (
        replace(account, positions=valued_positions),
        valuation_prices,
        tuple(sorted(missing_quotes)),
        False,
    )


def _value_risk_account(
    account: AccountSnapshot,
    raw_prices: Mapping[str, object],
    *,
    equity_override: Decimal | None = None,
) -> tuple[PortfolioMetrics, tuple[str, ...], bool, Decimal, float]:
    valuation_account, prices, missing, complete = _valuation_context(
        account, raw_prices
    )
    try:
        metrics = value_account(valuation_account, prices).metrics
    except (TypeError, ValueError, ValuationError) as exc:
        raise RiskError(f"account valuation failed: {exc}") from exc
    long_value = sum(
        (
            Decimal(str(prices[held.symbol])) * held.quantity
            for held in valuation_account.positions
            if held.side is PositionSide.LONG
        ),
        Decimal(0),
    )
    short_value = sum(
        (
            Decimal(str(prices[held.symbol])) * held.quantity
            for held in valuation_account.positions
            if held.side is PositionSide.SHORT
        ),
        Decimal(0),
    )
    gross = long_value + short_value
    exact_equity = equity_override
    if exact_equity is None:
        exact_equity = (
            Decimal(str(valuation_account.available_cash))
            + Decimal(str(valuation_account.restricted_short_proceeds))
            + long_value
            - short_value
            - Decimal(str(valuation_account.margin_loan))
        )
    stable_margin_rate = (
        math.inf
        if gross == 0
        else float(exact_equity / gross * Decimal(100))
    )
    return metrics, missing, complete, exact_equity, stable_margin_rate


def _margin_policy(value: object) -> tuple[MarginPolicy, float, float]:
    if value is None:
        policy = MarginPolicy()
    elif type(value) is MarginPolicy:
        policy = value
    else:
        raise TypeError("margin_policy must be MarginPolicy or None")
    maintenance = _positive_number(
        policy.maintenance_margin_pct,
        "maintenance_margin_pct",
    )
    buffer = _nonnegative_number(
        policy.liquidation_buffer_pct,
        "liquidation_buffer_pct",
    )
    threshold = maintenance + buffer
    if not math.isfinite(threshold) or threshold <= 0:
        raise RiskError("margin buffer threshold must be finite and positive")
    return policy, maintenance, threshold


def _gross(metrics: PortfolioMetrics) -> float:
    gross = metrics.long_market_value + metrics.short_liability
    if not math.isfinite(gross) or gross < 0:
        raise RiskError("gross market value must be finite and nonnegative")
    return gross


def _margin_state(
    metrics: PortfolioMetrics,
    maintenance: float,
    threshold: float,
    *,
    exact_equity: Decimal | None = None,
    stable_margin_rate: float | None = None,
) -> str:
    if _gross(metrics) == 0:
        return NORMAL
    equity_insolvent = (
        metrics.equity <= 0
        if exact_equity is None
        else exact_equity <= Decimal(0)
    )
    margin_rate = (
        metrics.margin_rate_pct
        if stable_margin_rate is None
        else stable_margin_rate
    )
    if equity_insolvent or margin_rate < maintenance:
        return MARGIN_CALL
    if margin_rate < threshold:
        return REDUCE_ONLY
    return NORMAL


def evaluate_forced_deleveraging(
    account: AccountSnapshot,
    prices: MarketSnapshot | Mapping[str, object],
    margin_policy: MarginPolicy | None = None,
) -> ForcedDeleveragingResult:
    """Plan deterministic full closes until the liquidation buffer is restored."""

    if type(account) is not AccountSnapshot:
        raise TypeError("account must be AccountSnapshot")
    raw_prices = _raw_prices(prices)
    resolved_policy, maintenance, threshold = _margin_policy(margin_policy)
    threshold_decimal = Decimal(str(maintenance)) + Decimal(
        str(resolved_policy.liquidation_buffer_pct)
    )
    (
        initial_metrics,
        missing,
        complete,
        exact_equity,
        initial_rate,
    ) = _value_risk_account(account, raw_prices)
    diagnostics: list[Mapping[str, object]] = [
        {"code": "MISSING_PRICE", "symbol": symbol} for symbol in missing
    ]
    if complete and exact_equity <= Decimal(0):
        return ForcedDeleveragingResult(
            state=INSOLVENT_HALT,
            diagnostics=tuple(diagnostics),
            missing_price_symbols=missing,
            initial_margin_rate_pct=initial_rate,
            final_margin_rate_pct=initial_rate,
        )
    state = _margin_state(
        initial_metrics,
        maintenance,
        threshold,
        exact_equity=exact_equity,
        stable_margin_rate=initial_rate,
    )
    if state != MARGIN_CALL:
        return ForcedDeleveragingResult(
            state=state,
            diagnostics=tuple(diagnostics),
            missing_price_symbols=missing,
            initial_margin_rate_pct=initial_rate,
            final_margin_rate_pct=initial_rate,
        )

    candidates: list[ForcedDeleveragingCandidate] = []
    for held in sorted(account.positions, key=lambda item: item.symbol):
        price = _market_price(raw_prices, held.symbol)
        if price is None:
            continue
        intent = _close_intent(held, MARGIN_CALL, account.id)
        try:
            candidate_account = project_account_for_intent(account, intent, raw_prices)
            _value_risk_account(
                candidate_account,
                raw_prices,
                equity_override=exact_equity,
            )
        except (TypeError, ValueError) as exc:
            diagnostics.append(
                {
                    "code": "CANDIDATE_PROJECTION_FAILED",
                    "symbol": held.symbol,
                    "message": str(exc),
                }
            )
            continue
        risk_contribution = price * held.quantity
        if not math.isfinite(risk_contribution):
            diagnostics.append(
                {"code": "CANDIDATE_NONFINITE", "symbol": held.symbol}
            )
            continue
        candidates.append(
            ForcedDeleveragingCandidate(
                symbol=held.symbol,
                margin_released=float(
                    Decimal(str(price))
                    * held.quantity
                    * threshold_decimal
                    / Decimal(100)
                ),
                risk_contribution=risk_contribution,
                estimated_transaction_cost=(
                    risk_contribution * FORCED_DELEVERAGING_COST_RATE
                ),
                intent=intent,
            )
        )
    candidates.sort(key=lambda item: item.sort_key)

    projected_account = account
    final_rate = initial_rate
    planned: list[OrderIntent] = []
    for candidate in candidates:
        try:
            projected_account = project_account_for_intent(
                projected_account,
                candidate.intent,
                raw_prices,
            )
            _, _, _, _, final_rate = _value_risk_account(
                projected_account,
                raw_prices,
                equity_override=exact_equity,
            )
        except (TypeError, ValueError) as exc:
            diagnostics.append(
                {
                    "code": "DELEVERAGING_PROJECTION_FAILED",
                    "symbol": candidate.symbol,
                    "message": str(exc),
                }
            )
            continue
        planned.append(candidate.intent)
        if final_rate >= threshold:
            break

    return ForcedDeleveragingResult(
        state=MARGIN_CALL,
        intents=tuple(planned),
        candidates=tuple(candidates),
        diagnostics=tuple(diagnostics),
        missing_price_symbols=missing,
        initial_margin_rate_pct=initial_rate,
        final_margin_rate_pct=final_rate,
    )


def plan_forced_deleveraging(
    account: AccountSnapshot,
    prices: MarketSnapshot | Mapping[str, object],
    margin_policy: MarginPolicy | None = None,
) -> tuple[OrderIntent, ...]:
    """Compatibility helper returning only executable deleveraging intents."""

    return evaluate_forced_deleveraging(account, prices, margin_policy).intents


def _quote_for(
    raw_prices: Mapping[str, object],
    symbol: str,
) -> Mapping[str, object]:
    raw = raw_prices.get(symbol, {})
    return raw if isinstance(raw, Mapping) else {"price": raw}


def _merge_mode(current: str, proposed: str) -> str:
    _validate_position_mode(current)
    _validate_position_mode(proposed)
    return COVER_ONLY if COVER_ONLY in {current, proposed} else NORMAL


class PortfolioRiskStage:
    """Pure pipeline adapter for position, drawdown, and margin-call risk."""

    name = "portfolio_risk"
    component_version = "2.0.0"

    def __init__(
        self,
        account: AccountSnapshot,
        market_or_prices: MarketSnapshot | Mapping[str, object],
        *,
        peak_equity: object,
        policies: PositionRiskPolicies | ShortPolicy | Mapping[str, object] | None = None,
        short_policy: ShortPolicy | None = None,
        margin_policy: MarginPolicy | None = None,
        borrow_position_modes: Mapping[str, str] | None = None,
    ) -> None:
        if type(account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        self._account = account
        self._market_or_prices = market_or_prices
        self._raw_prices = _raw_prices(market_or_prices)
        self._peak_equity = peak_equity
        self._policies = _resolve_position_policies(policies)
        self._short_policy = _short_policy(short_policy)
        self._margin_policy = _margin_policy(margin_policy)[0]
        if borrow_position_modes is None:
            borrow_position_modes = {}
        if not isinstance(borrow_position_modes, Mapping):
            raise TypeError("borrow_position_modes must be a mapping")
        modes: dict[str, str] = {}
        for symbol, mode in borrow_position_modes.items():
            if type(symbol) is not str or not symbol:
                raise ValueError("borrow position mode symbol must be non-empty")
            modes[symbol] = _validate_position_mode(mode, "borrow position mode")
        self._borrow_position_modes = MappingProxyType(modes)

    def _upstream_modes(self, stage_input: StageInput) -> dict[str, str]:
        matching = [
            fact
            for fact in stage_input.upstream_facts
            if isinstance(fact, Mapping) and fact.get("kind") == "position_modes"
        ]
        if len(matching) > 1:
            raise PipelineContractError("duplicate upstream fact: position_modes")
        if not matching:
            return {}
        items = matching[0].get("items", {})
        if not isinstance(items, Mapping):
            raise PipelineContractError("position_modes items must be a mapping")
        modes: dict[str, str] = {}
        try:
            for symbol, mode in items.items():
                if type(symbol) is not str or not symbol:
                    raise RiskError("position_modes symbol must be non-empty")
                modes[symbol] = _validate_position_mode(mode)
        except (TypeError, ValueError) as exc:
            raise PipelineContractError(str(exc)) from exc
        return modes

    def evaluate(self, stage_input: StageInput) -> StageOutput:
        if type(stage_input) is not StageInput:
            raise TypeError("stage_input must be StageInput")
        upstream_modes = self._upstream_modes(stage_input)
        modes = dict(self._borrow_position_modes)
        for symbol, mode in upstream_modes.items():
            modes[symbol] = _merge_mode(modes.get(symbol, NORMAL), mode)

        position_intents: list[OrderIntent] = []
        position_updates: list[PositionRiskUpdate] = []
        diagnostics: list[dict[str, object]] = []
        for held in sorted(self._account.positions, key=lambda item: item.symbol):
            mode = _merge_mode(
                held.position_mode,
                modes.get(held.symbol, NORMAL),
            )
            price = _market_price(self._raw_prices, held.symbol)
            if price is None:
                modes[held.symbol] = mode
                position_updates.append(
                    PositionRiskUpdate.from_position(
                        replace(held, position_mode=mode)
                    )
                )
                diagnostics.append(
                    {"code": "MISSING_PRICE", "symbol": held.symbol}
                )
                continue
            valued = replace(held, current_price=price, position_mode=mode)
            try:
                position_result = evaluate_position_risk(
                    valued,
                    self._policies,
                    snapshot_id=stage_input.portfolio_snapshot_id,
                )
                squeeze = evaluate_squeeze(
                    position_result.updated_position,
                    _quote_for(self._raw_prices, held.symbol),
                    self._short_policy,
                )
            except (TypeError, ValueError) as exc:
                diagnostics.append(
                    {
                        "code": "POSITION_RISK_INVALID",
                        "symbol": held.symbol,
                        "message": str(exc),
                    }
                )
                modes[held.symbol] = mode
                position_updates.append(
                    PositionRiskUpdate.from_position(
                        replace(held, position_mode=mode)
                    )
                )
                continue
            mode = _merge_mode(mode, squeeze.position_mode)
            modes[held.symbol] = mode
            position_intents.extend(position_result.intents)
            position_updates.append(
                PositionRiskUpdate.from_position(
                    replace(squeeze.updated_position, position_mode=mode)
                )
            )
            diagnostics.append(
                {
                    "code": "POSITION_RISK",
                    "symbol": held.symbol,
                    "reason": (
                        position_result.decisions[0].reason
                        if position_result.decisions
                        else squeeze.reason
                    ),
                }
            )

        try:
            metrics, _, _, _, _ = _value_risk_account(
                self._account, self._raw_prices
            )
            drawdown = evaluate_portfolio_drawdown(metrics, self._peak_equity)
            forced = evaluate_forced_deleveraging(
                self._account,
                self._market_or_prices,
                self._margin_policy,
            )
        except (TypeError, ValueError) as exc:
            raise PipelineContractError(str(exc)) from exc
        diagnostics.append(dict(drawdown.diagnostic))
        diagnostics.extend(dict(item) for item in forced.diagnostics)
        diagnostics.append(
            {
                "code": "MARGIN_RISK",
                "state": forced.state,
                "initial_margin_rate_pct": forced.initial_margin_rate_pct,
                "final_margin_rate_pct": forced.final_margin_rate_pct,
            }
        )

        exited_symbols = {item.symbol for item in position_intents}
        if INSOLVENT_HALT in {drawdown.state, forced.state}:
            combined: tuple[OrderIntent, ...] = ()
        else:
            combined = tuple(position_intents) + tuple(
                item for item in forced.intents if item.symbol not in exited_symbols
            )
        return StageOutput(
            stage=self.name,
            component_version=self.component_version,
            facts=(
                {"kind": "risk_intents", "items": combined},
                {
                    "kind": "risk_diagnostic",
                    "items": tuple(diagnostics),
                    "drawdown_state": drawdown.state,
                    "margin_state": forced.state,
                },
                {
                    "kind": "position_risk_updates",
                    "items": tuple(position_updates),
                },
                {"kind": "position_modes", "items": dict(sorted(modes.items()))},
            ),
        )


__all__ = (
    "COVER_ONLY",
    "DERISK",
    "FORCED_DELEVERAGING_COST_RATE",
    "ForcedDeleveragingCandidate",
    "ForcedDeleveragingResult",
    "INSOLVENT_HALT",
    "LONG_STOP_LOSS",
    "LONG_TRAILING_STOP",
    "MANUAL_HALT",
    "MARGIN_CALL",
    "NORMAL",
    "PortfolioDrawdownResult",
    "PortfolioRiskStage",
    "PositionRiskPolicies",
    "PositionRiskResult",
    "PositionRiskUpdate",
    "REDUCE_ONLY",
    "RiskDecision",
    "RiskError",
    "SHORT_SQUEEZE",
    "SQUEEZE_DATA_INVALID",
    "SHORT_STOP_LOSS",
    "SHORT_TRAILING_STOP",
    "WARNING",
    "default_policies",
    "evaluate_forced_deleveraging",
    "evaluate_portfolio_drawdown",
    "evaluate_position_risk",
    "evaluate_squeeze",
    "plan_forced_deleveraging",
)
