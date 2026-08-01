"""Pure, current-snapshot hard admission before simulated execution."""

from __future__ import annotations

import math
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
from .valuation import ValuationError, value_account


_TOLERANCE = 1e-9


def _prices_for_account(
    account: AccountSnapshot,
    market: MarketSnapshot,
) -> dict[str, object]:
    prices: dict[str, object] = {}
    for held in account.positions:
        quote = market.quotes.get(held.symbol)
        if not isinstance(quote, Mapping) or quote.get("price") is None:
            raise ValuationError(f"missing price for held symbol: {held.symbol}")
        prices[held.symbol] = quote["price"]
    return prices


def hard_cap_breaches(
    account: AccountSnapshot,
    market: MarketSnapshot,
    exposure: ExposurePolicy,
    margin: MarginPolicy,
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
    try:
        valuation = value_account(account, _prices_for_account(account, market))
    except (TypeError, ValueError, ValuationError):
        return ("VALUATION_UNAVAILABLE",)
    metrics = valuation.metrics
    breaches: list[str] = []
    if metrics.equity <= 0 or not math.isfinite(metrics.equity):
        breaches.append("NON_POSITIVE_EQUITY")
    if len(valuation.positions) > exposure.max_positions:
        breaches.append("POSITION_COUNT_CAP")
    if exposure.mode == "LONG_ONLY" and any(
        held.side is PositionSide.SHORT for held in valuation.positions
    ):
        breaches.append("LONG_ONLY_SHORT_FORBIDDEN")
    comparisons = (
        (metrics.gross_exposure_pct, exposure.max_gross_exposure_pct, "GROSS_EXPOSURE_CAP"),
        (abs(metrics.net_exposure_pct), exposure.max_net_exposure_pct, "NET_EXPOSURE_CAP"),
        (metrics.long_exposure_pct, exposure.max_long_exposure_pct, "LONG_EXPOSURE_CAP"),
        (metrics.short_exposure_pct, exposure.max_short_exposure_pct, "SHORT_EXPOSURE_CAP"),
    )
    for actual, maximum, reason in comparisons:
        if not math.isfinite(actual) or actual > maximum + _TOLERANCE:
            breaches.append(reason)
    if metrics.equity > 0:
        for held in valuation.positions:
            quote = market.quotes[held.symbol]
            price = float(quote["price"])
            exposure_pct = held.quantity * price / metrics.equity * 100.0
            maximum = (
                exposure.max_long_position_pct
                if held.side is PositionSide.LONG
                else exposure.max_short_position_pct
            )
            if exposure_pct > maximum + _TOLERANCE:
                breaches.append(
                    "LONG_POSITION_CAP"
                    if held.side is PositionSide.LONG
                    else "SHORT_POSITION_CAP"
                )
    gross = metrics.long_market_value + metrics.short_liability
    buffer_threshold = (
        margin.maintenance_margin_pct + margin.liquidation_buffer_pct
    )
    if gross > 0 and (
        not math.isfinite(metrics.margin_rate_pct)
        or metrics.margin_rate_pct + _TOLERANCE < buffer_threshold
    ):
        breaches.append("MARGIN_BUFFER_BREACH")
    return tuple(dict.fromkeys(breaches))


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
            (*[item for item in intents if not item.increases_risk],
             *[item for item in intents if item.increases_risk])
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
