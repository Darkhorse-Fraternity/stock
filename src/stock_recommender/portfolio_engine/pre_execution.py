"""Pure, current-snapshot hard admission before simulated execution."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

from ..pipeline import PipelineContractError, StageInput, StageOutput
from .borrow import BorrowSnapshot, borrow_security_failure
from .config import ExposurePolicy, MarginPolicy, ShortPolicy
from .contracts import (
    AccountSnapshot,
    MarketSnapshot,
    OrderExecutionProgress,
    OrderIntent,
    PositionSide,
)
from .execution import ExecutionPolicy, execute_intents


@dataclass(frozen=True)
class _DecimalCapState:
    equity: Decimal
    gross: Decimal
    net_absolute: Decimal
    long_value: Decimal
    short_value: Decimal
    positions: tuple[tuple[str, PositionSide, Decimal], ...]
    position_count: int


@dataclass(frozen=True)
class HardCapBreach:
    """One exact hard-cap violation with a stable comparison identity."""

    code: str
    key: tuple[str, ...]
    actual: Decimal
    maximum: Decimal

    def __post_init__(self) -> None:
        if type(self.code) is not str or not self.code:
            raise ValueError("hard-cap breach code must be non-empty")
        if (
            type(self.key) is not tuple
            or not self.key
            or any(type(item) is not str or not item for item in self.key)
        ):
            raise ValueError("hard-cap breach key must contain non-empty strings")
        if self.key[0] != self.code:
            raise ValueError("hard-cap breach key must start with its code")
        if type(self.actual) is not Decimal or type(self.maximum) is not Decimal:
            raise TypeError("hard-cap breach actual and maximum must be Decimal")


def _decimal(value: object, field_name: str) -> Decimal:
    if type(value) not in {int, float}:
        raise ValueError(f"{field_name} must be numeric")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} must be decimal-compatible") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def _cap_state(
    account: AccountSnapshot,
    market: MarketSnapshot,
) -> _DecimalCapState:
    long_values: list[Decimal] = []
    short_values: list[Decimal] = []
    position_values: list[tuple[str, PositionSide, Decimal]] = []
    for held in account.positions:
        quote = market.quotes.get(held.symbol)
        if not isinstance(quote, Mapping) or quote.get("price") is None:
            raise ValueError(f"missing price for held symbol: {held.symbol}")
        price = _decimal(quote["price"], f"quote[{held.symbol}].price")
        if price <= 0:
            raise ValueError(f"quote[{held.symbol}].price must be positive")
        market_value = Decimal(held.quantity) * price
        position_values.append((held.symbol, held.side, market_value))
        if held.side is PositionSide.LONG:
            long_values.append(market_value)
        else:
            short_values.append(market_value)
    long_value = sum(long_values, Decimal(0))
    short_value = sum(short_values, Decimal(0))
    equity = (
        _decimal(account.available_cash, "available_cash")
        + _decimal(
            account.restricted_short_proceeds,
            "restricted_short_proceeds",
        )
        + long_value
        - short_value
        - _decimal(account.margin_loan, "margin_loan")
    )
    return _DecimalCapState(
        equity=equity,
        gross=long_value + short_value,
        net_absolute=abs(long_value - short_value),
        long_value=long_value,
        short_value=short_value,
        positions=tuple(
            sorted(position_values, key=lambda item: (item[1].value, item[0]))
        ),
        position_count=len(account.positions),
    )


def _exceeds_percentage(
    numerator: Decimal,
    equity: Decimal,
    maximum_pct: float,
) -> bool:
    if numerator == 0:
        return False
    if equity <= 0:
        return True
    return numerator * Decimal(100) > equity * _decimal(maximum_pct, "maximum_pct")


def _percentage(numerator: Decimal, equity: Decimal) -> Decimal:
    if numerator == 0:
        return Decimal(0)
    if equity <= 0:
        return Decimal("Infinity")
    return numerator * Decimal(100) / equity


def _raw_hard_cap_breaches(
    state: _DecimalCapState,
    exposure: ExposurePolicy,
    margin: MarginPolicy,
) -> tuple[HardCapBreach, ...]:
    breaches: list[HardCapBreach] = []
    if state.equity <= 0:
        breaches.append(
            HardCapBreach(
                code="NON_POSITIVE_EQUITY",
                key=("NON_POSITIVE_EQUITY",),
                actual=max(Decimal(0), -state.equity),
                maximum=Decimal(0),
            )
        )
    if state.position_count > exposure.max_positions:
        breaches.append(
            HardCapBreach(
                code="POSITION_COUNT_CAP",
                key=("POSITION_COUNT_CAP",),
                actual=Decimal(state.position_count),
                maximum=Decimal(exposure.max_positions),
            )
        )
    if exposure.mode == "LONG_ONLY" and state.short_value > 0:
        breaches.append(
            HardCapBreach(
                code="LONG_ONLY_SHORT_FORBIDDEN",
                key=("LONG_ONLY_SHORT_FORBIDDEN",),
                actual=_percentage(state.short_value, state.equity),
                maximum=Decimal(0),
            )
        )
    comparisons = (
        (state.gross, exposure.max_gross_exposure_pct, "GROSS_EXPOSURE_CAP"),
        (state.net_absolute, exposure.max_net_exposure_pct, "NET_EXPOSURE_CAP"),
        (state.long_value, exposure.max_long_exposure_pct, "LONG_EXPOSURE_CAP"),
        (state.short_value, exposure.max_short_exposure_pct, "SHORT_EXPOSURE_CAP"),
    )
    for numerator, maximum, reason in comparisons:
        if _exceeds_percentage(numerator, state.equity, maximum):
            breaches.append(
                HardCapBreach(
                    code=reason,
                    key=(reason,),
                    actual=_percentage(numerator, state.equity),
                    maximum=_decimal(maximum, f"{reason}.maximum"),
                )
            )
    for symbol, side, market_value in state.positions:
        maximum = (
            exposure.max_long_position_pct
            if side is PositionSide.LONG
            else exposure.max_short_position_pct
        )
        if _exceeds_percentage(market_value, state.equity, maximum):
            code = (
                "LONG_POSITION_CAP"
                if side is PositionSide.LONG
                else "SHORT_POSITION_CAP"
            )
            breaches.append(
                HardCapBreach(
                    code=code,
                    key=(code, side.value, symbol),
                    actual=_percentage(market_value, state.equity),
                    maximum=_decimal(maximum, f"{code}.maximum"),
                )
            )
    threshold = (
        _decimal(margin.maintenance_margin_pct, "maintenance_margin_pct")
        + _decimal(margin.liquidation_buffer_pct, "liquidation_buffer_pct")
    )
    if state.gross > 0 and state.equity * Decimal(100) < state.gross * threshold:
        margin_rate = state.equity * Decimal(100) / state.gross
        breaches.append(
            HardCapBreach(
                code="MARGIN_BUFFER_BREACH",
                key=("MARGIN_BUFFER_BREACH",),
                actual=threshold - margin_rate,
                maximum=Decimal(0),
            )
        )
    return tuple(breaches)


def hard_cap_breaches(
    account: AccountSnapshot,
    market: MarketSnapshot,
    exposure: ExposurePolicy,
    margin: MarginPolicy,
    *,
    baseline_account: AccountSnapshot | None = None,
) -> tuple[HardCapBreach, ...]:
    """Return deterministic hard-cap breach codes for one valued account."""

    if type(account) is not AccountSnapshot:
        raise TypeError("account must be AccountSnapshot")
    if type(market) is not MarketSnapshot:
        raise TypeError("market must be MarketSnapshot")
    if type(exposure) is not ExposurePolicy:
        raise TypeError("exposure must be ExposurePolicy")
    if type(margin) is not MarginPolicy:
        raise TypeError("margin must be MarginPolicy")
    if baseline_account is not None and type(baseline_account) is not AccountSnapshot:
        raise TypeError("baseline_account must be AccountSnapshot or None")
    try:
        state = _cap_state(account, market)
        baseline = (
            None
            if baseline_account is None
            else _cap_state(baseline_account, market)
        )
    except (TypeError, ValueError, InvalidOperation):
        return (
            HardCapBreach(
                code="VALUATION_UNAVAILABLE",
                key=("VALUATION_UNAVAILABLE",),
                actual=Decimal("Infinity"),
                maximum=Decimal(0),
            ),
        )
    breaches = _raw_hard_cap_breaches(state, exposure, margin)
    if baseline is None:
        return breaches
    baseline_by_key = {
        breach.key: breach
        for breach in _raw_hard_cap_breaches(baseline, exposure, margin)
    }
    return tuple(
        breach
        for breach in breaches
        if breach.key not in baseline_by_key
        or breach.actual > baseline_by_key[breach.key].actual
    )


class PreExecutionAdmissionStage:
    """Re-admit persisted intents against the current executable snapshot."""

    name = "pre_execution_admission"
    component_version = "1.1.0"

    def __init__(
        self,
        account: AccountSnapshot,
        market: MarketSnapshot,
        exposure: ExposurePolicy,
        margin: MarginPolicy,
        execution_policy: ExecutionPolicy,
        *,
        borrow_snapshot: BorrowSnapshot,
        short_policy: ShortPolicy,
    ) -> None:
        if type(account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        if type(market) is not MarketSnapshot:
            raise TypeError("market must be MarketSnapshot")
        if type(exposure) is not ExposurePolicy:
            raise TypeError("exposure must be ExposurePolicy")
        if type(margin) is not MarginPolicy:
            raise TypeError("margin must be MarginPolicy")
        if type(execution_policy) is not ExecutionPolicy:
            raise TypeError("execution_policy must be ExecutionPolicy")
        if type(borrow_snapshot) is not BorrowSnapshot:
            raise TypeError("borrow_snapshot must be BorrowSnapshot")
        if type(short_policy) is not ShortPolicy:
            raise TypeError("short_policy must be ShortPolicy")
        self._account = account
        self._market = market
        self._exposure = exposure
        self._margin = margin
        self._execution_policy = execution_policy
        self._borrow_snapshot = borrow_snapshot
        self._short_policy = short_policy

    def evaluate(self, stage_input: StageInput) -> StageOutput:
        if type(stage_input) is not StageInput:
            raise TypeError("stage_input must be StageInput")
        intent_facts = [
            fact
            for fact in stage_input.upstream_facts
            if isinstance(fact, Mapping) and fact.get("kind") == "order_intents"
        ]
        progress_facts = [
            fact
            for fact in stage_input.upstream_facts
            if isinstance(fact, Mapping)
            and fact.get("kind") == "execution_progress"
        ]
        if len(intent_facts) != 1 or len(progress_facts) > 1:
            raise PipelineContractError(
                "pre-execution admission requires one intent fact and at most one progress fact"
            )
        raw_intents = intent_facts[0].get("items", ())
        raw_progress = (
            () if not progress_facts else progress_facts[0].get("items", ())
        )
        if not isinstance(raw_intents, (tuple, list)) or any(
            type(item) is not OrderIntent for item in raw_intents
        ):
            raise PipelineContractError("order_intents must contain OrderIntent values")
        if not isinstance(raw_progress, (tuple, list)) or any(
            type(item) is not OrderExecutionProgress for item in raw_progress
        ):
            raise PipelineContractError(
                "execution_progress must contain OrderExecutionProgress values"
            )
        intents = tuple(raw_intents)
        progress = tuple(raw_progress)
        progress_by_id = {item.intent_id: item for item in progress}
        if len(progress_by_id) != len(progress):
            raise PipelineContractError("execution_progress contains duplicate intents")

        ordered = tuple(
            sorted(
                intents,
                key=lambda item: (
                    1 if item.increases_risk else 0,
                    item.created_market_at,
                    item.symbol,
                    item.id,
                ),
            )
        )
        projected = self._account
        admitted: list[OrderIntent] = []
        rejected: list[OrderIntent] = []
        diagnostics: list[dict[str, object]] = []
        reserved_borrow_by_symbol: dict[str, int] = {}
        for item in ordered:
            prior = (
                (progress_by_id[item.id],)
                if item.id in progress_by_id
                else ()
            )
            previously_filled = 0 if not prior else prior[0].filled_quantity
            remaining_quantity = max(0, item.quantity - previously_filled)
            if (
                item.increases_risk
                and item.position_side is PositionSide.SHORT
            ):
                total_requested = (
                    reserved_borrow_by_symbol.get(item.symbol, 0)
                    + remaining_quantity
                )
                borrow_reason, _ = borrow_security_failure(
                    self._borrow_snapshot,
                    item.symbol,
                    self._short_policy,
                    requested_quantity=total_requested,
                )
                if borrow_reason is not None:
                    security = self._borrow_snapshot.securities.get(item.symbol)
                    rejected.append(item)
                    diagnostics.append(
                        {
                            "intent_id": item.id,
                            "symbol": item.symbol,
                            "reason": borrow_reason,
                            "requested_quantity": total_requested,
                            "available_quantity": (
                                None
                                if security is None
                                else security.available_quantity
                            ),
                            "borrow_snapshot_id": self._borrow_snapshot.id,
                        }
                    )
                    continue
            simulation = execute_intents(
                projected,
                (item,),
                self._market,
                self._execution_policy,
                prior_progress=prior,
            )
            if not item.increases_risk:
                admitted.append(item)
                projected = simulation.account
                continue
            if simulation.diagnostics and not simulation.fills:
                reason = simulation.diagnostics[0].reason
                rejected.append(item)
                diagnostics.append(
                    {"intent_id": item.id, "symbol": item.symbol, "reason": reason}
                )
                continue
            breaches = hard_cap_breaches(
                simulation.account,
                self._market,
                self._exposure,
                self._margin,
                baseline_account=projected,
            )
            if breaches:
                rejected.append(item)
                diagnostics.append(
                    {
                        "intent_id": item.id,
                        "symbol": item.symbol,
                        "reason": breaches[0].code,
                        "breaches": tuple(
                            {
                                "code": breach.code,
                                "key": breach.key,
                                "actual": str(breach.actual),
                                "maximum": str(breach.maximum),
                            }
                            for breach in breaches
                        ),
                    }
                )
                continue
            admitted.append(item)
            projected = simulation.account
            if item.position_side is PositionSide.SHORT:
                reserved_borrow_by_symbol[item.symbol] = (
                    reserved_borrow_by_symbol.get(item.symbol, 0)
                    + remaining_quantity
                )
        return StageOutput(
            stage=self.name,
            component_version=self.component_version,
            facts=(
                {"kind": "pre_execution_admitted_intents", "items": tuple(admitted)},
                {"kind": "pre_execution_rejected_intents", "items": tuple(rejected)},
                {"kind": "pre_execution_diagnostics", "items": tuple(diagnostics)},
            ),
        )


__all__ = (
    "HardCapBreach",
    "PreExecutionAdmissionStage",
    "hard_cap_breaches",
)
