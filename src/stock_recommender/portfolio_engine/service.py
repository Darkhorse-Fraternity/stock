"""Application-independent orchestration of portfolio engine workflows."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from ..markets import market_date
from ..pipeline import StageInput, StageOutput
from ..signal_engine import SIGNAL_MODEL_ID
from .borrow import BorrowAdmissionStage
from .config import (
    ExposurePolicy,
    MarginPolicy,
    ShortPolicy,
    effective_exposure_policy,
    normalize_margin_policy,
    normalize_short_policy,
    validate_strategy_policies,
)
from .contracts import (
    AccountSnapshot,
    DecisionBatch,
    MarketSnapshot,
    OrderIntent,
    PlanRequest,
    PortfolioSnapshot,
    PositionRiskUpdate,
    ProcessRequest,
    SignalCandidate,
    TargetPosition,
)
from .execution import (
    ExecutionSimulationStage,
    RebalanceIntentStage,
    accrue_carry_costs,
    execution_policy,
)
from .exposure import ExposureBudgetStage
from .margin import MarginAdmissionStage
from .pre_execution import PreExecutionAdmissionStage, hard_cap_breaches
from .risk import PortfolioRiskStage
from .signal_ports import SIGNAL_MODELS, SignalModel
from .target_pipeline import TargetNettingStage
from .valuation import value_account


PolicyNormalizer = Callable[[object], object]


def _stage_input(
    request: PlanRequest | ProcessRequest,
    upstream_facts: tuple[dict[str, Any], ...],
    *,
    portfolio_snapshot_id: str | None = None,
) -> StageInput:
    return StageInput(
        run_id=request.run_key,
        strategy_id=str(request.strategy["id"]),
        strategy_version=int(request.strategy["revision"]),
        as_of=request.market.occurred_at.isoformat(),
        market_snapshot_id=request.market.id,
        portfolio_snapshot_id=(
            portfolio_snapshot_id
            or request.account.snapshot_id
            or request.account.id
        ),
        upstream_facts=upstream_facts,
    )


def _plain_fact(output: StageOutput) -> tuple[dict[str, Any], ...]:
    return tuple(dict(item) for item in output.facts)


def _fact(output: StageOutput, kind: str) -> Mapping[str, Any]:
    selected = [item for item in output.facts if item.get("kind") == kind]
    if len(selected) != 1:
        raise RuntimeError(f"stage {output.stage} must produce one {kind} fact")
    return selected[0]


def _typed_items(output: StageOutput, kind: str, item_type: type[Any]) -> tuple[Any, ...]:
    raw = _fact(output, kind).get("items", ())
    if not isinstance(raw, (tuple, list)):
        raise TypeError(f"{kind} items must be a sequence")
    items = tuple(raw)
    if any(type(item) is not item_type for item in items):
        raise TypeError(f"{kind} items must be {item_type.__name__}")
    return items


def _diagnostic_sort_key(item: Mapping[str, Any]) -> str:
    return json.dumps(dict(item), ensure_ascii=False, sort_keys=True, default=str)


def _ledger_safe_plain(value: Any) -> Any:
    if type(value) is float and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {key: _ledger_safe_plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_ledger_safe_plain(item) for item in value)
    if isinstance(value, list):
        return tuple(_ledger_safe_plain(item) for item in value)
    return value


def _ledger_safe_output(output: StageOutput) -> StageOutput:
    return StageOutput(
        stage=output.stage,
        component_version=output.component_version,
        schema_version=output.schema_version,
        facts=tuple(_ledger_safe_plain(item) for item in output.facts),
        diagnostics=tuple(_ledger_safe_plain(item) for item in output.diagnostics),
    )


def _signal_output(stage: str, candidates: tuple[SignalCandidate, ...]) -> StageOutput:
    return StageOutput(
        stage=stage,
        component_version="1.0.0",
        facts=({"kind": stage, "items": candidates},),
    )


def _model_from_registry(
    registry: Mapping[str, SignalModel],
    model_id: str,
) -> SignalModel:
    try:
        return registry[model_id]
    except KeyError:
        raise KeyError(f"unregistered signal model: {model_id}") from None


class PortfolioEngine:
    """Thin service coordinating pure decisions and one ledger transaction."""

    def __init__(
        self,
        *,
        signal_registry: Mapping[str, SignalModel] | None = None,
        exposure_normalizer: PolicyNormalizer = effective_exposure_policy,
        margin_normalizer: PolicyNormalizer = normalize_margin_policy,
        short_normalizer: PolicyNormalizer = normalize_short_policy,
        quote_provider: object | None = None,
        borrow_provider: object | None = None,
        calendar_provider: object | None = None,
        ledger_store: object | None = None,
    ) -> None:
        self._signal_registry = dict(signal_registry or SIGNAL_MODELS)
        self._exposure_normalizer = exposure_normalizer
        self._margin_normalizer = margin_normalizer
        self._short_normalizer = short_normalizer
        self._quote_provider = quote_provider
        self._borrow_provider = borrow_provider
        self._calendar_provider = calendar_provider
        self._ledger = ledger_store

    def _policies(
        self,
        strategy: Mapping[str, Any],
    ) -> tuple[ExposurePolicy, MarginPolicy, ShortPolicy]:
        validate_strategy_policies(strategy)
        exposure_value = self._exposure_normalizer(strategy["exposure_policy"])
        margin_value = self._margin_normalizer(strategy["margin_policy"])
        short_value = self._short_normalizer(strategy["short_policy"])
        exposure = (
            exposure_value
            if type(exposure_value) is ExposurePolicy
            else ExposurePolicy(**dict(exposure_value))
        )
        margin = (
            margin_value
            if type(margin_value) is MarginPolicy
            else MarginPolicy(**dict(margin_value))
        )
        short = (
            short_value
            if type(short_value) is ShortPolicy
            else ShortPolicy(**dict(short_value))
        )
        return exposure, margin, short

    def prepare_plan_request(
        self,
        *,
        run_key: str,
        strategy: Mapping[str, Any],
        account: AccountSnapshot,
        analyzed_rows: Iterable[Mapping[str, Any]],
        occurred_at: datetime,
    ) -> PlanRequest:
        """Capture all external snapshots once, then seal a strict plan request."""

        if not isinstance(strategy, Mapping):
            raise TypeError("strategy must be a mapping")
        if type(account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        if type(occurred_at) is not datetime or occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be a timezone-aware datetime")
        rows = tuple(analyzed_rows)
        if any(not isinstance(item, Mapping) for item in rows):
            raise TypeError("analyzed_rows items must be mappings")
        symbols = tuple(
            sorted(
                {
                    str(item.get("symbol"))
                    for item in rows
                    if type(item.get("symbol")) is str and item.get("symbol")
                }
            )
        )
        if self._quote_provider is None:
            raise RuntimeError("PortfolioEngine requires a quote_provider")
        if self._borrow_provider is None:
            raise RuntimeError("PortfolioEngine requires a borrow_provider")
        if self._calendar_provider is None:
            raise RuntimeError("PortfolioEngine requires a calendar_provider")
        market = self._quote_provider.snapshot(symbols, occurred_at)
        borrow = self._borrow_provider.snapshot(symbols, occurred_at)
        event_calendar = self._calendar_provider.sessions_until_events(
            symbols,
            occurred_at,
        )
        return PlanRequest(
            run_key=run_key,
            strategy=strategy,
            account=account,
            analyzed_rows=rows,
            market=market,
            borrow=borrow,
            event_calendar=event_calendar,
        )

    def prepare_process_request(
        self,
        *,
        run_key: str,
        strategy: Mapping[str, Any],
        account: AccountSnapshot,
        occurred_at: datetime,
    ) -> ProcessRequest:
        """Capture execution inputs from typed ports without exposing ledger JSON."""

        if not isinstance(strategy, Mapping):
            raise TypeError("strategy must be a mapping")
        if type(account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        if type(occurred_at) is not datetime or occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be a timezone-aware datetime")
        if self._ledger is None:
            raise RuntimeError("PortfolioEngine requires a ledger_store")
        if self._quote_provider is None:
            raise RuntimeError("PortfolioEngine requires a quote_provider")
        if self._borrow_provider is None:
            raise RuntimeError("PortfolioEngine requires a borrow_provider")
        view = self._ledger.load_view(str(strategy["id"]))
        if view.account != account:
            raise ValueError("stale portfolio account snapshot")
        symbols = tuple(
            sorted(
                {
                    *(item.symbol for item in view.account.positions),
                    *(item.symbol for item in view.open_intents),
                }
            )
        )
        market = self._quote_provider.snapshot(symbols, occurred_at)
        borrow = self._borrow_provider.snapshot(symbols, occurred_at)
        return ProcessRequest(
            run_key=run_key,
            strategy=strategy,
            account=account,
            market=market,
            borrow=borrow,
        )

    def evaluate(self, request: PlanRequest) -> DecisionBatch:
        """Evaluate one immutable universe without reading or writing the ledger."""

        if type(request) is not PlanRequest:
            raise TypeError("request must be PlanRequest")
        exposure_policy, margin_policy, short_policy = self._policies(request.strategy)
        signal_config = request.strategy.get("signal")
        if not isinstance(signal_config, Mapping):
            raise ValueError("strategy.signal must be explicit")
        long_model_id = str(signal_config.get("model") or SIGNAL_MODEL_ID)
        long_model = _model_from_registry(self._signal_registry, long_model_id)
        long_candidates = tuple(
            long_model.evaluate(request.analyzed_rows, request.event_calendar)
        )
        if any(type(item) is not SignalCandidate for item in long_candidates):
            raise TypeError("long signal model must return SignalCandidate values")
        if any(item.side.value != "LONG" for item in long_candidates):
            raise ValueError("long signal model returned a non-LONG candidate")

        short_candidates: tuple[SignalCandidate, ...] = ()
        if exposure_policy.mode == "LONG_SHORT":
            short_model = _model_from_registry(
                self._signal_registry,
                short_policy.signal_model,
            )
            short_candidates = tuple(
                short_model.evaluate(request.analyzed_rows, request.event_calendar)
            )
            if any(type(item) is not SignalCandidate for item in short_candidates):
                raise TypeError("short signal model must return SignalCandidate values")
            if any(item.side.value != "SHORT" for item in short_candidates):
                raise ValueError("short signal model returned a non-SHORT candidate")

        long_output = _signal_output("long_signal", long_candidates)
        short_output = _signal_output("short_signal", short_candidates)
        signal_fact = {
            "kind": "signal_candidates",
            "items": tuple(
                sorted(
                    (*long_candidates, *short_candidates),
                    key=lambda item: (item.symbol, item.side.value, item.model_id),
                )
            ),
        }
        net_output = TargetNettingStage().evaluate(
            _stage_input(request, (signal_fact,))
        )
        exposure_output = ExposureBudgetStage(exposure_policy).evaluate(
            _stage_input(request, _plain_fact(net_output))
        )
        borrow_output = BorrowAdmissionStage(
            request.borrow,
            short_policy,
            request.account.positions,
        ).evaluate(_stage_input(request, _plain_fact(exposure_output)))

        borrow_targets = _typed_items(
            borrow_output,
            "borrow_targets",
            TargetPosition,
        )
        borrow_modes = dict(_fact(borrow_output, "position_modes").get("items", {}))
        price_map = {
            symbol: quote.get("price")
            for symbol, quote in request.market.quotes.items()
            if isinstance(quote, Mapping) and quote.get("price") is not None
        }
        valuation = value_account(request.account, {
            held.symbol: price_map[held.symbol]
            for held in request.account.positions
            if held.symbol in price_map
        })
        peak_equity = request.strategy.get("portfolio", {}).get(
            "peak_equity", valuation.metrics.equity
        ) if isinstance(request.strategy.get("portfolio"), Mapping) else valuation.metrics.equity
        risk_output = PortfolioRiskStage(
            request.account,
            request.market,
            peak_equity=peak_equity,
            short_policy=short_policy,
            margin_policy=margin_policy,
            borrow_position_modes=borrow_modes,
        ).evaluate(
            _stage_input(
                request,
                _plain_fact(borrow_output),
                portfolio_snapshot_id=request.market.id,
            )
        )

        rebalance_output_raw = RebalanceIntentStage(
            request.account,
            request.market,
            execution_policy(
                request.market_name,
                {
                    "max_bar_participation_pct": 5.0,
                    **dict(request.strategy.get("portfolio", {})),
                },
            ),
            account_equity=max(0.0, valuation.metrics.equity),
        ).evaluate(
            _stage_input(
                request,
                (
                    {"kind": "borrow_targets", "items": borrow_targets},
                    _fact(risk_output, "risk_intents"),
                ),
            )
        )
        margin_output = MarginAdmissionStage(
            request.account,
            request.market,
            margin_policy,
        ).evaluate(
            _stage_input(
                request,
                ({"kind": "order_intents", "items": _typed_items(rebalance_output_raw, "order_intents", OrderIntent)},),
            )
        )
        admitted_intents = _typed_items(
            margin_output,
            "margin_admitted_intents",
            OrderIntent,
        )
        risk_updates = _typed_items(
            risk_output,
            "position_risk_updates",
            PositionRiskUpdate,
        )

        diagnostics: list[Mapping[str, Any]] = []
        borrow_rejections = _fact(borrow_output, "borrow_diagnostic").get(
            "rejections",
            (),
        )
        for rejection in borrow_rejections:
            if isinstance(rejection, Mapping):
                diagnostics.append(
                    {
                        "code": str(rejection.get("reason") or "BORROW_REJECTED"),
                        "stage": "borrow_admission",
                        "symbol": str(rejection.get("symbol") or ""),
                    }
                )
        margin_diagnostics = _fact(margin_output, "margin_diagnostics").get("items", ())
        for item in margin_diagnostics:
            if isinstance(item, Mapping) and not item.get("admitted", False):
                diagnostics.append(
                    {
                        "code": str(item.get("reason") or "MARGIN_REJECTED"),
                        "stage": "margin_admission",
                        "symbol": str(item.get("symbol") or ""),
                    }
                )
        diagnostics.sort(key=_diagnostic_sort_key)
        outputs = tuple(_ledger_safe_output(item) for item in (
            long_output,
            short_output,
            net_output,
            exposure_output,
            borrow_output,
            risk_output,
            rebalance_output_raw,
            margin_output,
        ))
        return DecisionBatch(
            run_key=request.run_key,
            strategy_id=str(request.strategy["id"]),
            strategy_revision=int(request.strategy["revision"]),
            portfolio_snapshot_id=request.account.snapshot_id or request.account.id,
            market_snapshot_id=request.market.id,
            intents=admitted_intents,
            diagnostics=tuple(diagnostics),
            stage_outputs=outputs,
            position_risk_updates=risk_updates,
        )

    def commit(self, batch: DecisionBatch) -> AccountSnapshot:
        if type(batch) is not DecisionBatch:
            raise TypeError("batch must be DecisionBatch")
        if self._ledger is None:
            raise RuntimeError("PortfolioEngine requires a ledger_store for commit")
        return self._ledger.commit(batch)

    def plan_and_commit(self, request: PlanRequest) -> DecisionBatch:
        batch = self.evaluate(request)
        self.commit(batch)
        return batch

    def process_and_commit(self, request: ProcessRequest) -> DecisionBatch:
        """Advance persisted intents, carry and risk in one atomic commit."""

        if type(request) is not ProcessRequest:
            raise TypeError("request must be ProcessRequest")
        if self._ledger is None:
            raise RuntimeError(
                "PortfolioEngine requires a ledger_store for process_and_commit"
            )
        view = self._ledger.load_view(str(request.strategy["id"]))
        if view.account != request.account:
            raise ValueError("stale portfolio account snapshot")

        exposure_policy, margin_policy, short_policy = self._policies(request.strategy)
        eligible = tuple(
            item
            for item in view.open_intents
            if item.created_snapshot_id != request.market.id
            and item.created_market_at < request.market.occurred_at
        )
        eligible_ids = {item.id for item in eligible}
        progress = tuple(
            item
            for item in view.execution_progress
            if item.intent_id in eligible_ids
        )
        resolved_execution_policy = execution_policy(
            request.market_name,
            {
                "max_bar_participation_pct": 5.0,
                **dict(request.strategy.get("portfolio", {})),
            },
        )
        pre_execution_output = PreExecutionAdmissionStage(
            request.account,
            request.market,
            exposure_policy,
            margin_policy,
            resolved_execution_policy,
        ).evaluate(
            _stage_input(
                request,
                (
                    {"kind": "order_intents", "items": eligible},
                    {"kind": "execution_progress", "items": progress},
                ),
            )
        )
        admitted_for_execution = _typed_items(
            pre_execution_output,
            "pre_execution_admitted_intents",
            OrderIntent,
        )
        execution_output = ExecutionSimulationStage(
            request.account,
            request.market,
            resolved_execution_policy,
        ).evaluate(
            _stage_input(
                request,
                (
                    {
                        "kind": "pre_execution_admitted_intents",
                        "items": admitted_for_execution,
                    },
                    {"kind": "execution_progress", "items": progress},
                ),
            )
        )
        execution_account = _fact(execution_output, "execution_account").get(
            "account"
        )
        if type(execution_account) is not AccountSnapshot:
            raise TypeError("execution stage must return AccountSnapshot")
        if any(item.increases_risk for item in admitted_for_execution):
            post_execution_breaches = hard_cap_breaches(
                execution_account,
                request.market,
                exposure_policy,
                margin_policy,
            )
            if post_execution_breaches:
                raise RuntimeError(
                    "post-execution hard-cap breach: "
                    + ", ".join(post_execution_breaches)
                )

        borrow_apr = {
            symbol: security.borrow_apr_pct
            for symbol, security in request.borrow.securities.items()
        }
        carry = accrue_carry_costs(
            execution_account,
            as_of=market_date(request.market.occurred_at, request.market_name),
            prices=request.market.quotes,
            financing_apr_pct=margin_policy.financing_apr_pct,
            borrow_apr_by_symbol=borrow_apr,
            estimated_borrow_apr_pct=short_policy.estimated_borrow_apr_pct,
        )
        carry_output = StageOutput(
            stage="carry_accrual",
            component_version="1.0.0",
            facts=(
                {
                    "kind": "carry_account",
                    "account": carry.account,
                },
                {
                    "kind": "carry_accruals",
                    "items": carry.new_accruals,
                },
                {
                    "kind": "carry_events",
                    "items": carry.events,
                },
                {
                    "kind": "carry_diagnostics",
                    "items": tuple(
                        {"symbol": item.symbol, "reason": item.reason}
                        for item in carry.diagnostics
                    ),
                },
            ),
        )

        borrow_output = BorrowAdmissionStage(
            request.borrow,
            short_policy,
            carry.account.positions,
        ).evaluate(
            _stage_input(
                request,
                ({"kind": "exposure_targets", "items": ()},),
            )
        )
        borrow_modes = dict(_fact(borrow_output, "position_modes").get("items", {}))
        price_map = {
            symbol: quote.get("price")
            for symbol, quote in request.market.quotes.items()
            if isinstance(quote, Mapping) and quote.get("price") is not None
        }
        valuation = value_account(
            carry.account,
            {
                held.symbol: price_map[held.symbol]
                for held in carry.account.positions
                if held.symbol in price_map
            },
        )
        portfolio_config = request.strategy.get("portfolio")
        peak_equity = (
            portfolio_config.get("peak_equity", valuation.metrics.equity)
            if isinstance(portfolio_config, Mapping)
            else valuation.metrics.equity
        )
        risk_output = PortfolioRiskStage(
            carry.account,
            request.market,
            peak_equity=peak_equity,
            short_policy=short_policy,
            margin_policy=margin_policy,
            borrow_position_modes=borrow_modes,
        ).evaluate(
            _stage_input(
                request,
                _plain_fact(borrow_output),
                portfolio_snapshot_id=request.market.id,
            )
        )

        risk_intents = _typed_items(risk_output, "risk_intents", OrderIntent)
        fills = tuple(_fact(execution_output, "execution_fills").get("items", ()))
        execution_progress = tuple(
            _fact(execution_output, "execution_progress").get("items", ())
        )
        settlement_updates = tuple(
            _fact(execution_output, "position_settlement_updates").get("items", ())
        )
        risk_updates = _typed_items(
            risk_output,
            "position_risk_updates",
            PositionRiskUpdate,
        )
        diagnostics: list[Mapping[str, Any]] = []
        for item in _fact(
            pre_execution_output,
            "pre_execution_diagnostics",
        ).get("items", ()):
            if isinstance(item, Mapping):
                diagnostics.append(
                    {
                        "code": str(item.get("reason") or "PRE_EXECUTION_REJECTED"),
                        "stage": "pre_execution_admission",
                        "symbol": str(item.get("symbol") or ""),
                    }
                )
        for item in _fact(execution_output, "execution_diagnostics").get(
            "items", ()
        ):
            if isinstance(item, Mapping):
                diagnostics.append(
                    {
                        "code": str(item.get("reason") or "EXECUTION_SKIPPED"),
                        "stage": "execution_simulation",
                        "symbol": str(item.get("symbol") or ""),
                    }
                )
        for item in carry.diagnostics:
            diagnostics.append(
                {
                    "code": item.reason,
                    "stage": "carry_accrual",
                    "symbol": item.symbol or "",
                }
            )
        for item in _fact(risk_output, "risk_diagnostic").get("items", ()):
            if isinstance(item, Mapping):
                diagnostics.append(
                    {
                        "code": str(item.get("code") or "RISK_DIAGNOSTIC"),
                        "stage": "portfolio_risk",
                        "symbol": str(item.get("symbol") or ""),
                    }
                )
        diagnostics.sort(key=_diagnostic_sort_key)

        batch = DecisionBatch(
            run_key=request.run_key,
            strategy_id=str(request.strategy["id"]),
            strategy_revision=int(request.strategy["revision"]),
            portfolio_snapshot_id=request.account.snapshot_id or request.account.id,
            market_snapshot_id=request.market.id,
            intents=risk_intents,
            fills=fills,
            events=carry.events,
            diagnostics=tuple(diagnostics),
            stage_outputs=tuple(
                _ledger_safe_output(item)
                for item in (
                    pre_execution_output,
                    execution_output,
                    carry_output,
                    borrow_output,
                    risk_output,
                )
            ),
            execution_progress=execution_progress,
            position_settlement_updates=settlement_updates,
            carry_accruals=carry.new_accruals,
            position_risk_updates=risk_updates,
        )
        self.commit(batch)
        return batch

    def performance(self, strategy_id: str, market: object) -> PortfolioSnapshot:
        """Return a valuation from the typed ledger view without writing state."""

        if type(market) is not MarketSnapshot:
            raise TypeError("market must be MarketSnapshot")
        if type(strategy_id) is not str or not strategy_id:
            raise ValueError("strategy_id must be a non-empty string")
        if self._ledger is None:
            raise RuntimeError("PortfolioEngine requires a ledger_store for performance")
        view = self._ledger.load_view(strategy_id)
        prices = {
            held.symbol: market.quotes[held.symbol]["price"]
            for held in view.account.positions
            if held.symbol in market.quotes
            and isinstance(market.quotes[held.symbol], Mapping)
            and market.quotes[held.symbol].get("price") is not None
        }
        valuation = value_account(view.account, prices)
        valued_account = replace(view.account, positions=valuation.positions)
        return PortfolioSnapshot(
            account=valued_account,
            metrics=valuation.metrics,
            positions=valuation.positions,
            open_intents=view.open_intents,
            recent_events=view.recent_events,
        )
