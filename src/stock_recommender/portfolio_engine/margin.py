"""Margin requirement calculations for portfolio positions."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from ..pipeline import PipelineContractError, StageInput, StageOutput
from .config import MarginPolicy
from .contracts import (
    AccountSnapshot,
    MarketSnapshot,
    OrderIntent,
    OrderSide,
    PortfolioMetrics,
    PositionEffect,
    PositionSide,
    PositionSnapshot,
)
from .valuation import value_account


NORMAL = "NORMAL"
REDUCE_ONLY = "REDUCE_ONLY"
MARGIN_CALL = "MARGIN_CALL"


class MarketPriceMissingError(ValueError):
    """Raised when margin projection lacks a valid current market price."""


class _ImmutableMarginValue:
    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, object]):
        memo[id(self)] = self
        return self


def _finite_nonnegative(value: object, field_name: str) -> float:
    if type(value) not in (int, float):
        raise TypeError(f"{field_name} must be an int or float")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} must be finite and nonnegative")
    return 0.0 if number == 0.0 else number


def _nonnegative_difference(required: float, equity: float) -> float:
    if math.isclose(required, equity, rel_tol=1e-12, abs_tol=1e-12):
        return 0.0
    return max(0.0, required - equity)


@dataclass(frozen=True)
class MarginAdmissionResult(_ImmutableMarginValue):
    admitted: bool
    reason: str | None
    state: str
    required_margin: float
    available_buying_power: float
    difference: float
    maintenance_margin_pct: float
    buffer_threshold_pct: float
    current_metrics: PortfolioMetrics | None = None
    projected_metrics: PortfolioMetrics | None = None

    def __post_init__(self) -> None:
        if type(self.admitted) is not bool:
            raise TypeError("admitted must be a bool")
        if self.reason is not None and (type(self.reason) is not str or not self.reason):
            raise ValueError("reason must be a non-empty string or None")
        if self.state not in {NORMAL, REDUCE_ONLY, MARGIN_CALL}:
            raise ValueError(f"unsupported margin state: {self.state}")
        for field_name in (
            "required_margin",
            "available_buying_power",
            "difference",
            "maintenance_margin_pct",
            "buffer_threshold_pct",
        ):
            object.__setattr__(
                self,
                field_name,
                _finite_nonnegative(getattr(self, field_name), field_name),
            )
        if self.buffer_threshold_pct <= 0:
            raise ValueError("buffer_threshold_pct must be positive")
        if self.buffer_threshold_pct < self.maintenance_margin_pct:
            raise ValueError(
                "buffer_threshold_pct must not be below maintenance_margin_pct"
            )
        for field_name in ("current_metrics", "projected_metrics"):
            value = getattr(self, field_name)
            if value is not None and type(value) is not PortfolioMetrics:
                raise TypeError(f"{field_name} must be PortfolioMetrics or None")
        if self.admitted and self.reason is not None:
            raise ValueError("an admitted margin result must not have a reason")
        if not self.admitted and self.reason is None:
            raise ValueError("a rejected margin result must have a reason")
        if (self.current_metrics is None) != (self.projected_metrics is None):
            raise ValueError(
                "current_metrics and projected_metrics must be provided together"
            )
        if self.current_metrics is not None and self.projected_metrics is not None:
            ratio = self.buffer_threshold_pct / 100.0
            projected_gross = (
                self.projected_metrics.long_market_value
                + self.projected_metrics.short_liability
            )
            current_gross = (
                self.current_metrics.long_market_value
                + self.current_metrics.short_liability
            )
            expected = {
                "required_margin": projected_gross * ratio,
                "available_buying_power": max(
                    0.0,
                    self.current_metrics.equity / ratio - current_gross,
                ),
                "difference": max(
                    0.0,
                    _nonnegative_difference(
                        projected_gross * ratio,
                        self.projected_metrics.equity,
                    ),
                ),
            }
            for field_name, expected_value in expected.items():
                if not math.isclose(
                    getattr(self, field_name),
                    expected_value,
                    rel_tol=1e-12,
                    abs_tol=1e-9,
                ):
                    raise ValueError(
                        f"{field_name} is inconsistent with margin metrics"
                    )
            expected_state = _margin_state(
                self.projected_metrics,
                self.maintenance_margin_pct,
                self.buffer_threshold_pct,
            )
            if self.state != expected_state:
                raise ValueError("state is inconsistent with projected_metrics")


def _validate_policy(policy: object) -> tuple[MarginPolicy, float, float]:
    if type(policy) is not MarginPolicy:
        raise TypeError("policy must be MarginPolicy")
    maintenance = _finite_nonnegative(
        policy.maintenance_margin_pct, "maintenance_margin_pct"
    )
    buffer = _finite_nonnegative(
        policy.liquidation_buffer_pct, "liquidation_buffer_pct"
    )
    threshold = maintenance + buffer
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("margin buffer threshold must be finite and positive")
    return policy, maintenance, threshold


def _validate_intent_semantics(
    account: AccountSnapshot,
    intent: OrderIntent,
) -> PositionSnapshot | None:
    if type(account) is not AccountSnapshot:
        raise TypeError("account must be AccountSnapshot")
    if type(intent) is not OrderIntent:
        raise TypeError("intent must be OrderIntent")
    expected_order_side = (
        OrderSide.BUY
        if (
            intent.position_side is PositionSide.LONG
            and intent.position_effect in {PositionEffect.OPEN, PositionEffect.INCREASE}
        )
        or (
            intent.position_side is PositionSide.SHORT
            and intent.position_effect in {PositionEffect.REDUCE, PositionEffect.CLOSE}
        )
        else OrderSide.SELL
    )
    if intent.order_side is not expected_order_side:
        raise ValueError("order_side is inconsistent with position side and effect")

    existing = next(
        (position for position in account.positions if position.symbol == intent.symbol),
        None,
    )
    if existing is not None and existing.side is not intent.position_side:
        raise ValueError("intent position side conflicts with the existing position")
    if intent.position_effect is PositionEffect.OPEN and existing is not None:
        raise ValueError("OPEN intent requires no existing position")
    if intent.position_effect is PositionEffect.INCREASE and existing is None:
        raise ValueError("INCREASE intent requires an existing position")
    if intent.position_effect in {PositionEffect.REDUCE, PositionEffect.CLOSE}:
        if existing is None:
            raise ValueError("risk-reducing intent requires an existing position")
        if intent.position_effect is PositionEffect.CLOSE:
            if intent.quantity != existing.quantity:
                raise ValueError("CLOSE quantity must equal the existing quantity")
        elif intent.quantity >= existing.quantity:
            raise ValueError("REDUCE quantity must be smaller than the existing quantity")
    return existing


def _raw_prices(market_or_prices: object) -> Mapping[str, object]:
    if type(market_or_prices) is MarketSnapshot:
        return market_or_prices.quotes
    if not isinstance(market_or_prices, Mapping):
        raise TypeError("market_or_prices must be MarketSnapshot or a price mapping")
    return market_or_prices


def _price_for(raw_prices: Mapping[str, object], symbol: str) -> float:
    if symbol not in raw_prices:
        raise MarketPriceMissingError(f"market price missing for {symbol}")
    raw = raw_prices[symbol]
    if isinstance(raw, Mapping):
        if "price" not in raw:
            raise MarketPriceMissingError(f"market price missing for {symbol}")
        raw = raw["price"]
    if type(raw) not in (int, float):
        raise MarketPriceMissingError(f"market price missing for {symbol}")
    price = float(raw)
    if not math.isfinite(price) or price <= 0:
        raise MarketPriceMissingError(f"market price missing for {symbol}")
    return price


def _valuation_prices(
    account: AccountSnapshot,
    raw_prices: Mapping[str, object],
) -> dict[str, float]:
    return {
        position.symbol: _price_for(raw_prices, position.symbol)
        for position in account.positions
    }


def _updated_positions(
    account: AccountSnapshot,
    intent: OrderIntent,
    existing: PositionSnapshot | None,
    price: float,
) -> tuple[PositionSnapshot, ...]:
    positions = list(account.positions)
    if intent.position_effect in {PositionEffect.OPEN, PositionEffect.INCREASE}:
        if existing is None:
            positions.append(
                PositionSnapshot(
                    symbol=intent.symbol,
                    side=intent.position_side,
                    quantity=intent.quantity,
                    average_cost=price,
                )
            )
        else:
            quantity = existing.quantity + intent.quantity
            average_cost = (
                existing.average_cost * existing.quantity + price * intent.quantity
            ) / quantity
            positions[positions.index(existing)] = replace(
                existing,
                quantity=quantity,
                average_cost=average_cost,
                current_price=None,
            )
    elif intent.position_effect is PositionEffect.CLOSE:
        if existing is None:
            raise RuntimeError("validated CLOSE intent lost its existing position")
        positions.remove(existing)
    else:
        if existing is None:
            raise RuntimeError("validated REDUCE intent lost its existing position")
        positions[positions.index(existing)] = replace(
            existing,
            quantity=existing.quantity - intent.quantity,
            current_price=None,
        )
    return tuple(positions)


def project_account_for_intent(
    account: AccountSnapshot,
    intent: OrderIntent,
    market_or_prices: MarketSnapshot | Mapping[str, object],
) -> AccountSnapshot:
    """Project one full-fill intent without mutating or committing the account."""

    existing = _validate_intent_semantics(account, intent)
    raw_prices = _raw_prices(market_or_prices)
    price = _price_for(raw_prices, intent.symbol)
    notional = price * intent.quantity
    if not math.isfinite(notional):
        raise ValueError("intent notional must be finite")

    available_cash = float(account.available_cash)
    restricted = float(account.restricted_short_proceeds)
    margin_loan = float(account.margin_loan)
    increasing = intent.increases_risk
    if intent.position_side is PositionSide.LONG:
        if increasing:
            cash_used = min(max(0.0, available_cash), notional)
            available_cash -= cash_used
            margin_loan += notional - cash_used
        else:
            loan_repayment = min(margin_loan, notional)
            margin_loan -= loan_repayment
            available_cash += notional - loan_repayment
    elif increasing:
        restricted += notional
    else:
        restricted_used = min(restricted, notional)
        restricted -= restricted_used
        remainder = notional - restricted_used
        cash_used = min(max(0.0, available_cash), remainder)
        available_cash -= cash_used
        margin_loan += remainder - cash_used

    return replace(
        account,
        available_cash=available_cash,
        restricted_short_proceeds=restricted,
        margin_loan=margin_loan,
        positions=_updated_positions(account, intent, existing, price),
    )


def _margin_state(
    metrics: PortfolioMetrics,
    maintenance_pct: float,
    buffer_threshold_pct: float,
) -> str:
    gross = metrics.long_market_value + metrics.short_liability
    if gross == 0:
        return NORMAL
    below_maintenance = metrics.margin_rate_pct < maintenance_pct and not math.isclose(
        metrics.margin_rate_pct,
        maintenance_pct,
        rel_tol=1e-12,
        abs_tol=1e-12,
    )
    if metrics.equity <= 0 or below_maintenance:
        return MARGIN_CALL
    below_buffer = metrics.margin_rate_pct < buffer_threshold_pct and not math.isclose(
        metrics.margin_rate_pct,
        buffer_threshold_pct,
        rel_tol=1e-12,
        abs_tol=1e-12,
    )
    if below_buffer:
        return REDUCE_ONLY
    return NORMAL


def _amounts(
    current: PortfolioMetrics,
    projected: PortfolioMetrics,
    buffer_threshold_pct: float,
) -> tuple[float, float, float]:
    projected_gross = projected.long_market_value + projected.short_liability
    current_gross = current.long_market_value + current.short_liability
    ratio = buffer_threshold_pct / 100.0
    required = projected_gross * ratio
    buying_power = max(0.0, current.equity / ratio - current_gross)
    difference = _nonnegative_difference(required, projected.equity)
    return required, buying_power, difference


def admit_margin(
    account: AccountSnapshot,
    intent: OrderIntent,
    market_or_prices: MarketSnapshot | Mapping[str, object],
    policy: MarginPolicy,
) -> MarginAdmissionResult:
    """Admit one intent using valuation-derived current and projected margin."""

    _, maintenance, threshold = _validate_policy(policy)
    _validate_intent_semantics(account, intent)
    raw_prices = _raw_prices(market_or_prices)
    try:
        current = value_account(account, _valuation_prices(account, raw_prices)).metrics
        projected_account = project_account_for_intent(account, intent, raw_prices)
        projected_prices = _valuation_prices(projected_account, raw_prices)
        projected = value_account(projected_account, projected_prices).metrics
    except MarketPriceMissingError:
        if not intent.increases_risk:
            return MarginAdmissionResult(
                admitted=True,
                reason=None,
                state=REDUCE_ONLY,
                required_margin=0.0,
                available_buying_power=0.0,
                difference=0.0,
                maintenance_margin_pct=maintenance,
                buffer_threshold_pct=threshold,
            )
        return MarginAdmissionResult(
            admitted=False,
            reason="MARKET_PRICE_MISSING",
            state=REDUCE_ONLY,
            required_margin=0.0,
            available_buying_power=0.0,
            difference=0.0,
            maintenance_margin_pct=maintenance,
            buffer_threshold_pct=threshold,
        )

    state = _margin_state(projected, maintenance, threshold)
    required, buying_power, difference = _amounts(current, projected, threshold)
    reason: str | None = None
    admitted = True
    if intent.increases_risk and state is not NORMAL:
        admitted = False
        reason = "MARGIN_CALL" if state == MARGIN_CALL else "MARGIN_BUFFER_BREACH"
    return MarginAdmissionResult(
        admitted=admitted,
        reason=reason,
        state=state,
        required_margin=required,
        available_buying_power=buying_power,
        difference=difference,
        maintenance_margin_pct=maintenance,
        buffer_threshold_pct=threshold,
        current_metrics=current,
        projected_metrics=projected,
    )


def _metrics_fact(metrics: PortfolioMetrics | None) -> dict[str, float] | None:
    if metrics is None:
        return None
    return {
        field_name: getattr(metrics, field_name)
        for field_name in metrics.__dataclass_fields__
    }


class MarginAdmissionStage:
    """Pure pipeline adapter for intent margin admission."""

    name = "margin_admission"
    component_version = "1.0.0"

    def __init__(
        self,
        account: AccountSnapshot,
        market_or_prices: MarketSnapshot | Mapping[str, object],
        policy: MarginPolicy,
    ) -> None:
        if type(account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        _validate_policy(policy)
        _raw_prices(market_or_prices)
        self._account = account
        self._market_or_prices = market_or_prices
        self._policy = policy

    def evaluate(self, stage_input: StageInput) -> StageOutput:
        matching = [
            fact
            for fact in stage_input.upstream_facts
            if isinstance(fact, Mapping) and fact.get("kind") == "order_intents"
        ]
        if len(matching) > 1:
            raise PipelineContractError("duplicate upstream fact: order_intents")
        items: object = () if not matching else matching[0].get("items", ())
        if not isinstance(items, (tuple, list)):
            raise PipelineContractError("order_intents items must be a sequence")
        intents = tuple(items)
        if any(type(item) is not OrderIntent for item in intents):
            raise PipelineContractError("order_intents items must be OrderIntent")
        ids = [item.id for item in intents]
        if len(ids) != len(set(ids)):
            raise PipelineContractError("order_intents must not contain duplicate ids")

        admitted: list[OrderIntent] = []
        rejected: list[OrderIntent] = []
        diagnostics: list[dict[str, object]] = []
        projected_account = self._account
        for item in intents:
            try:
                result = admit_margin(
                    projected_account,
                    item,
                    self._market_or_prices,
                    self._policy,
                )
            except (TypeError, ValueError) as exc:
                raise PipelineContractError(str(exc)) from exc
            if result.admitted:
                admitted.append(item)
                try:
                    projected_account = project_account_for_intent(
                        projected_account,
                        item,
                        self._market_or_prices,
                    )
                except MarketPriceMissingError:
                    pass
            else:
                rejected.append(item)
            diagnostics.append(
                {
                    "intent_id": item.id,
                    "symbol": item.symbol,
                    "admitted": result.admitted,
                    "reason": result.reason,
                    "state": result.state,
                    "required_margin": result.required_margin,
                    "available_buying_power": result.available_buying_power,
                    "difference": result.difference,
                    "maintenance_margin_pct": result.maintenance_margin_pct,
                    "buffer_threshold_pct": result.buffer_threshold_pct,
                    "current_metrics": _metrics_fact(result.current_metrics),
                    "projected_metrics": _metrics_fact(result.projected_metrics),
                }
            )
        return StageOutput(
            stage=self.name,
            component_version=self.component_version,
            facts=(
                {"kind": "margin_admitted_intents", "items": tuple(admitted)},
                {"kind": "margin_rejected_intents", "items": tuple(rejected)},
                {"kind": "margin_diagnostics", "items": tuple(diagnostics)},
            ),
        )


__all__ = (
    "MARGIN_CALL",
    "NORMAL",
    "REDUCE_ONLY",
    "MarginAdmissionResult",
    "MarginAdmissionStage",
    "MarketPriceMissingError",
    "admit_margin",
    "project_account_for_intent",
)
