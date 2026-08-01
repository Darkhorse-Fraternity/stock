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
    long_position_max: Decimal
    short_position_max: Decimal
    position_count: int


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
    for held in account.positions:
        quote = market.quotes.get(held.symbol)
        if not isinstance(quote, Mapping) or quote.get("price") is None:
            raise ValueError(f"missing price for held symbol: {held.symbol}")
        price = _decimal(quote["price"], f"quote[{held.symbol}].price")
        if price <= 0:
            raise ValueError(f"quote[{held.symbol}].price must be positive")
        market_value = Decimal(held.quantity) * price
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
        long_position_max=max(long_values, default=Decimal(0)),
        short_position_max=max(short_values, default=Decimal(0)),
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
) -> tuple[str, ...]:
    breaches: list[str] = []
    if state.equity <= 0:
        breaches.append("NON_POSITIVE_EQUITY")
    if state.position_count > exposure.max_positions:
        breaches.append("POSITION_COUNT_CAP")
    if exposure.mode == "LONG_ONLY" and state.short_value > 0:
        breaches.append("LONG_ONLY_SHORT_FORBIDDEN")
    comparisons = (
        (state.gross, exposure.max_gross_exposure_pct, "GROSS_EXPOSURE_CAP"),
        (state.net_absolute, exposure.max_net_exposure_pct, "NET_EXPOSURE_CAP"),
        (state.long_value, exposure.max_long_exposure_pct, "LONG_EXPOSURE_CAP"),
        (state.short_value, exposure.max_short_exposure_pct, "SHORT_EXPOSURE_CAP"),
        (
            state.long_position_max,
            exposure.max_long_position_pct,
            "LONG_POSITION_CAP",
        ),
        (
            state.short_position_max,
            exposure.max_short_position_pct,
            "SHORT_POSITION_CAP",
        ),
    )
    for numerator, maximum, reason in comparisons:
        if _exceeds_percentage(numerator, state.equity, maximum):
            breaches.append(reason)
    threshold = (
        _decimal(margin.maintenance_margin_pct, "maintenance_margin_pct")
        + _decimal(margin.liquidation_buffer_pct, "liquidation_buffer_pct")
    )
    if state.gross > 0 and state.equity * Decimal(100) < state.gross * threshold:
        breaches.append("MARGIN_BUFFER_BREACH")
    return tuple(dict.fromkeys(breaches))


def _breach_worsened(
    reason: str,
    current: _DecimalCapState,
    baseline: _DecimalCapState,
) -> bool:
    if reason == "NON_POSITIVE_EQUITY":
        return current.equity < baseline.equity
    if reason == "POSITION_COUNT_CAP":
        return current.position_count > baseline.position_count
    if reason == "LONG_ONLY_SHORT_FORBIDDEN":
        return current.short_value > baseline.short_value
    numerators = {
        "GROSS_EXPOSURE_CAP": (current.gross, baseline.gross),
        "NET_EXPOSURE_CAP": (current.net_absolute, baseline.net_absolute),
        "LONG_EXPOSURE_CAP": (current.long_value, baseline.long_value),
        "SHORT_EXPOSURE_CAP": (current.short_value, baseline.short_value),
        "LONG_POSITION_CAP": (
            current.long_position_max,
            baseline.long_position_max,
        ),
        "SHORT_POSITION_CAP": (
            current.short_position_max,
            baseline.short_position_max,
        ),
    }
    if reason in numerators:
        current_value, baseline_value = numerators[reason]
        return _percentage(current_value, current.equity) > _percentage(
            baseline_value,
            baseline.equity,
        )
    if reason == "MARGIN_BUFFER_BREACH":
        current_rate = (
            Decimal("Infinity")
            if current.gross == 0
            else current.equity * Decimal(100) / current.gross
        )
        baseline_rate = (
            Decimal("Infinity")
            if baseline.gross == 0
            else baseline.equity * Decimal(100) / baseline.gross
        )
        return current_rate < baseline_rate
    return True


def hard_cap_breaches(
    account: AccountSnapshot,
    market: MarketSnapshot,
    exposure: ExposurePolicy,
    margin: MarginPolicy,
    *,
    baseline_account: AccountSnapshot | None = None,
) -> tuple[str, ...]:
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
        return ("VALUATION_UNAVAILABLE",)
    breaches = _raw_hard_cap_breaches(state, exposure, margin)
    if baseline is None:
        return breaches
    baseline_breaches = set(_raw_hard_cap_breaches(baseline, exposure, margin))
    return tuple(
        reason
        for reason in breaches
        if reason not in baseline_breaches
        or _breach_worsened(reason, state, baseline)
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
                        "reason": breaches[0],
                        "breaches": breaches,
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


__all__ = ("PreExecutionAdmissionStage", "hard_cap_breaches")
