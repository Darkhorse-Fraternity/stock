"""Application-independent orchestration of portfolio engine workflows."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
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
    PerformanceClosedTrade,
    PerformanceEventView,
    PerformanceHistoryAvailability,
    PerformanceHistoryStatus,
    PerformanceNavPoint,
    PerformanceOrder,
    PerformancePosition,
    PerformanceProjectionRequest,
    PerformanceRuntime,
    PerformanceSummary,
    PlanRequest,
    PortfolioSnapshot,
    PortfolioPerformanceLedgerView,
    PositionEffect,
    PositionRiskUpdate,
    PositionSide,
    ProcessRequest,
    SignalCandidate,
    StrategyPerformanceProjection,
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
from .pre_execution import (
    PreExecutionAdmissionStage,
    hard_cap_breaches,
)
from .risk import PortfolioRiskStage
from .request_identity import request_fingerprint
from .signal_ports import (
    SIGNAL_MODELS,
    SignalRegistryEntry,
    resolve_signal_model,
)
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


def _report_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _performance_runtime(
    view: object,
    *,
    lifecycle_complete: bool | None = None,
    lifecycle_reason: str | None = None,
) -> PerformanceRuntime:
    effective_complete = (
        bool(getattr(view, "lifecycle_complete", True))
        if lifecycle_complete is None
        else lifecycle_complete
    )
    effective_reason = lifecycle_reason or getattr(view, "lifecycle_reason", None)

    def apply_lifecycle(runtime: PerformanceRuntime) -> PerformanceRuntime:
        if effective_complete:
            return runtime
        return replace(
            runtime,
            availability=PerformanceHistoryAvailability(
                complete=False,
                source="v2_ledger",
                reason=(
                    effective_reason
                    or runtime.availability.reason
                    or "canonical portfolio lifecycle is incomplete"
                ),
            ),
        )

    completed_events = [event for event in view.events if event.type == "PIPELINE_COMPLETED"]
    if not completed_events:
        if view.batches:
            return apply_lifecycle(
                PerformanceRuntime(
                    availability=PerformanceHistoryAvailability(
                        complete=False,
                        source="v2_ledger",
                        reason=(
                            "canonical pipeline completion event and run identity "
                            "are unavailable for persisted DecisionBatch history"
                        ),
                    ),
                )
            )
        if not getattr(view, "lifecycle_complete", True):
            return apply_lifecycle(
                PerformanceRuntime(
                    availability=PerformanceHistoryAvailability(
                        complete=False,
                        source="v2_ledger",
                        reason="canonical pipeline runtime history is incomplete",
                    ),
                )
            )
        return apply_lifecycle(PerformanceRuntime())
    event = max(completed_events, key=lambda item: item.occurred_at)
    raw_run_key = event.data.get("run_key")
    run_key = raw_run_key if type(raw_run_key) is str and raw_run_key else None
    if run_key is None:
        return apply_lifecycle(
            PerformanceRuntime(
                last_successful_pipeline_at=event.occurred_at,
                availability=PerformanceHistoryAvailability(
                    complete=False,
                    source="v2_ledger",
                    reason="PIPELINE_COMPLETED event has no canonical run key",
                ),
            )
        )
    batch = next((item for item in view.batches if item.run_key == run_key), None)
    if batch is None:
        return apply_lifecycle(
            PerformanceRuntime(
                last_successful_pipeline_at=event.occurred_at,
                last_successful_pipeline_run_id=run_key,
                availability=PerformanceHistoryAvailability(
                    complete=False,
                    source="v2_ledger",
                    reason="pipeline DecisionBatch is unavailable",
                ),
            )
        )
    stages = tuple(
        {
            "stage": output.stage,
            "component_version": output.component_version,
            "schema_version": output.schema_version,
            "diagnostics": tuple(dict(item) for item in output.diagnostics),
        }
        for output in batch.stage_outputs
    )
    admitted = len(batch.intents)

    def canonical_mapping(field_name: str) -> Mapping[str, Any] | None:
        containers: list[Mapping[str, Any]] = []
        containers.extend(
            item for item in batch.diagnostics if isinstance(item, Mapping)
        )
        for output in batch.stage_outputs:
            containers.extend(
                item
                for item in (*output.facts, *output.diagnostics)
                if isinstance(item, Mapping)
            )
        for container in reversed(containers):
            candidate = container.get(field_name)
            if isinstance(candidate, Mapping):
                return candidate
            if container.get("kind") == field_name:
                value = container.get("value")
                if isinstance(value, Mapping):
                    return value
        return None

    market_regime = canonical_mapping("market_regime")
    data_quality = canonical_mapping("data_quality")
    missing = [
        label
        for label, value in (
            ("market regime", market_regime),
            ("data quality", data_quality),
        )
        if value is None
    ]
    return apply_lifecycle(
        PerformanceRuntime(
            last_successful_pipeline_at=event.occurred_at,
            last_successful_pipeline_run_id=run_key,
            last_pipeline_admitted=admitted,
            last_pipeline_stages=stages,
            last_pipeline_market_regime=market_regime,
            last_pipeline_data_quality=data_quality,
            availability=PerformanceHistoryAvailability(
                complete=not missing,
                source="v2_ledger",
                reason=(
                    "canonical pipeline metadata unavailable: " + ", ".join(missing)
                    if missing
                    else None
                ),
            ),
        )
    )


_PUBLIC_RISK_REASONS = frozenset(
    {
        "MARGIN_CALL",
        "LONG_STOP_LOSS",
        "SHORT_STOP_LOSS",
        "LONG_TRAILING_STOP",
        "SHORT_TRAILING_STOP",
    }
)


def _risk_reason_message(reason: object) -> str:
    return {
        "MARGIN_CALL": "保证金追缴，强制去杠杆（保证金率低于维持线）",
        "LONG_STOP_LOSS": "多头止损",
        "SHORT_STOP_LOSS": "空头止损回补",
        "LONG_TRAILING_STOP": "多头追踪止盈",
        "SHORT_TRAILING_STOP": "空头追踪止盈回补",
    }.get(str(reason or ""), "")


def _event_message(
    event: object,
    *,
    data: Mapping[str, Any] | None = None,
) -> str:
    payload = event.data if data is None else data
    symbol = str(payload.get("symbol") or "")
    messages = {
        "ACCOUNT_OPENED": "模拟账户已创建",
        "PIPELINE_COMPLETED": "Portfolio Pipeline 已完成",
        "ORDER_FILLED": f"{symbol} 订单已成交".strip(),
        "ORDER_PARTIAL": f"{symbol} 订单部分成交".strip(),
        "ORDER_CANCELLED": f"{symbol} 订单已取消".strip(),
        "ORDER_EXPIRED": f"{symbol} 订单已过期".strip(),
        "RISK_CHANGED": f"{symbol} 风险状态已更新".strip(),
        "REVISION_TRANSITIONED": "策略版本已切换",
        "FINANCING_COST_ACCRUED": "融资成本已计提",
        "BORROW_COST_ACCRUED": f"{symbol} 借券成本已计提".strip(),
    }
    message = messages.get(event.type, event.type)
    if event.type != "RISK_CHANGED":
        return message
    reason = _risk_reason_message(payload.get("reason"))
    if reason:
        return f"{symbol} {reason}".strip()
    if payload.get("position_mode") == "COVER_ONLY":
        return f"{symbol} 进入仅允许空头回补状态".strip()
    return message


def _performance_event_data(
    event: object,
    batches_by_run_key: Mapping[str, DecisionBatch],
) -> Mapping[str, Any]:
    if event.type != "RISK_CHANGED":
        return event.data
    run_key = event.data.get("run_key")
    batch = batches_by_run_key.get(run_key) if type(run_key) is str else None
    if batch is None:
        return event.data
    symbol = str(event.data.get("symbol") or "")
    side = str(event.data.get("side") or "")
    reasons = {
        intent.reason
        for intent in batch.intents
        if intent.symbol == symbol
        and intent.position_side.value == side
        and intent.reason in _PUBLIC_RISK_REASONS
    }
    if len(reasons) != 1:
        return event.data
    return MappingProxyType({**event.data, "reason": next(iter(reasons))})


def _exit_distance_pct(position: object, config: Mapping[str, Any]) -> float | None:
    price = position.current_price
    if price is None or price <= 0:
        return None
    stop_loss = _report_number(config.get("stop_loss_pct"))
    trailing = _report_number(
        config.get("trailing_drawdown_pct", config.get("trailing_rebound_pct"))
    )
    if position.side is PositionSide.LONG:
        thresholds = []
        if stop_loss is not None:
            thresholds.append(position.average_cost * (1.0 - stop_loss / 100.0))
        if position.trailing_active and trailing is not None and position.peak_price is not None:
            thresholds.append(position.peak_price * (1.0 - trailing / 100.0))
        return ((price - max(thresholds)) / price * 100.0) if thresholds else None
    thresholds = []
    if stop_loss is not None:
        thresholds.append(position.average_cost * (1.0 + stop_loss / 100.0))
    if position.trailing_active and trailing is not None and position.trough_price is not None:
        thresholds.append(position.trough_price * (1.0 + trailing / 100.0))
    return ((min(thresholds) - price) / price * 100.0) if thresholds else None


def _public_ratio(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _performance_margin_terms(
    source: object,
    metrics: object,
) -> tuple[float, float]:
    """Return policy-consistent margin usage ratio and available buying power."""

    exposure = effective_exposure_policy(source.exposure_policy)
    margin = MarginPolicy(**normalize_margin_policy(source.margin_policy))
    threshold_pct = margin.maintenance_margin_pct + margin.liquidation_buffer_pct
    gross = metrics.long_market_value + metrics.short_liability
    margin_capacity = (
        max(0.0, metrics.equity * 100.0 / threshold_pct - gross)
        if metrics.equity > 0 and threshold_pct > 0
        else 0.0
    )
    gross_capacity = max(
        0.0,
        metrics.equity * exposure.max_gross_exposure_pct / 100.0 - gross,
    )
    return threshold_pct, min(margin_capacity, gross_capacity)


@dataclass(frozen=True)
class _CurrentLotLifecycle:
    symbol: str
    position_side: PositionSide
    quantity: int
    average_cost: float
    first_entry_price: float
    first_entry_at: datetime


@dataclass(frozen=True)
class _PortfolioLifecycleReplay:
    closed_trades: tuple[PerformanceClosedTrade, ...]
    current_lots: Mapping[tuple[str, PositionSide], _CurrentLotLifecycle]
    complete: bool
    reason: str | None


def _unavailable_lifecycle(reason: str | None) -> _PortfolioLifecycleReplay:
    return _PortfolioLifecycleReplay((), MappingProxyType({}), False, reason)


def _replay_portfolio_lifecycle(
    intents: tuple[OrderIntent, ...],
    progress_by_intent: Mapping[str, object],
    revision_by_intent: Mapping[str, int],
    source: object,
    default_revision: int,
    lifecycle_complete: bool,
    lifecycle_reason: str | None,
    *,
    expected_positions: tuple[object, ...] | None = None,
) -> _PortfolioLifecycleReplay:
    """Replay canonical fills with the same weighted-average position accounting."""

    if not lifecycle_complete:
        return _unavailable_lifecycle(lifecycle_reason)
    states: dict[tuple[str, PositionSide], dict[str, object]] = {}
    trades: list[PerformanceClosedTrade] = []
    intent_by_id = {intent.id: intent for intent in intents}
    timeline: list[tuple[object, OrderIntent, object]] = []
    for progress in progress_by_intent.values():
        intent = intent_by_id.get(progress.intent_id)
        if intent is None:
            return _unavailable_lifecycle("execution progress has no canonical intent")
        for fill in progress.fills:
            timeline.append((fill, intent, progress))
    timeline.sort(
        key=lambda item: (
            item[0].occurred_at,
            item[0].snapshot_id,
            item[1].id,
            item[0].id,
        )
    )

    for fill, intent, progress in timeline:
        key = (intent.symbol, intent.position_side)
        state = states.get(key)
        if intent.position_effect in {PositionEffect.OPEN, PositionEffect.INCREASE}:
            if state is None:
                if intent.position_effect is PositionEffect.INCREASE:
                    return _unavailable_lifecycle(
                        "an increase has no reconstructable open position"
                    )
                state = {
                    "quantity": 0,
                    "average_cost": 0.0,
                    "entry_quantity": 0,
                    "entry_notional": 0.0,
                    "entry_fees": 0.0,
                    "exit_quantity": 0,
                    "exit_notional": 0.0,
                    "exit_fees": 0.0,
                    "raw_realized": 0.0,
                    "open_intent_id": intent.id,
                    "first_entry_price": fill.price,
                    "first_entry_at": fill.occurred_at,
                }
                states[key] = state
            elif (
                intent.position_effect is PositionEffect.OPEN
                and state.get("open_intent_id") != intent.id
            ):
                return _unavailable_lifecycle(
                    "an open fill overlaps a reconstructed position"
                )
            quantity = int(state["quantity"])
            average_cost = float(state["average_cost"])
            next_quantity = quantity + fill.quantity
            state["average_cost"] = (
                average_cost * quantity + fill.price * fill.quantity
            ) / next_quantity
            state["quantity"] = next_quantity
            state["entry_quantity"] = int(state["entry_quantity"]) + fill.quantity
            state["entry_notional"] = (
                float(state["entry_notional"]) + fill.price * fill.quantity
            )
            state["entry_fees"] = float(state["entry_fees"]) + fill.fees
            continue
        if state is None:
            return _unavailable_lifecycle("an exit has no reconstructable open position")
        direction = 1.0 if intent.position_side is PositionSide.LONG else -1.0
        quantity = int(state["quantity"])
        if fill.quantity > quantity:
            return _unavailable_lifecycle("exit fills exceed the reconstructed position")
        average_cost = float(state["average_cost"])
        state["raw_realized"] = float(state["raw_realized"]) + (
            direction * (fill.price - average_cost) * fill.quantity
        )
        state["quantity"] = quantity - fill.quantity
        state["exit_quantity"] = int(state["exit_quantity"]) + fill.quantity
        state["exit_notional"] = (
            float(state["exit_notional"]) + fill.price * fill.quantity
        )
        state["exit_fees"] = float(state["exit_fees"]) + fill.fees
        if int(state["quantity"]) == 0:
            if intent.position_effect is not PositionEffect.CLOSE:
                return _unavailable_lifecycle(
                    "a reduce fill flattened the reconstructed position"
                )
            entry_notional = float(state["entry_notional"])
            entry_quantity = int(state["entry_quantity"])
            exit_notional = float(state["exit_notional"])
            exit_quantity = int(state["exit_quantity"])
            if entry_quantity <= 0 or exit_quantity != entry_quantity:
                return _unavailable_lifecycle(
                    "closed lifecycle quantities do not reconcile"
                )
            realized = (
                float(state["raw_realized"])
                - float(state["entry_fees"])
                - float(state["exit_fees"])
            )
            trades.append(
                PerformanceClosedTrade(
                    id=intent.id,
                    name=source.symbol_names.get(intent.symbol, intent.symbol),
                    symbol=intent.symbol,
                    entry_price=entry_notional / entry_quantity,
                    exit_price=exit_notional / exit_quantity,
                    quantity=exit_quantity,
                    realized_pnl=realized,
                    return_pct=realized / entry_notional * 100.0,
                    reason=intent.reason,
                    closed_at=fill.occurred_at,
                    strategy_revision=revision_by_intent.get(
                        intent.id,
                        default_revision,
                    ),
                    position_side=intent.position_side.value,
                )
            )
            del states[key]
        elif (
            intent.position_effect is PositionEffect.CLOSE
            and fill is progress.fills[-1]
            and progress.status == "FILLED"
        ):
            return _unavailable_lifecycle(
                "a completed close did not flatten the reconstructed position"
            )

    if expected_positions is not None:
        expected = {
            (position.symbol, position.side): position
            for position in expected_positions
        }
        if set(states) != set(expected):
            return _unavailable_lifecycle(
                "current positions are not explained by canonical fills"
            )
        for key, position in expected.items():
            state = states[key]
            if int(state["quantity"]) != position.quantity:
                return _unavailable_lifecycle(
                    "current position quantity differs from canonical fills"
                )
            if not math.isclose(
                float(state["average_cost"]),
                position.average_cost,
                rel_tol=1e-12,
                abs_tol=1e-9,
            ):
                return _unavailable_lifecycle(
                    "current position average cost differs from canonical fills"
                )
    trades.sort(key=lambda item: item.closed_at, reverse=True)
    current_lots = {
        key: _CurrentLotLifecycle(
            symbol=key[0],
            position_side=key[1],
            quantity=int(state["quantity"]),
            average_cost=float(state["average_cost"]),
            first_entry_price=float(state["first_entry_price"]),
            first_entry_at=state["first_entry_at"],
        )
        for key, state in states.items()
    }
    return _PortfolioLifecycleReplay(
        tuple(trades),
        MappingProxyType(current_lots),
        True,
        None,
    )


def _replay_closed_trades(
    intents: tuple[OrderIntent, ...],
    progress_by_intent: Mapping[str, object],
    revision_by_intent: Mapping[str, int],
    source: object,
    default_revision: int,
    lifecycle_complete: bool,
    lifecycle_reason: str | None,
    *,
    expected_positions: tuple[object, ...] | None = None,
) -> tuple[tuple[PerformanceClosedTrade, ...], bool, str | None]:
    replay = _replay_portfolio_lifecycle(
        intents,
        progress_by_intent,
        revision_by_intent,
        source,
        default_revision,
        lifecycle_complete,
        lifecycle_reason,
        expected_positions=expected_positions,
    )
    return replay.closed_trades, replay.complete, replay.reason


class PortfolioEngine:
    """Thin service coordinating pure decisions and one ledger transaction."""

    def __init__(
        self,
        *,
        signal_registry: Mapping[str, SignalRegistryEntry] | None = None,
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
        cost_multiplier: float = 1.0,
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
            cost_multiplier=cost_multiplier,
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
        long_model = resolve_signal_model(self._signal_registry, long_model_id)
        long_candidates = tuple(
            long_model.evaluate(request.analyzed_rows, request.event_calendar)
        )
        if any(type(item) is not SignalCandidate for item in long_candidates):
            raise TypeError("long signal model must return SignalCandidate values")
        if any(item.side.value != "LONG" for item in long_candidates):
            raise ValueError("long signal model returned a non-LONG candidate")

        short_candidates: tuple[SignalCandidate, ...] = ()
        if exposure_policy.mode == "LONG_SHORT":
            short_model = resolve_signal_model(
                self._signal_registry,
                short_policy.signal_model,
                policy=short_policy,
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
        if type(request) is not PlanRequest:
            raise TypeError("request must be PlanRequest")
        if self._ledger is None:
            raise RuntimeError(
                "PortfolioEngine requires a ledger_store for plan_and_commit"
            )
        fingerprint = request_fingerprint(request)
        committed = self._ledger.load_committed_batch(
            str(request.strategy["id"]),
            request.run_key,
            fingerprint,
        )
        if committed is not None:
            return committed
        batch = replace(
            self.evaluate(request),
            request_fingerprint=fingerprint,
        )
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
        fingerprint = request_fingerprint(request)
        committed = self._ledger.load_committed_batch(
            str(request.strategy["id"]),
            request.run_key,
            fingerprint,
        )
        if committed is not None:
            return committed
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
            borrow_snapshot=request.borrow,
            short_policy=short_policy,
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
        admitted_intent_ids = {item.id for item in admitted_for_execution}
        admitted_progress = tuple(
            item for item in progress if item.intent_id in admitted_intent_ids
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
                    {"kind": "execution_progress", "items": admitted_progress},
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
                baseline_account=request.account,
            )
            if post_execution_breaches:
                raise RuntimeError(
                    "post-execution hard-cap breach: "
                    + ", ".join(item.code for item in post_execution_breaches)
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
            cost_multiplier=request.cost_multiplier,
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
            request_fingerprint=fingerprint,
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

    def performance_projection(
        self,
        request: PerformanceProjectionRequest,
        *,
        ledger_view: PortfolioPerformanceLedgerView | None = None,
    ) -> StrategyPerformanceProjection:
        """Project the complete report contract from typed ledger and market facts."""

        if type(request) is not PerformanceProjectionRequest:
            raise TypeError("request must be PerformanceProjectionRequest")
        source = request.strategy
        if ledger_view is not None and type(ledger_view) is not PortfolioPerformanceLedgerView:
            raise TypeError("ledger_view must be PortfolioPerformanceLedgerView")
        if ledger_view is None:
            if self._ledger is None:
                raise RuntimeError("PortfolioEngine requires a ledger_store for performance")
            view = self._ledger.load_performance_view(source.id)
        else:
            view = ledger_view
        if view.account.strategy_id != source.id:
            raise ValueError("ledger_view strategy_id differs from performance strategy")
        if view.account.strategy_revision != source.revision:
            raise ValueError("ledger_view strategy_revision differs from performance strategy")
        prices = {
            held.symbol: request.market.quotes[held.symbol]["price"]
            for held in view.account.positions
            if held.symbol in request.market.quotes
            and isinstance(request.market.quotes[held.symbol], Mapping)
            and request.market.quotes[held.symbol].get("price") is not None
        }
        valuation = value_account(view.account, prices)
        valued_account = replace(view.account, positions=valuation.positions)
        snapshot = PortfolioSnapshot(
            account=valued_account,
            metrics=valuation.metrics,
            positions=valuation.positions,
            open_intents=(),
            recent_events=view.events,
        )
        progress_by_intent = {
            item.intent_id: item for item in view.execution_progress
        }
        revision_by_intent = {
            intent.id: batch.strategy_revision
            for batch in view.batches
            for intent in batch.intents
        }
        lifecycle = _replay_portfolio_lifecycle(
            view.intents,
            progress_by_intent,
            revision_by_intent,
            source,
            snapshot.account.strategy_revision,
            view.lifecycle_complete,
            view.lifecycle_reason,
            expected_positions=view.account.positions,
        )
        closed_trades = lifecycle.closed_trades
        lifecycle_complete = lifecycle.complete
        lifecycle_reason = lifecycle.reason

        margin_threshold_pct, buying_power = _performance_margin_terms(
            source,
            snapshot.metrics,
        )
        short_policy = ShortPolicy(**normalize_short_policy(source.short_policy))
        positions = []
        for slot_id, held in enumerate(snapshot.positions, start=1):
            market_value = held.market_value
            unrealized = held.unrealized_pnl
            if held.current_price is None or market_value is None or unrealized is None:
                raise RuntimeError(f"position valuation is incomplete: {held.symbol}")
            cost_notional = held.average_cost * held.quantity
            direction = 1.0 if held.side is PositionSide.LONG else -1.0
            quote = request.market.quotes.get(held.symbol, {})
            day_change = (
                _report_number(quote.get("percent"))
                if isinstance(quote, Mapping)
                else None
            )
            current_lot = lifecycle.current_lots.get((held.symbol, held.side))
            positions.append(
                PerformancePosition(
                    slot_id=slot_id,
                    name=source.symbol_names.get(held.symbol, held.symbol),
                    symbol=held.symbol,
                    first_entry_price=(
                        current_lot.first_entry_price if current_lot is not None else None
                    ),
                    first_entry_at=(
                        current_lot.first_entry_at if current_lot is not None else None
                    ),
                    current_price=held.current_price,
                    day_change_pct=day_change,
                    return_pct=(unrealized / cost_notional * 100.0),
                    unrealized_pnl=unrealized,
                    weight_pct=(
                        direction * market_value / snapshot.metrics.equity * 100.0
                        if snapshot.metrics.equity != 0
                        else 0.0
                    ),
                    quantity=held.quantity,
                    sellable_quantity=held.sellable_quantity,
                    trailing_active=held.trailing_active,
                    signal_invalid_days=None,
                    exit_distance_pct=_exit_distance_pct(
                        held,
                        source.short_policy
                        if held.side is PositionSide.SHORT
                        else source.config,
                    ),
                    market_value=market_value,
                    average_cost=held.average_cost,
                    position_side=held.side.value,
                    side=held.side.value,
                    position_mode=held.position_mode,
                    borrow_rate_pct=(
                        short_policy.estimated_borrow_apr_pct
                        if held.side is PositionSide.SHORT
                        else None
                    ),
                    borrow_rate_source=(
                        "strategy_estimate"
                        if held.side is PositionSide.SHORT
                        else "unavailable"
                    ),
                    borrow_rate_estimated=held.side is PositionSide.SHORT,
                    margin_used=market_value * margin_threshold_pct / 100.0,
                )
            )

        cancellation_by_intent: dict[str, tuple[object, str, str | None]] = {}
        for event in view.events:
            terminal_status = None
            intent_ids: tuple[str, ...] = ()
            cancellation_reason = None
            if event.type in {"ORDER_CANCELLED", "ORDER_EXPIRED"} and event.data.get(
                "intent_id"
            ):
                terminal_status = event.type.removeprefix("ORDER_")
                intent_ids = (str(event.data["intent_id"]),)
                if event.data.get("reason"):
                    cancellation_reason = str(event.data["reason"])
            elif event.type == "REVISION_TRANSITIONED":
                raw_ids = event.data.get("cancelled_intent_ids", ())
                if isinstance(raw_ids, tuple):
                    intent_ids = tuple(str(intent_id) for intent_id in raw_ids)
                terminal_status = "CANCELLED"
                cancellation_reason = "STRATEGY_REVISION_TRANSITION"
            if terminal_status is None:
                continue
            for intent_id in intent_ids:
                current = cancellation_by_intent.get(intent_id)
                if current is None or event.occurred_at >= current[0].occurred_at:
                    cancellation_by_intent[intent_id] = (
                        event,
                        terminal_status,
                        cancellation_reason,
                    )
        orders = []
        for intent in view.intents:
            progress = progress_by_intent.get(intent.id)
            cancellation = cancellation_by_intent.get(intent.id)
            cancelled = cancellation[0] if cancellation is not None else None
            status = (
                cancellation[1]
                if cancellation is not None
                else progress.status
                if progress is not None
                else "INTENDED"
            )
            updated_at = (
                cancelled.occurred_at
                if cancelled is not None
                else progress.fills[-1].occurred_at
                if progress is not None
                else intent.created_market_at
            )
            orders.append(
                PerformanceOrder(
                    id=intent.id,
                    side=intent.order_side.value,
                    symbol=intent.symbol,
                    name=source.symbol_names.get(intent.symbol, intent.symbol),
                    quantity=intent.quantity,
                    filled_quantity=progress.filled_quantity if progress is not None else 0,
                    status=status,
                    reason=intent.reason,
                    created_at=intent.created_market_at,
                    updated_at=updated_at,
                    filled_notional=progress.filled_notional if progress is not None else 0.0,
                    commission_charged=(
                        progress.commission_charged if progress is not None else 0.0
                    ),
                    fees_charged=progress.fees_charged if progress is not None else 0.0,
                    strategy_revision=revision_by_intent.get(
                        intent.id,
                    ),
                    position_side=intent.position_side.value,
                    position_effect=intent.position_effect.value,
                    key=None,
                    control_epoch=None,
                    purpose=(
                        "ENTRY"
                        if intent.position_effect
                        in {PositionEffect.OPEN, PositionEffect.INCREASE}
                        else "EXIT"
                    ),
                    slot_id=None,
                    signal_price=None,
                    score=None,
                    reserved_cash=None,
                    valid_date=None,
                    valid_session_date=None,
                    cancel_reason=(
                        cancellation[2]
                        if cancellation is not None
                        else None
                    ),
                    replacement_candidate=None,
                )
            )
        orders.sort(key=lambda item: item.updated_at, reverse=True)

        opened_events = [event for event in view.events if event.type == "ACCOUNT_OPENED"]
        opened = min(opened_events, key=lambda item: item.occurred_at) if opened_events else None
        nav_history = []
        opened_cash = (
            _report_number(opened.data.get("available_cash"))
            if opened is not None
            else None
        )
        opened_cash_missing = opened is not None and opened_cash is None
        if (
            opened is not None
            and lifecycle_complete
            and opened.occurred_at != request.generated_at
            and opened_cash is not None
        ):
            nav_history.append(
                PerformanceNavPoint(
                    at=opened.occurred_at,
                    nav=opened_cash,
                    cash=opened_cash,
                    market_value=0.0,
                    cumulative_return_pct=(
                        opened_cash / source.initial_cash - 1.0
                    )
                    * 100.0,
                    drawdown_pct=0.0,
                    risk_level=None,
                    trading_mode=None,
                    source="account_opened",
                )
            )
        nav_history.append(
            PerformanceNavPoint(
                at=request.generated_at,
                nav=snapshot.metrics.equity,
                cash=snapshot.metrics.available_cash,
                market_value=snapshot.metrics.long_market_value,
                cumulative_return_pct=(
                    snapshot.metrics.equity / source.initial_cash - 1.0
                )
                * 100.0,
                drawdown_pct=None,
                risk_level=source.risk_level,
                # Exposure mode belongs to strategy metadata. No persisted runtime
                # trading state exists for this valuation mark, so keep it unknown.
                trading_mode=None,
                source=request.valuation_source,
            )
        )
        nav_complete = (
            opened is not None
            and not opened_cash_missing
            and not view.batches
            and lifecycle_complete
        )
        nav_reason = None
        if opened is None:
            nav_reason = "ACCOUNT_OPENED fact is unavailable"
        elif opened_cash_missing:
            nav_reason = "ACCOUNT_OPENED has no canonical available cash"
        elif not lifecycle_complete:
            nav_reason = "lifecycle is incomplete; historical NAV cannot be reconstructed"
        elif view.batches:
            nav_reason = "historical NAV marks are not persisted in ledger schema v2"
        maximum_drawdown: float | None = None
        if nav_complete:
            peak = nav_history[0].nav
            maximum_drawdown = 0.0
            complete_nav_history = []
            for point in nav_history:
                peak = max(peak, point.nav)
                drawdown = (peak - point.nav) / peak * 100.0 if peak else 0.0
                complete_nav_history.append(
                    replace(point, drawdown_pct=drawdown)
                )
                maximum_drawdown = max(maximum_drawdown, drawdown)
            nav_history = complete_nav_history
        realized_pnl = (
            sum(item.realized_pnl for item in closed_trades)
            if lifecycle_complete
            else None
        )
        wins = sum(item.realized_pnl > 0 for item in closed_trades)
        revision_by_event = {
            event.id: batch.strategy_revision
            for batch in view.batches
            for event in batch.events
        }
        batches_by_run_key = {batch.run_key: batch for batch in view.batches}
        event_views = []
        for event in reversed(view.events[-200:]):
            event_data = _performance_event_data(event, batches_by_run_key)
            event_views.append(
                PerformanceEventView(
                    id=event.id,
                    key=None,
                    type=event.type,
                    occurred_at=event.occurred_at,
                    message=_event_message(event, data=event_data),
                    strategy_revision=(
                        event_data.get("strategy_revision")
                        if type(event_data.get("strategy_revision")) is int
                        else revision_by_event.get(event.id)
                        or revision_by_intent.get(
                            str(event_data.get("intent_id") or "")
                        )
                    ),
                    data=event_data,
                )
            )
        events = tuple(event_views)
        summary = PerformanceSummary(
            initial_cash=source.initial_cash,
            nav=snapshot.metrics.equity,
            cash=snapshot.metrics.available_cash,
            reserved_cash=snapshot.account.reserved_cash,
            market_value=snapshot.metrics.long_market_value,
            long_market_value=snapshot.metrics.long_market_value,
            short_liability=snapshot.metrics.short_liability,
            gross_exposure_pct=_public_ratio(snapshot.metrics.gross_exposure_pct),
            net_exposure_pct=_public_ratio(snapshot.metrics.net_exposure_pct),
            margin_rate_pct=_public_ratio(snapshot.metrics.margin_rate_pct),
            buying_power=buying_power,
            margin_loan=snapshot.metrics.margin_loan,
            financing_cost=snapshot.metrics.accrued_financing_cost,
            borrow_cost=snapshot.metrics.accrued_borrow_cost,
            cumulative_return_pct=(
                snapshot.metrics.equity / source.initial_cash - 1.0
            )
            * 100.0,
            maximum_drawdown_pct=maximum_drawdown,
            realized_pnl=realized_pnl,
            unrealized_pnl=sum(item.unrealized_pnl for item in positions),
            position_count=len(positions),
            max_positions=source.max_positions,
            target_exposure_pct=source.target_exposure_pct,
            closed_trade_count=(len(closed_trades) if lifecycle_complete else None),
            win_rate_pct=(
                wins / len(closed_trades) * 100.0 if closed_trades else None
            ),
        )
        return StrategyPerformanceProjection(
            generated_at=request.generated_at,
            quote_error=request.quote_error,
            strategy=source,
            summary=summary,
            runtime=_performance_runtime(
                view,
                lifecycle_complete=lifecycle_complete,
                lifecycle_reason=lifecycle_reason,
            ),
            nav_history=tuple(nav_history),
            positions=tuple(positions),
            orders=tuple(orders[:100]),
            closed_trades=tuple(closed_trades[:200]),
            events=events,
            history_availability=PerformanceHistoryStatus(
                nav=PerformanceHistoryAvailability(
                    complete=nav_complete,
                    source="v2_ledger",
                    reason=nav_reason,
                ),
                lifecycle=PerformanceHistoryAvailability(
                    complete=lifecycle_complete,
                    source="v2_ledger",
                    reason=lifecycle_reason,
                ),
            ),
        )
