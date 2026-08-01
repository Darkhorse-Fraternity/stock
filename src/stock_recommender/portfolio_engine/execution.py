"""Pure order-intent generation and paper execution simulation."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, DecimalException, ROUND_DOWN

from ..market_adapters import get_market_adapter
from ..markets import market_date, next_business_date
from ..pipeline import PipelineContractError, StageInput, StageOutput
from .contracts import (
    AccountSnapshot,
    AccrualLifecycle,
    CarryAccrualRecord,
    CarryCostType,
    ExecutionFill,
    ExecutionProgressFill,
    MarketSnapshot,
    OrderIntent,
    OrderExecutionProgress,
    OrderSide,
    PositionEffect,
    PositionSettlementUpdate,
    PositionSide,
    PositionSnapshot,
    PortfolioEvent,
    TargetPosition,
    stable_execution_intent_id,
    stable_execution_progress_fill_id,
    verify_order_intent_id,
)
from .margin import IntentSemanticsError, project_account_for_intent


def _finite_nonnegative(value: object, field_name: str) -> float:
    if type(value) not in (int, float):
        raise TypeError(f"{field_name} must be an int or float")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite and nonnegative") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} must be finite and nonnegative")
    return 0.0 if number == 0.0 else number


@dataclass(frozen=True)
class ExecutionPolicy:
    """Market-specific, immutable paper execution conventions."""

    market: str
    lot_size: int
    same_day_sell: bool
    commission_rate_pct: float
    minimum_commission: float
    stamp_duty_rate_pct: float
    transfer_fee_rate_pct: float
    slippage_bps: float
    max_bar_participation_pct: float

    def __post_init__(self) -> None:
        if type(self.market) is not str or not self.market:
            raise ValueError("market must be a non-empty string")
        if type(self.lot_size) is not int:
            raise TypeError("lot_size must be an integer")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if type(self.same_day_sell) is not bool:
            raise TypeError("same_day_sell must be a bool")
        for field_name in (
            "commission_rate_pct",
            "minimum_commission",
            "stamp_duty_rate_pct",
            "transfer_fee_rate_pct",
            "slippage_bps",
            "max_bar_participation_pct",
        ):
            object.__setattr__(
                self,
                field_name,
                _finite_nonnegative(getattr(self, field_name), field_name),
            )
        if not 0 < self.max_bar_participation_pct <= 100:
            raise ValueError("max_bar_participation_pct must be in (0, 100]")


_EXECUTION_POLICY_FINGERPRINT_VERSION = "execution-policy-v1"


def execution_policy_fingerprint(policy: ExecutionPolicy) -> str:
    """Bind resumable execution progress to every fee and fill convention."""

    if type(policy) is not ExecutionPolicy:
        raise TypeError("policy must be ExecutionPolicy")
    material = "\x1f".join(
        (
            _EXECUTION_POLICY_FINGERPRINT_VERSION,
            policy.market,
            str(policy.lot_size),
            "1" if policy.same_day_sell else "0",
            format(policy.commission_rate_pct, ".17g"),
            format(policy.minimum_commission, ".17g"),
            format(policy.stamp_duty_rate_pct, ".17g"),
            format(policy.transfer_fee_rate_pct, ".17g"),
            format(policy.slippage_bps, ".17g"),
            format(policy.max_bar_participation_pct, ".17g"),
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{_EXECUTION_POLICY_FINGERPRINT_VERSION}:{digest}"


def execution_policy(market: object, config: Mapping[str, object]) -> ExecutionPolicy:
    """Resolve fees and lot/session rules through the existing market adapter."""

    if not isinstance(config, Mapping):
        raise TypeError("config must be a mapping")
    adapter = get_market_adapter(market)
    effective = adapter.execution_config(dict(config))
    return ExecutionPolicy(
        market=adapter.market,
        lot_size=adapter.profile.lot_size,
        same_day_sell=adapter.profile.same_day_sell,
        commission_rate_pct=effective.get("commission_rate_pct", 0.0),
        minimum_commission=effective.get("minimum_commission_cny", 0.0),
        stamp_duty_rate_pct=effective.get("stamp_duty_rate_pct", 0.0),
        transfer_fee_rate_pct=effective.get("transfer_fee_rate_pct", 0.0),
        slippage_bps=effective.get("slippage_bps", 0.0),
        max_bar_participation_pct=effective.get(
            "max_bar_participation_pct", 0.0
        ),
    )


@dataclass(frozen=True)
class PlanningDiagnostic:
    symbol: str
    reason: str

    def __post_init__(self) -> None:
        if type(self.symbol) is not str or not self.symbol:
            raise ValueError("symbol must be a non-empty string")
        if type(self.reason) is not str or not self.reason:
            raise ValueError("reason must be a non-empty string")


@dataclass(frozen=True)
class IntentPlanningResult:
    intents: tuple[OrderIntent, ...] = ()
    diagnostics: tuple[PlanningDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        intents = tuple(self.intents)
        diagnostics = tuple(self.diagnostics)
        if any(type(item) is not OrderIntent for item in intents):
            raise TypeError("intents items must be OrderIntent")
        if any(type(item) is not PlanningDiagnostic for item in diagnostics):
            raise TypeError("diagnostics items must be PlanningDiagnostic")
        symbols = [item.symbol for item in intents]
        if len(symbols) != len(set(symbols)):
            raise ValueError("intents must not contain duplicate symbols")
        object.__setattr__(self, "intents", intents)
        object.__setattr__(self, "diagnostics", diagnostics)


@dataclass(frozen=True)
class ExecutionDiagnostic:
    intent_id: str
    symbol: str
    reason: str

    def __post_init__(self) -> None:
        for field_name in ("intent_id", "symbol", "reason"):
            value = getattr(self, field_name)
            if type(value) is not str or not value:
                raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True)
class ExecutionSimulationResult:
    account: AccountSnapshot
    fills: tuple[ExecutionFill, ...] = ()
    diagnostics: tuple[ExecutionDiagnostic, ...] = ()
    progress: tuple[OrderExecutionProgress, ...] = ()
    settlement_updates: tuple[PositionSettlementUpdate, ...] = ()

    def __post_init__(self) -> None:
        if type(self.account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        fills = tuple(self.fills)
        diagnostics = tuple(self.diagnostics)
        progress = tuple(self.progress)
        settlement_updates = tuple(self.settlement_updates)
        if any(type(item) is not ExecutionFill for item in fills):
            raise TypeError("fills items must be ExecutionFill")
        if any(type(item) is not ExecutionDiagnostic for item in diagnostics):
            raise TypeError("diagnostics items must be ExecutionDiagnostic")
        if any(type(item) is not OrderExecutionProgress for item in progress):
            raise TypeError("progress items must be OrderExecutionProgress")
        if any(
            type(item) is not PositionSettlementUpdate
            for item in settlement_updates
        ):
            raise TypeError(
                "settlement_updates items must be PositionSettlementUpdate"
            )
        ids = [item.intent_id for item in fills]
        if len(ids) != len(set(ids)):
            raise ValueError("fills must not contain duplicate intent IDs")
        progress_ids = [item.intent_id for item in progress]
        if len(progress_ids) != len(set(progress_ids)):
            raise ValueError("progress must not contain duplicate intent IDs")
        settlement_symbols = [item.symbol for item in settlement_updates]
        if len(settlement_symbols) != len(set(settlement_symbols)):
            raise ValueError("settlement_updates must not contain duplicate symbols")
        if any(item.intent_id not in set(progress_ids) for item in fills):
            raise ValueError("every fill must have execution progress")
        object.__setattr__(self, "fills", fills)
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "progress", progress)
        object.__setattr__(self, "settlement_updates", settlement_updates)


@dataclass(frozen=True)
class CarryAccrualDiagnostic:
    symbol: str | None
    reason: str

    def __post_init__(self) -> None:
        if self.symbol is not None and (
            type(self.symbol) is not str or not self.symbol
        ):
            raise ValueError("symbol must be a non-empty string or None")
        if type(self.reason) is not str or not self.reason:
            raise ValueError("reason must be a non-empty string")


@dataclass(frozen=True)
class CarryAccrualResult:
    account: AccountSnapshot
    financing_cost: float
    borrow_cost: float
    new_accruals: tuple[CarryAccrualRecord, ...] = ()
    events: tuple[PortfolioEvent, ...] = ()
    diagnostics: tuple[CarryAccrualDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if type(self.account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        financing = _finite_nonnegative(self.financing_cost, "financing_cost")
        borrow = _finite_nonnegative(self.borrow_cost, "borrow_cost")
        records = tuple(self.new_accruals)
        events = tuple(self.events)
        diagnostics = tuple(self.diagnostics)
        if any(type(item) is not CarryAccrualRecord for item in records):
            raise TypeError("new_accruals items must be CarryAccrualRecord")
        if any(type(item) is not PortfolioEvent for item in events):
            raise TypeError("events items must be PortfolioEvent")
        if any(type(item) is not CarryAccrualDiagnostic for item in diagnostics):
            raise TypeError("diagnostics items must be CarryAccrualDiagnostic")
        expected = {
            CarryCostType.FINANCING: financing,
            CarryCostType.BORROW: borrow,
        }
        for cost_type, amount in expected.items():
            recorded = sum(
                (item.amount for item in records if item.cost_type is cost_type),
                0.0,
            )
            if not math.isclose(recorded, amount, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("new accrual amounts must match result costs")
        account_keys = {item.idempotency_key for item in self.account.carry_accruals}
        if any(item.idempotency_key not in account_keys for item in records):
            raise ValueError("new accruals must be present on the result account")
        object.__setattr__(self, "financing_cost", financing)
        object.__setattr__(self, "borrow_cost", borrow)
        object.__setattr__(self, "new_accruals", records)
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "diagnostics", diagnostics)


def _intent_id(
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
    return stable_execution_intent_id(
        symbol=symbol,
        position_side=position_side,
        order_side=order_side,
        position_effect=position_effect,
        quantity=quantity,
        reason=reason,
        created_snapshot_id=created_snapshot_id,
        created_market_at=created_market_at,
    )


def verify_intent_id(
    intent: OrderIntent,
    existing_position: PositionSnapshot | None = None,
) -> bool:
    """Return whether an execution intent ID is bound to all intent semantics."""

    return verify_order_intent_id(intent, existing_position)


def intent_for_delta(
    existing: PositionSnapshot | None,
    target: TargetPosition | None,
    *,
    target_quantity: int,
    created_snapshot_id: str,
    created_market_at: datetime,
    reason: str = "REBALANCE",
) -> OrderIntent | None:
    """Plan one unambiguous delta; reversals close before a later snapshot opens."""

    if existing is not None and type(existing) is not PositionSnapshot:
        raise TypeError("existing must be PositionSnapshot or None")
    if target is not None and type(target) is not TargetPosition:
        raise TypeError("target must be TargetPosition or None")
    if type(target_quantity) is not int:
        raise TypeError("target_quantity must be an integer")
    if target_quantity < 0:
        raise ValueError("target_quantity must be nonnegative")
    if type(created_snapshot_id) is not str or not created_snapshot_id:
        raise ValueError("created_snapshot_id must be a non-empty string")
    if (
        type(created_market_at) is not datetime
        or created_market_at.tzinfo is None
        or created_market_at.utcoffset() is None
    ):
        raise ValueError("created_market_at must be a timezone-aware datetime")
    if type(reason) is not str or not reason:
        raise ValueError("reason must be a non-empty string")
    if existing is None and target is None:
        return None
    if existing is not None and target is not None and existing.symbol != target.symbol:
        raise ValueError("existing and target symbols must match")

    if existing is not None and (
        target is None
        or existing.side is not target.side
        or target_quantity == 0
    ):
        side = existing.side
        effect = PositionEffect.CLOSE
        quantity = existing.quantity
    elif existing is None:
        if target is None or target_quantity == 0:
            return None
        side = target.side
        effect = PositionEffect.OPEN
        quantity = target_quantity
    else:
        if target is None:
            raise RuntimeError("unreachable target state")
        delta = target_quantity - existing.quantity
        if delta == 0:
            return None
        side = existing.side
        effect = PositionEffect.INCREASE if delta > 0 else PositionEffect.REDUCE
        quantity = abs(delta)

    symbol = existing.symbol if existing is not None else target.symbol
    order_side = (
        OrderSide.BUY
        if (side is PositionSide.LONG and effect in {PositionEffect.OPEN, PositionEffect.INCREASE})
        or (side is PositionSide.SHORT and effect in {PositionEffect.REDUCE, PositionEffect.CLOSE})
        else OrderSide.SELL
    )
    intent_id = _intent_id(
        symbol=symbol,
        position_side=side,
        order_side=order_side,
        position_effect=effect,
        quantity=quantity,
        reason=reason,
        created_snapshot_id=created_snapshot_id,
        created_market_at=created_market_at,
    )
    return OrderIntent(
        id=intent_id,
        symbol=symbol,
        position_side=side,
        order_side=order_side,
        position_effect=effect,
        quantity=quantity,
        reason=reason,
        created_snapshot_id=created_snapshot_id,
        created_market_at=created_market_at,
    )


def _typed_unique(
    values: Iterable[object],
    item_type: type,
    field_name: str,
) -> tuple:
    if isinstance(values, (str, bytes, bytearray, Mapping)):
        raise TypeError(f"{field_name} must be an iterable")
    try:
        items = tuple(values)
    except TypeError as exc:
        raise TypeError(f"{field_name} must be an iterable") from exc
    if any(type(item) is not item_type for item in items):
        raise TypeError(f"{field_name} items must be {item_type.__name__}")
    symbols = [item.symbol for item in items]
    if len(symbols) != len(set(symbols)):
        raise ValueError(f"{field_name} must not contain duplicate symbols")
    return items


def _target_quantity(
    target: TargetPosition,
    quote: object,
    policy: ExecutionPolicy,
    account_equity: float,
) -> tuple[int | None, str | None]:
    if not isinstance(quote, Mapping):
        return None, "MARKET_PRICE_MISSING"
    price = _quote_number(quote, "price", positive=True)
    if price is None:
        price = _quote_number(quote, "bar_open", positive=True)
    if price is None:
        return None, "MARKET_PRICE_MISSING"
    exact = (
        Decimal(str(account_equity))
        * Decimal(str(target.target_weight_pct))
        / Decimal(100)
        / Decimal(str(price))
    )
    raw_quantity = int(exact.to_integral_value(rounding=ROUND_DOWN))
    quantity = raw_quantity // policy.lot_size * policy.lot_size
    if quantity <= 0:
        return 0, "LOT_TOO_SMALL"
    return quantity, None


def _validate_risk_intent(
    intent: OrderIntent,
    existing_by_symbol: Mapping[str, PositionSnapshot],
) -> None:
    if intent.increases_risk or intent.position_effect not in {
        PositionEffect.REDUCE,
        PositionEffect.CLOSE,
    }:
        raise ValueError("risk intent must reduce or close a position")
    if not _direction_semantics_are_valid(intent):
        raise ValueError("risk intent has invalid direction semantics")
    existing = existing_by_symbol.get(intent.symbol)
    if existing is None or existing.side is not intent.position_side:
        raise ValueError("risk intent must match an existing position")
    if not verify_intent_id(intent, existing):
        raise ValueError("risk intent ID does not match its semantics")
    if intent.position_effect is PositionEffect.CLOSE:
        if intent.quantity != existing.quantity:
            raise ValueError("risk CLOSE quantity must equal the existing position")
    elif intent.quantity >= existing.quantity:
        raise ValueError("risk REDUCE quantity must be below the existing position")


def plan_rebalance_intents(
    account: AccountSnapshot,
    targets: Iterable[TargetPosition],
    market: MarketSnapshot,
    policy: ExecutionPolicy,
    *,
    account_equity: object,
    risk_intents: Iterable[OrderIntent] = (),
) -> IntentPlanningResult:
    """Build a deterministic, pure intent batch from targets and risk exits."""

    if type(account) is not AccountSnapshot:
        raise TypeError("account must be AccountSnapshot")
    if type(market) is not MarketSnapshot:
        raise TypeError("market must be MarketSnapshot")
    if type(policy) is not ExecutionPolicy:
        raise TypeError("policy must be ExecutionPolicy")
    equity = _finite_nonnegative(account_equity, "account_equity")
    resolved_targets = _typed_unique(targets, TargetPosition, "targets")
    resolved_risk = _typed_unique(risk_intents, OrderIntent, "risk_intents")
    existing = {item.symbol: item for item in account.positions}
    for item in resolved_risk:
        _validate_risk_intent(item, existing)
    risk_symbols = {item.symbol for item in resolved_risk}
    target_by_symbol = {item.symbol: item for item in resolved_targets}
    planned: list[OrderIntent] = list(resolved_risk)
    diagnostics: list[PlanningDiagnostic] = []

    symbols = sorted((set(existing) | set(target_by_symbol)) - risk_symbols)
    for symbol in symbols:
        held = existing.get(symbol)
        requested = target_by_symbol.get(symbol)
        target_quantity = 0
        if (
            held is not None
            and requested is not None
            and held.side is not requested.side
        ):
            intent = intent_for_delta(
                held,
                requested,
                target_quantity=0,
                created_snapshot_id=market.id,
                created_market_at=market.occurred_at,
            )
            if intent is not None:
                planned.append(intent)
            continue
        if requested is not None:
            calculated, reason = _target_quantity(
                requested,
                market.quotes.get(symbol),
                policy,
                equity,
            )
            if reason is not None:
                if reason == "LOT_TOO_SMALL" and held is not None:
                    intent = intent_for_delta(
                        held,
                        requested,
                        target_quantity=0,
                        created_snapshot_id=market.id,
                        created_market_at=market.occurred_at,
                    )
                    if intent is not None:
                        planned.append(intent)
                    continue
                diagnostics.append(PlanningDiagnostic(symbol, reason))
                continue
            if calculated is None:
                raise RuntimeError("target quantity calculation lost its result")
            target_quantity = calculated
        intent = intent_for_delta(
            held,
            requested,
            target_quantity=target_quantity,
            created_snapshot_id=market.id,
            created_market_at=market.occurred_at,
        )
        if intent is not None:
            planned.append(intent)

    effect_priority = {
        PositionEffect.CLOSE: 0,
        PositionEffect.REDUCE: 1,
        PositionEffect.INCREASE: 2,
        PositionEffect.OPEN: 3,
    }
    planned.sort(key=lambda item: (effect_priority[item.position_effect], item.symbol))
    diagnostics.sort(key=lambda item: (item.symbol, item.reason))
    return IntentPlanningResult(tuple(planned), tuple(diagnostics))


def _single_stage_fact(
    stage_input: StageInput,
    kinds: tuple[str, ...],
) -> Mapping[str, object] | None:
    matching = [
        fact
        for fact in stage_input.upstream_facts
        if isinstance(fact, Mapping) and fact.get("kind") in kinds
    ]
    if len(matching) > 1:
        raise PipelineContractError(
            f"duplicate upstream fact: {'/'.join(kinds)}"
        )
    return matching[0] if matching else None


class RebalanceIntentStage:
    """Pure pipeline stage converting admitted targets and risk exits to intents."""

    name = "rebalance_intent"
    component_version = "2.0.0"

    def __init__(
        self,
        account: AccountSnapshot,
        market: MarketSnapshot,
        policy: ExecutionPolicy,
        *,
        account_equity: object,
    ) -> None:
        if type(account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        if type(market) is not MarketSnapshot:
            raise TypeError("market must be MarketSnapshot")
        if type(policy) is not ExecutionPolicy:
            raise TypeError("policy must be ExecutionPolicy")
        self._account = account
        self._market = market
        self._policy = policy
        self._account_equity = _finite_nonnegative(account_equity, "account_equity")

    def evaluate(self, stage_input: StageInput) -> StageOutput:
        if type(stage_input) is not StageInput:
            raise TypeError("stage_input must be StageInput")
        borrow_fact = _single_stage_fact(stage_input, ("borrow_targets",))
        exposure_fact = _single_stage_fact(stage_input, ("exposure_targets",))
        target_fact = borrow_fact if borrow_fact is not None else exposure_fact
        risk_fact = _single_stage_fact(stage_input, ("risk_intents",))
        targets: object = () if target_fact is None else target_fact.get("items", ())
        risks: object = () if risk_fact is None else risk_fact.get("items", ())
        if not isinstance(targets, (tuple, list)):
            raise PipelineContractError("target items must be a sequence")
        if not isinstance(risks, (tuple, list)):
            raise PipelineContractError("risk_intents items must be a sequence")
        try:
            result = plan_rebalance_intents(
                self._account,
                targets,
                self._market,
                self._policy,
                account_equity=self._account_equity,
                risk_intents=risks,
            )
        except (TypeError, ValueError) as exc:
            raise PipelineContractError(str(exc)) from exc
        return StageOutput(
            stage=self.name,
            component_version=self.component_version,
            facts=(
                {"kind": "order_intents", "items": result.intents},
                {
                    "kind": "execution_planning_diagnostics",
                    "items": tuple(
                        {"symbol": item.symbol, "reason": item.reason}
                        for item in result.diagnostics
                    ),
                },
            ),
        )


def _direction_semantics_are_valid(intent: OrderIntent) -> bool:
    expected = (
        OrderSide.BUY
        if (
            intent.position_side is PositionSide.LONG
            and intent.position_effect
            in {PositionEffect.OPEN, PositionEffect.INCREASE}
        )
        or (
            intent.position_side is PositionSide.SHORT
            and intent.position_effect
            in {PositionEffect.REDUCE, PositionEffect.CLOSE}
        )
        else OrderSide.SELL
    )
    return intent.order_side is expected


def _quote_number(
    quote: Mapping[str, object],
    key: str,
    *,
    positive: bool = False,
) -> float | None:
    if key not in quote or type(quote[key]) not in (int, float):
        return None
    try:
        number = float(quote[key])
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def _fill_quantity(
    intent: OrderIntent,
    bar_volume: float,
    policy: ExecutionPolicy,
) -> int:
    capacity = int(
        (
            Decimal(str(bar_volume))
            * Decimal(str(policy.max_bar_participation_pct))
            / Decimal(100)
        ).to_integral_value(rounding=ROUND_DOWN)
    )
    if capacity <= 0:
        return 0
    lot = policy.lot_size
    if (
        not intent.increases_risk
        and intent.quantity < lot
        and capacity >= intent.quantity
    ):
        return intent.quantity
    return min(intent.quantity, capacity // lot * lot)


def _fill_price(
    intent: OrderIntent,
    quote: Mapping[str, object],
    policy: ExecutionPolicy,
) -> float | None:
    references: list[float] = []
    for field_name in ("bar_open", "price"):
        if field_name not in quote:
            continue
        reference = _quote_number(quote, field_name, positive=True)
        if reference is None:
            return None
        references.append(reference)
    base = _quote_number(quote, "bar_open", positive=True)
    if base is None:
        base = _quote_number(quote, "price", positive=True)
    if base is None:
        return None
    high = (
        _quote_number(quote, "bar_high", positive=True)
        if "bar_high" in quote
        else _quote_number(quote, "high", positive=True)
    )
    low = (
        _quote_number(quote, "bar_low", positive=True)
        if "bar_low" in quote
        else _quote_number(quote, "low", positive=True)
    )
    if ("bar_high" in quote or "high" in quote) and high is None:
        return None
    if ("bar_low" in quote or "low" in quote) and low is None:
        return None
    if high is not None and low is not None and low > high:
        return None
    if any(
        (low is not None and reference < low)
        or (high is not None and reference > high)
        for reference in references
    ):
        return None
    try:
        slip = Decimal(str(policy.slippage_bps)) / Decimal(10_000)
        price = Decimal(str(base)) * (
            Decimal(1) + slip
            if intent.order_side is OrderSide.BUY
            else Decimal(1) - slip
        )
        if high is not None:
            price = min(price, Decimal(str(high)))
        if low is not None:
            price = max(price, Decimal(str(low)))
        rounded = float(price.quantize(Decimal("0.0001")))
    except (ArithmeticError, DecimalException, OverflowError, ValueError):
        return None
    return rounded if math.isfinite(rounded) and rounded > 0 else None


def _fees(
    intent: OrderIntent,
    notional: Decimal,
    policy: ExecutionPolicy,
    *,
    previous_filled_notional: float,
) -> float:
    previous = Decimal(str(previous_filled_notional))
    charged = _commission_entitlement(previous, policy)
    cumulative = previous + notional
    commission = max(
        Decimal(str(policy.minimum_commission)),
        cumulative * Decimal(str(policy.commission_rate_pct)) / Decimal(100),
    ) - charged
    commission = max(Decimal(0), commission)
    transfer = (
        notional
        * Decimal(str(policy.transfer_fee_rate_pct))
        / Decimal(100)
    )
    stamp = (
        notional
        * Decimal(str(policy.stamp_duty_rate_pct))
        / Decimal(100)
        if intent.order_side is OrderSide.SELL
        else Decimal(0)
    )
    total = commission + transfer + stamp
    result = float(total)
    if not math.isfinite(result) or result < 0:
        raise ValueError("fees must be finite and nonnegative")
    return 0.0 if result == 0.0 else result


def _commission_entitlement(
    cumulative_notional: Decimal,
    policy: ExecutionPolicy,
) -> Decimal:
    if cumulative_notional <= 0:
        return Decimal(0)
    return max(
        Decimal(str(policy.minimum_commission)),
        cumulative_notional
        * Decimal(str(policy.commission_rate_pct))
        / Decimal(100),
    )


def _canonical_progress_notional(progress: OrderExecutionProgress) -> Decimal:
    return sum(
        (
            Decimal(str(fill.price)) * fill.quantity
            for fill in progress.fills
        ),
        Decimal(0),
    )


def _validate_progress_cost_history(
    progress: OrderExecutionProgress,
    policy: ExecutionPolicy,
) -> None:
    """Audit claims against canonical fill facts; claims never grant entitlement."""

    cumulative_notional = Decimal(0)
    for fill in progress.fills:
        prior_commission = _commission_entitlement(cumulative_notional, policy)
        fill_notional = Decimal(str(fill.price)) * fill.quantity
        cumulative_notional += fill_notional
        expected_commission = float(
            _commission_entitlement(cumulative_notional, policy)
            - prior_commission
        )
        transfer = (
            fill_notional
            * Decimal(str(policy.transfer_fee_rate_pct))
            / Decimal(100)
        )
        stamp = (
            fill_notional
            * Decimal(str(policy.stamp_duty_rate_pct))
            / Decimal(100)
            if fill.order_side is OrderSide.SELL
            else Decimal(0)
        )
        expected_fees = expected_commission + float(transfer + stamp)
        if not math.isclose(
            fill.commission,
            expected_commission,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError(
                "prior progress commission does not match canonical fill facts"
            )
        if not math.isclose(
            fill.fees,
            expected_fees,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("prior progress fees do not match canonical fill facts")


def simulate_fill(
    intent: OrderIntent,
    quote: Mapping[str, object] | None,
    policy: ExecutionPolicy,
    *,
    current_snapshot_id: str | None = None,
    previous_filled_notional: float = 0.0,
    existing_position: PositionSnapshot | None = None,
) -> ExecutionFill | None:
    """Simulate at most one paper fill, failing closed on invalid market data."""

    if type(intent) is not OrderIntent:
        raise TypeError("intent must be OrderIntent")
    if type(policy) is not ExecutionPolicy:
        raise TypeError("policy must be ExecutionPolicy")
    if not verify_intent_id(intent, existing_position):
        return None
    return _simulate_fill_unchecked(
        intent,
        quote,
        policy,
        current_snapshot_id=current_snapshot_id,
        previous_filled_notional=previous_filled_notional,
    )


def _simulate_fill_unchecked(
    intent: OrderIntent,
    quote: Mapping[str, object] | None,
    policy: ExecutionPolicy,
    *,
    current_snapshot_id: str | None = None,
    previous_filled_notional: float = 0.0,
) -> ExecutionFill | None:
    """Fill an already-authenticated intent or an internal partial remainder."""

    previous_filled_notional = _finite_nonnegative(
        previous_filled_notional, "previous_filled_notional"
    )
    if current_snapshot_id is not None:
        if type(current_snapshot_id) is not str or not current_snapshot_id:
            raise ValueError("current_snapshot_id must be a non-empty string")
        if current_snapshot_id == intent.created_snapshot_id:
            return None
    if not _direction_semantics_are_valid(intent) or not isinstance(quote, Mapping):
        return None
    volume = _quote_number(quote, "bar_volume", positive=True)
    if volume is None:
        return None
    try:
        quantity = _fill_quantity(intent, volume, policy)
        if quantity <= 0:
            return None
        price = _fill_price(intent, quote, policy)
        if price is None:
            return None
        notional = Decimal(str(price)) * quantity
        if not notional.is_finite() or notional <= 0:
            return None
        fees = _fees(
            intent,
            notional,
            policy,
            previous_filled_notional=previous_filled_notional,
        )
        return ExecutionFill(
            intent_id=intent.id,
            symbol=intent.symbol,
            quantity=quantity,
            price=price,
            fees=fees,
            status="FILLED" if quantity == intent.quantity else "PARTIAL",
        )
    except (ArithmeticError, DecimalException, OverflowError, ValueError):
        return None


def _unlock_long_positions(
    account: AccountSnapshot,
    market: MarketSnapshot,
    policy: ExecutionPolicy,
) -> AccountSnapshot:
    session = market_date(market.occurred_at, policy.market)
    changed = False
    positions: list[PositionSnapshot] = []
    for held in account.positions:
        if (
            held.side is PositionSide.LONG
            and held.sellable_on is not None
            and session >= held.sellable_on
            and held.sellable_quantity != held.quantity
        ):
            held = replace(held, sellable_quantity=held.quantity)
            changed = True
        positions.append(held)
    return replace(account, positions=tuple(positions)) if changed else account


def _sellable_quantity(position: PositionSnapshot) -> int:
    return (
        position.quantity
        if position.sellable_quantity is None
        else position.sellable_quantity
    )


def _capped_for_t_plus_one(
    account: AccountSnapshot,
    intent: OrderIntent,
    policy: ExecutionPolicy,
) -> tuple[OrderIntent | None, str | None]:
    if (
        policy.same_day_sell
        or intent.position_side is PositionSide.SHORT
        or intent.position_effect
        not in {PositionEffect.REDUCE, PositionEffect.CLOSE}
    ):
        return intent, None
    existing = next(
        (item for item in account.positions if item.symbol == intent.symbol),
        None,
    )
    if existing is None:
        return intent, None
    executable = min(intent.quantity, _sellable_quantity(existing))
    if executable <= 0:
        return None, "T_PLUS_ONE_LOCKED"
    effect = (
        PositionEffect.CLOSE
        if executable == existing.quantity
        else PositionEffect.REDUCE
    )
    return replace(intent, quantity=executable, position_effect=effect), None


def _charge_fee(account: AccountSnapshot, fees: float) -> AccountSnapshot:
    amount = Decimal(str(_finite_nonnegative(fees, "fees")))
    cash = Decimal(str(account.available_cash))
    loan = Decimal(str(account.margin_loan))
    available = max(Decimal(0), cash)
    cash_used = min(available, amount)
    cash -= cash_used
    loan += amount - cash_used
    return replace(account, available_cash=float(cash), margin_loan=float(loan))


def _apply_position_sellability(
    before: AccountSnapshot,
    after: AccountSnapshot,
    original_intent: OrderIntent,
    fill: ExecutionFill,
    market: MarketSnapshot,
    policy: ExecutionPolicy,
) -> AccountSnapshot:
    if original_intent.position_side is PositionSide.SHORT:
        return after
    current = next(
        (item for item in after.positions if item.symbol == original_intent.symbol),
        None,
    )
    if current is None:
        return after
    prior = next(
        (item for item in before.positions if item.symbol == original_intent.symbol),
        None,
    )
    if original_intent.position_effect in {
        PositionEffect.OPEN,
        PositionEffect.INCREASE,
    }:
        if policy.same_day_sell:
            sellable = current.quantity
            unlock_date = market_date(market.occurred_at, policy.market)
        else:
            sellable = 0 if prior is None else _sellable_quantity(prior)
            unlock_date = next_business_date(
                market_date(market.occurred_at, policy.market)
            )
    else:
        if policy.same_day_sell:
            sellable = current.quantity
            unlock_date = market_date(market.occurred_at, policy.market)
        else:
            prior_sellable = (
                prior.quantity if prior is not None and prior.sellable_quantity is None
                else prior.sellable_quantity if prior is not None else 0
            )
            sellable = max(0, int(prior_sellable) - fill.quantity)
            unlock_date = prior.sellable_on if prior is not None else None
    updated = replace(
        current,
        sellable_quantity=sellable,
        sellable_on=unlock_date,
    )
    positions = tuple(
        updated if item.symbol == updated.symbol else item
        for item in after.positions
    )
    return replace(after, positions=positions)


def _account_for_position_projection(
    account: AccountSnapshot,
    intent: OrderIntent,
    fill_quantity: int,
) -> AccountSnapshot:
    """Keep long sellability valid while a reduction projection changes quantity."""

    projection_account = account
    if not intent.increases_risk and account.financing_lifecycle is not None:
        projection_account = replace(account, financing_lifecycle=None)
    if intent.position_side is not PositionSide.LONG or intent.increases_risk:
        return projection_account
    held = next(
        (
            item
            for item in projection_account.positions
            if item.symbol == intent.symbol
        ),
        None,
    )
    if held is None or held.sellable_quantity is None:
        return projection_account
    remaining = held.quantity - fill_quantity
    if remaining <= 0 or held.sellable_quantity <= remaining:
        return projection_account
    adjusted = replace(held, sellable_quantity=remaining)
    return replace(
        projection_account,
        positions=tuple(
            adjusted if item.symbol == adjusted.symbol else item
            for item in projection_account.positions
        ),
    )


def _new_accrual_lifecycle(
    kind: str,
    account_id: str,
    intent_id: str,
    market: MarketSnapshot,
    policy: ExecutionPolicy,
) -> AccrualLifecycle:
    material = "|".join((kind, account_id, intent_id, market.id))
    lifecycle_id = kind + "-" + hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()[:24]
    return AccrualLifecycle(
        id=lifecycle_id,
        started_on=market_date(market.occurred_at, policy.market),
    )


def _apply_accrual_lifecycles(
    before: AccountSnapshot,
    after: AccountSnapshot,
    intent: OrderIntent,
    market: MarketSnapshot,
    policy: ExecutionPolicy,
) -> AccountSnapshot:
    prior_by_symbol = {item.symbol: item for item in before.positions}
    positions: list[PositionSnapshot] = []
    changed = False
    for held in after.positions:
        if held.side is not PositionSide.SHORT:
            positions.append(held)
            continue
        prior = prior_by_symbol.get(held.symbol)
        lifecycle = (
            prior.borrow_lifecycle
            if prior is not None
            else _new_accrual_lifecycle(
                "borrow",
                after.id,
                intent.id,
                market,
                policy,
            )
        )
        if held.borrow_lifecycle != lifecycle:
            held = replace(held, borrow_lifecycle=lifecycle)
            changed = True
        positions.append(held)

    financing_lifecycle = after.financing_lifecycle
    if after.margin_loan == 0:
        financing_lifecycle = None
    elif before.margin_loan == 0:
        financing_lifecycle = _new_accrual_lifecycle(
            "financing",
            after.id,
            intent.id,
            market,
            policy,
        )
    elif financing_lifecycle is None:
        financing_lifecycle = before.financing_lifecycle
    if financing_lifecycle != after.financing_lifecycle:
        changed = True
    if not changed:
        return after
    return replace(
        after,
        positions=tuple(positions),
        financing_lifecycle=financing_lifecycle,
    )


def _fill_failure_reason(
    intent: OrderIntent,
    market: MarketSnapshot,
    quote: object,
) -> str:
    if intent.created_snapshot_id == market.id:
        return "SAME_SNAPSHOT"
    if not _direction_semantics_are_valid(intent):
        return "INVALID_INTENT"
    if not isinstance(quote, Mapping):
        return "MARKET_DATA_INVALID"
    if _quote_number(quote, "bar_volume", positive=True) is None:
        return "MARKET_DATA_INVALID"
    return "MARKET_DATA_INVALID"


def _validate_prior_progress_binding(
    intent: OrderIntent,
    progress: OrderExecutionProgress,
    policy: ExecutionPolicy,
) -> None:
    if progress.intent_id != intent.id:
        raise ValueError("prior progress intent_id does not match intent")
    if progress.symbol != intent.symbol:
        raise ValueError("prior progress symbol does not match intent")
    if progress.position_side is not intent.position_side:
        raise ValueError("prior progress position side does not match intent")
    if progress.order_side is not intent.order_side:
        raise ValueError("prior progress order side does not match intent")
    if progress.intent_quantity != intent.quantity:
        raise ValueError("prior progress quantity does not match intent")
    if progress.execution_policy_fingerprint != execution_policy_fingerprint(policy):
        raise ValueError("prior progress execution policy does not match current policy")
    _validate_progress_cost_history(progress, policy)
    if progress.filled_quantity > intent.quantity:
        raise ValueError("prior progress exceeds intent quantity")
    if progress.filled_quantity == intent.quantity:
        if progress.status != "FILLED":
            raise ValueError("completed prior progress must be FILLED")
    elif progress.status != "PARTIAL":
        raise ValueError("incomplete prior progress must be PARTIAL")


def _identity_position_for_progress(
    intent: OrderIntent,
    current_position: PositionSnapshot | None,
    progress: OrderExecutionProgress | None,
) -> PositionSnapshot | None:
    """Reconstruct the immutable risk identity without changing its remainder."""

    if progress is None or not intent.id.startswith("risk-"):
        return current_position
    average_cost = progress.position_average_cost
    if average_cost is None:
        raise ValueError("risk prior progress is missing position average cost")
    if current_position is None:
        if (
            progress.filled_quantity != intent.quantity
            or progress.status != "FILLED"
        ):
            raise ValueError("risk prior progress has no remaining position")
        return PositionSnapshot(
            symbol=intent.symbol,
            side=intent.position_side,
            quantity=intent.quantity,
            average_cost=average_cost,
        )
    if current_position.side is not intent.position_side:
        raise ValueError("risk prior progress direction conflicts with position")
    if current_position.average_cost != average_cost:
        raise ValueError("risk prior progress average cost conflicts with position")
    if current_position.quantity + progress.filled_quantity != intent.quantity:
        raise ValueError("risk prior progress is inconsistent with remaining quantity")
    return replace(
        current_position,
        quantity=intent.quantity,
        average_cost=average_cost,
    )


def execute_intents(
    account: AccountSnapshot,
    intents: Iterable[OrderIntent],
    market: MarketSnapshot,
    policy: ExecutionPolicy,
    *,
    prior_progress: Iterable[OrderExecutionProgress] = (),
) -> ExecutionSimulationResult:
    """Simulate and account for an intent batch without persisting it."""

    if type(account) is not AccountSnapshot:
        raise TypeError("account must be AccountSnapshot")
    if type(market) is not MarketSnapshot:
        raise TypeError("market must be MarketSnapshot")
    if type(policy) is not ExecutionPolicy:
        raise TypeError("policy must be ExecutionPolicy")
    resolved = tuple(intents)
    if any(type(item) is not OrderIntent for item in resolved):
        raise TypeError("intents items must be OrderIntent")
    ids = [item.id for item in resolved]
    if len(ids) != len(set(ids)):
        raise ValueError("intents must not contain duplicate IDs")
    progress_items = tuple(prior_progress)
    if any(type(item) is not OrderExecutionProgress for item in progress_items):
        raise TypeError("prior_progress items must be OrderExecutionProgress")
    progress_by_id = {item.intent_id: item for item in progress_items}
    if len(progress_by_id) != len(progress_items):
        raise ValueError("prior_progress must not contain duplicate intent IDs")
    if any(intent_id not in set(ids) for intent_id in progress_by_id):
        raise ValueError("prior_progress contains an unknown intent ID")

    current = _unlock_long_positions(account, market, policy)
    policy_fingerprint = execution_policy_fingerprint(policy)
    initial_by_symbol = {item.symbol: item for item in current.positions}
    identity_position_by_id: dict[str, PositionSnapshot | None] = {}
    valid_intent_ids: set[str] = set()
    for item in resolved:
        previous = progress_by_id.get(item.id)
        if previous is not None:
            _validate_prior_progress_binding(item, previous, policy)
        identity_position = _identity_position_for_progress(
            item,
            initial_by_symbol.get(item.symbol),
            previous,
        )
        identity_position_by_id[item.id] = identity_position
        if verify_intent_id(item, identity_position):
            valid_intent_ids.add(item.id)
    closing_symbols = {
        item.symbol
        for item in resolved
        if item.position_effect is PositionEffect.CLOSE
        and item.id in valid_intent_ids
    }
    blocked_reversal_ids = {
        item.id
        for item in resolved
        if item.increases_risk and item.symbol in closing_symbols
    }
    fills: list[ExecutionFill] = []
    diagnostics: list[ExecutionDiagnostic] = []
    for original in resolved:
        if original.id not in valid_intent_ids:
            diagnostics.append(
                ExecutionDiagnostic(
                    original.id,
                    original.symbol,
                    "INVALID_INTENT_ID",
                )
            )
            continue
        if original.id in blocked_reversal_ids:
            diagnostics.append(
                ExecutionDiagnostic(
                    original.id,
                    original.symbol,
                    "SAME_BATCH_REVERSAL_BLOCKED",
                )
            )
            continue
        previous = progress_by_id.get(original.id)
        previously_filled = 0 if previous is None else previous.filled_quantity
        if previously_filled == original.quantity:
            continue
        if previous is not None and previous.last_snapshot_id == market.id:
            diagnostics.append(
                ExecutionDiagnostic(
                    original.id,
                    original.symbol,
                    "SAME_SNAPSHOT",
                )
            )
            continue
        if original.created_market_at >= market.occurred_at:
            diagnostics.append(
                ExecutionDiagnostic(
                    original.id,
                    original.symbol,
                    "MARKET_TIME_NOT_LATER",
                )
            )
            continue
        remaining = original.quantity - previously_filled
        resume_effect = original.position_effect
        if previous is not None and resume_effect is PositionEffect.OPEN:
            resume_effect = PositionEffect.INCREASE
        pending = replace(
            original,
            quantity=remaining,
            position_effect=resume_effect,
        )
        executable, reason = _capped_for_t_plus_one(current, pending, policy)
        if executable is None:
            diagnostics.append(
                ExecutionDiagnostic(original.id, original.symbol, str(reason))
            )
            continue
        quote = market.quotes.get(original.symbol)
        prior_notional_decimal = (
            Decimal(0)
            if previous is None
            else _canonical_progress_notional(previous)
        )
        prior_commission_decimal = _commission_entitlement(
            prior_notional_decimal,
            policy,
        )
        fill = _simulate_fill_unchecked(
            executable,
            quote if isinstance(quote, Mapping) else None,
            policy,
            current_snapshot_id=market.id,
            previous_filled_notional=float(prior_notional_decimal),
        )
        if fill is None:
            diagnostics.append(
                ExecutionDiagnostic(
                    original.id,
                    original.symbol,
                    _fill_failure_reason(original, market, quote),
                )
            )
            continue
        if fill.intent_id != original.id:
            raise RuntimeError("fill intent identity changed during simulation")
        fill = replace(
            fill,
            status=(
                "FILLED"
                if previously_filled + fill.quantity == original.quantity
                else "PARTIAL"
            ),
        )
        effect = pending.position_effect
        if effect is PositionEffect.CLOSE and fill.quantity < remaining:
            effect = PositionEffect.REDUCE
        applied_intent = replace(
            original,
            position_effect=effect,
            quantity=fill.quantity,
        )
        before_fill = current
        projection_account = _account_for_position_projection(
            current,
            applied_intent,
            fill.quantity,
        )
        try:
            current = project_account_for_intent(
                projection_account,
                applied_intent,
                {original.symbol: fill.price},
            )
        except (IntentSemanticsError, TypeError, ValueError) as exc:
            diagnostics.append(
                ExecutionDiagnostic(
                    original.id,
                    original.symbol,
                    f"ACCOUNT_PROJECTION_FAILED:{exc}",
                )
            )
            continue
        current = _apply_position_sellability(
            before_fill,
            current,
            original,
            fill,
            market,
            policy,
        )
        current = replace(
            current,
            positions=tuple(
                replace(held, current_price=fill.price)
                if held.symbol == original.symbol
                else held
                for held in current.positions
            ),
        )
        current = _charge_fee(current, fill.fees)
        current = _apply_accrual_lifecycles(
            before_fill,
            current,
            original,
            market,
            policy,
        )
        current = replace(current, occurred_at=market.occurred_at)
        fills.append(fill)
        cumulative_notional_decimal = (
            prior_notional_decimal
            + Decimal(str(fill.price)) * fill.quantity
        )
        incremental_commission = float(
            _commission_entitlement(cumulative_notional_decimal, policy)
            - prior_commission_decimal
        )
        progress_fill_values = {
            "intent_id": original.id,
            "symbol": original.symbol,
            "position_side": original.position_side,
            "order_side": original.order_side,
            "snapshot_id": market.id,
            "occurred_at": market.occurred_at,
            "quantity": fill.quantity,
            "price": fill.price,
            "fees": fill.fees,
            "commission": incremental_commission,
            "status": fill.status,
        }
        progress_fill = ExecutionProgressFill(
            id=stable_execution_progress_fill_id(**progress_fill_values),
            **progress_fill_values,
        )
        progress_by_id[original.id] = OrderExecutionProgress(
            intent_id=original.id,
            symbol=original.symbol,
            position_side=original.position_side,
            order_side=original.order_side,
            intent_quantity=original.quantity,
            execution_policy_fingerprint=policy_fingerprint,
            fills=(
                *(previous.fills if previous is not None else ()),
                progress_fill,
            ),
            position_average_cost=(
                identity_position_by_id[original.id].average_cost
                if original.id.startswith("risk-")
                and identity_position_by_id[original.id] is not None
                else None
            ),
        )
    return ExecutionSimulationResult(
        current,
        tuple(fills),
        tuple(diagnostics),
        tuple(sorted(progress_by_id.values(), key=lambda item: item.intent_id)),
        tuple(
            PositionSettlementUpdate.from_position(held)
            for held in sorted(current.positions, key=lambda item: item.symbol)
        ),
    )


class ExecutionSimulationStage:
    """Pure pipeline adapter for paper fills and account projections."""

    name = "execution_simulation"
    component_version = "2.0.0"

    def __init__(
        self,
        account: AccountSnapshot,
        market: MarketSnapshot,
        policy: ExecutionPolicy,
    ) -> None:
        if type(account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        if type(market) is not MarketSnapshot:
            raise TypeError("market must be MarketSnapshot")
        if type(policy) is not ExecutionPolicy:
            raise TypeError("policy must be ExecutionPolicy")
        self._account = account
        self._market = market
        self._policy = policy

    def evaluate(self, stage_input: StageInput) -> StageOutput:
        if type(stage_input) is not StageInput:
            raise TypeError("stage_input must be StageInput")
        pre_execution_admitted = [
            fact
            for fact in stage_input.upstream_facts
            if isinstance(fact, Mapping)
            and fact.get("kind") == "pre_execution_admitted_intents"
        ]
        admitted = [
            fact
            for fact in stage_input.upstream_facts
            if isinstance(fact, Mapping)
            and fact.get("kind") == "margin_admitted_intents"
        ]
        planned = [
            fact
            for fact in stage_input.upstream_facts
            if isinstance(fact, Mapping) and fact.get("kind") == "order_intents"
        ]
        selected = (
            pre_execution_admitted
            if pre_execution_admitted
            else admitted if admitted else planned
        )
        if len(selected) > 1:
            raise PipelineContractError("duplicate upstream execution intent fact")
        items: object = () if not selected else selected[0].get("items", ())
        if not isinstance(items, (tuple, list)):
            raise PipelineContractError("execution intent items must be a sequence")
        progress_facts = [
            fact
            for fact in stage_input.upstream_facts
            if isinstance(fact, Mapping) and fact.get("kind") == "execution_progress"
        ]
        if len(progress_facts) > 1:
            raise PipelineContractError("duplicate upstream fact: execution_progress")
        progress: object = (
            () if not progress_facts else progress_facts[0].get("items", ())
        )
        if not isinstance(progress, (tuple, list)):
            raise PipelineContractError("execution_progress items must be a sequence")
        try:
            result = execute_intents(
                self._account,
                items,
                self._market,
                self._policy,
                prior_progress=progress,
            )
        except (TypeError, ValueError) as exc:
            raise PipelineContractError(str(exc)) from exc
        return StageOutput(
            stage=self.name,
            component_version=self.component_version,
            facts=(
                {"kind": "execution_fills", "items": result.fills},
                {"kind": "execution_account", "account": result.account},
                {"kind": "execution_progress", "items": result.progress},
                {
                    "kind": "position_settlement_updates",
                    "items": result.settlement_updates,
                },
                {
                    "kind": "execution_diagnostics",
                    "items": tuple(
                        {
                            "intent_id": item.intent_id,
                            "symbol": item.symbol,
                            "reason": item.reason,
                        }
                        for item in result.diagnostics
                    ),
                },
            ),
        )


def _validate_carry_inputs(
    account: AccountSnapshot,
    as_of: date,
    financing_apr_pct: object,
    borrow_apr_by_symbol: Mapping[str, object] | None,
    estimated_borrow_apr_pct: object,
    cost_multiplier: object,
) -> tuple[float, dict[str, object], float, float]:
    if type(account) is not AccountSnapshot:
        raise TypeError("account must be AccountSnapshot")
    if type(as_of) is not date:
        raise TypeError("as_of must be a date")
    financing = _finite_nonnegative(financing_apr_pct, "financing_apr_pct")
    estimated = _finite_nonnegative(
        estimated_borrow_apr_pct,
        "estimated_borrow_apr_pct",
    )
    multiplier = _finite_nonnegative(cost_multiplier, "cost_multiplier")
    if borrow_apr_by_symbol is None:
        borrow_apr_by_symbol = {}
    if not isinstance(borrow_apr_by_symbol, Mapping):
        raise TypeError("borrow_apr_by_symbol must be a mapping")
    rates: dict[str, object] = {}
    for symbol, rate in borrow_apr_by_symbol.items():
        if type(symbol) is not str or not symbol:
            raise ValueError("borrow APR symbol must be non-empty")
        rates[symbol] = rate
    return financing, rates, estimated, multiplier


def _accrual_elapsed_days(
    account: AccountSnapshot,
    cost_type: CarryCostType,
    as_of: date,
    lifecycle: AccrualLifecycle,
    symbol: str | None = None,
) -> int | None:
    key = (account.id, cost_type, lifecycle.id, as_of, symbol)
    if any(item.idempotency_key == key for item in account.carry_accruals):
        return None
    previous = [
        item.accrual_date
        for item in account.carry_accruals
        if item.cost_type is cost_type
        and item.symbol == symbol
        and item.lifecycle_id == lifecycle.id
    ]
    start = max(
        lifecycle.started_on,
        max(previous) if previous else lifecycle.started_on,
    )
    elapsed = (as_of - start).days
    if elapsed < 0:
        raise ValueError("as_of must not precede the last carry accrual date")
    return elapsed or None


def _short_price(
    position: PositionSnapshot,
    prices: Mapping[str, object],
) -> Decimal:
    raw: object | None = prices.get(position.symbol)
    if isinstance(raw, Mapping):
        raw = raw.get("price")
    if raw is None:
        raw = position.current_price
    if type(raw) not in (int, float):
        raise ValueError(f"short price missing for {position.symbol}")
    try:
        number = float(raw)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"short price invalid for {position.symbol}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"short price invalid for {position.symbol}")
    return Decimal(str(number))


def _carry_event(
    record: CarryAccrualRecord,
    occurred_at: datetime,
) -> PortfolioEvent:
    material = "|".join(
        (
            record.account_id,
            record.cost_type.value,
            record.lifecycle_id,
            record.accrual_date.isoformat(),
            record.symbol or "",
        )
    )
    event_id = "carry-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return PortfolioEvent(
        id=event_id,
        type=f"{record.cost_type.value}_COST_ACCRUED",
        occurred_at=occurred_at,
        data={
            "account_id": record.account_id,
            "cost_type": record.cost_type.value,
            "lifecycle_id": record.lifecycle_id,
            "symbol": record.symbol,
            "accrual_date": record.accrual_date.isoformat(),
            "elapsed_days": record.elapsed_days,
            "amount": record.amount,
        },
    )


def accrue_carry_costs(
    account: AccountSnapshot,
    *,
    as_of: date,
    prices: Mapping[str, object] | None = None,
    financing_apr_pct: object = 8.0,
    borrow_apr_by_symbol: Mapping[str, object] | None = None,
    estimated_borrow_apr_pct: object = 8.0,
    cost_multiplier: object = 1.0,
) -> CarryAccrualResult:
    """Accrue financing per date and borrow per symbol/date using actual days."""

    financing_apr, borrow_rates, estimated_apr, multiplier = _validate_carry_inputs(
        account,
        as_of,
        financing_apr_pct,
        borrow_apr_by_symbol,
        estimated_borrow_apr_pct,
        cost_multiplier,
    )
    if prices is None:
        prices = {}
    if not isinstance(prices, Mapping):
        raise TypeError("prices must be a mapping")

    new_records: list[CarryAccrualRecord] = []
    diagnostics: list[CarryAccrualDiagnostic] = []
    amounts: dict[CarryCostType, float] = {
        CarryCostType.FINANCING: 0.0,
        CarryCostType.BORROW: 0.0,
    }
    financing_days: int | None = None
    if account.margin_loan > 0:
        if account.financing_lifecycle is None:
            raise ValueError("active margin loan requires financing_lifecycle")
        financing_days = _accrual_elapsed_days(
            account,
            CarryCostType.FINANCING,
            as_of,
            account.financing_lifecycle,
        )
    if financing_days is not None and account.financing_lifecycle is not None:
        financing_cost = (
            Decimal(str(account.margin_loan))
            * Decimal(str(financing_apr))
            / Decimal(100)
            * financing_days
            / Decimal(365)
            * Decimal(str(multiplier))
        )
        amount = float(financing_cost)
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("financing cost must be finite and nonnegative")
        amounts[CarryCostType.FINANCING] = amount
        new_records.append(
            CarryAccrualRecord(
                account_id=account.id,
                cost_type=CarryCostType.FINANCING,
                accrual_date=as_of,
                elapsed_days=financing_days,
                amount=amount,
                lifecycle_id=account.financing_lifecycle.id,
            )
        )

    for held in sorted(account.positions, key=lambda item: item.symbol):
        if held.side is not PositionSide.SHORT:
            continue
        if held.borrow_lifecycle is None:
            diagnostics.append(
                CarryAccrualDiagnostic(held.symbol, "BORROW_LIFECYCLE_MISSING")
            )
            continue
        borrow_days = _accrual_elapsed_days(
            account,
            CarryCostType.BORROW,
            as_of,
            held.borrow_lifecycle,
            held.symbol,
        )
        if borrow_days is None:
            continue
        raw_rate = borrow_rates.get(held.symbol)
        try:
            effective_rate = (
                estimated_apr
                if raw_rate is None
                else _finite_nonnegative(raw_rate, "borrow APR")
            )
        except (TypeError, ValueError):
            diagnostics.append(
                CarryAccrualDiagnostic(held.symbol, "BORROW_RATE_INVALID")
            )
            continue
        try:
            liability = _short_price(held, prices) * held.quantity
        except ValueError as exc:
            reason = (
                "SHORT_PRICE_MISSING"
                if "missing" in str(exc)
                else "SHORT_PRICE_INVALID"
            )
            diagnostics.append(CarryAccrualDiagnostic(held.symbol, reason))
            continue
        try:
            borrow_cost = (
                liability
                * Decimal(str(effective_rate))
                / Decimal(100)
                * borrow_days
                / Decimal(365)
                * Decimal(str(multiplier))
            )
            amount = float(borrow_cost)
        except (ArithmeticError, DecimalException, OverflowError, ValueError):
            diagnostics.append(
                CarryAccrualDiagnostic(held.symbol, "BORROW_COST_INVALID")
            )
            continue
        if not math.isfinite(amount) or amount < 0:
            diagnostics.append(
                CarryAccrualDiagnostic(held.symbol, "BORROW_COST_INVALID")
            )
            continue
        amounts[CarryCostType.BORROW] += amount
        new_records.append(
            CarryAccrualRecord(
                account_id=account.id,
                cost_type=CarryCostType.BORROW,
                accrual_date=as_of,
                elapsed_days=borrow_days,
                amount=amount,
                lifecycle_id=held.borrow_lifecycle.id,
                symbol=held.symbol,
            )
        )

    if not new_records:
        return CarryAccrualResult(
            account,
            0.0,
            0.0,
            diagnostics=tuple(diagnostics),
        )
    total = sum(amounts.values(), 0.0)
    if not math.isfinite(total):
        raise ValueError("carry cost total must be finite")
    updated = replace(
        account,
        available_cash=account.available_cash - total,
        accrued_financing_cost=(
            account.accrued_financing_cost
            + amounts[CarryCostType.FINANCING]
        ),
        accrued_borrow_cost=(
            account.accrued_borrow_cost + amounts[CarryCostType.BORROW]
        ),
        carry_accruals=(*account.carry_accruals, *new_records),
    )
    event_time = datetime.combine(as_of, account.occurred_at.timetz())
    events = tuple(
        _carry_event(record, event_time)
        for record in new_records
        if record.amount > 0
    )
    return CarryAccrualResult(
        updated,
        amounts[CarryCostType.FINANCING],
        amounts[CarryCostType.BORROW],
        tuple(new_records),
        events,
        tuple(diagnostics),
    )


__all__ = (
    "CarryAccrualResult",
    "ExecutionPolicy",
    "ExecutionDiagnostic",
    "ExecutionSimulationResult",
    "ExecutionSimulationStage",
    "OrderExecutionProgress",
    "IntentPlanningResult",
    "PlanningDiagnostic",
    "RebalanceIntentStage",
    "execution_policy",
    "execution_policy_fingerprint",
    "execute_intents",
    "accrue_carry_costs",
    "intent_for_delta",
    "plan_rebalance_intents",
    "simulate_fill",
    "verify_intent_id",
)
