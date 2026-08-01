"""Schema-v2 JSON ledger with atomic, idempotent account transitions."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..pipeline import StageOutput
from .canonical import (
    CanonicalGraphError,
    canonical_graph,
    decode_canonical_graph,
)
from .contracts import (
    AccrualLifecycle,
    AccountSnapshot,
    CarryAccrualRecord,
    CarryCostType,
    DecisionBatch,
    ExecutionFill,
    ExecutionProgressFill,
    OrderExecutionProgress,
    OrderIntent,
    OrderSide,
    PortfolioEvent,
    PortfolioLedgerView,
    RevisionTransition,
    PositionEffect,
    PositionRiskUpdate,
    PositionSettlementUpdate,
    PositionSide,
    PositionSnapshot,
    SignalCandidate,
    TargetPosition,
)
from .atomic_io import atomic_replace_bytes, transaction_guard
from .margin import project_account_for_intent
from .valuation import value_account


LEDGER_SCHEMA_VERSION = 2
_PROCESS_LOCK = threading.RLock()
_ACCOUNT_SNAPSHOT_FIELDS = frozenset(
    {
        "id",
        "strategy_id",
        "strategy_revision",
        "occurred_at",
        "available_cash",
        "reserved_cash",
        "restricted_short_proceeds",
        "margin_loan",
        "accrued_financing_cost",
        "accrued_borrow_cost",
        "positions",
        "carry_accruals",
        "financing_lifecycle",
        "portfolio_snapshot_id",
    }
)
_ACCOUNT_FIELDS = _ACCOUNT_SNAPSHOT_FIELDS | frozenset(
    {
        "open_intents",
        "fills",
        "execution_progress",
        "risk_facts",
        "revision_transitions",
        "events",
        "committed_batches",
        "run_results",
    }
)
_RISK_FACT_FIELDS = frozenset(
    {
        "fact_id",
        "strategy_id",
        "strategy_revision",
        "run_key",
        "portfolio_snapshot_id",
        "market_snapshot_id",
        "occurred_at",
        "update",
    }
)
_REVISION_TRANSITION_FIELDS = frozenset(
    {
        "transition_id",
        "strategy_id",
        "from_revision",
        "to_revision",
        "source_snapshot_id",
        "result_snapshot_id",
        "occurred_at",
        "cancelled_intent_ids",
    }
)


class LedgerError(ValueError):
    """Base class for ledger validation and transition failures."""


class LedgerSchemaError(LedgerError):
    """Raised when persisted JSON is malformed or not schema v2."""


class StalePortfolioSnapshotError(LedgerError):
    """Raised when a new run was evaluated from an obsolete snapshot."""


class UnknownPortfolioEventError(LedgerError):
    """Raised when no exhaustive event handler exists."""


@dataclass(frozen=True)
class _RiskFact:
    fact_id: str
    strategy_id: str
    strategy_revision: int
    run_key: str
    portfolio_snapshot_id: str
    market_snapshot_id: str
    occurred_at: datetime
    update: PositionRiskUpdate


@dataclass(frozen=True)
class _RevisionTransitionFact:
    transition_id: str
    strategy_id: str
    from_revision: int
    to_revision: int
    source_snapshot_id: str
    result_snapshot_id: str
    occurred_at: datetime
    cancelled_intent_ids: tuple[str, ...]


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LedgerSchemaError(f"{label} must be an object")
    return value


def _require_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise LedgerSchemaError(f"{label} must be an array")
    return value


def _require_keys(
    value: Mapping[str, Any],
    expected: frozenset[str],
    label: str,
    *,
    exact: bool = True,
) -> None:
    actual = set(value)
    missing = expected - actual
    extra = actual - expected if exact else set()
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unexpected " + ", ".join(sorted(map(str, extra))))
        raise LedgerSchemaError(f"{label} fields are invalid: {'; '.join(details)}")


def _iso_datetime(value: object, label: str) -> datetime:
    if type(value) is not str:
        raise LedgerSchemaError(f"{label} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LedgerSchemaError(f"{label} must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise LedgerSchemaError(f"{label} must include a timezone")
    return parsed


def _iso_date(value: object, label: str) -> date:
    if type(value) is not str:
        raise LedgerSchemaError(f"{label} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise LedgerSchemaError(f"{label} must be an ISO date") from exc


def _enum(enum_type: type[Any], value: object, label: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise LedgerSchemaError(f"invalid {label}: {value!r}") from exc


def _lifecycle_to_json(value: AccrualLifecycle | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"id": value.id, "started_on": value.started_on.isoformat()}


def _lifecycle_from_json(value: object, label: str) -> AccrualLifecycle | None:
    if value is None:
        return None
    item = _require_mapping(value, label)
    _require_keys(item, frozenset({"id", "started_on"}), label)
    try:
        return AccrualLifecycle(
            id=item["id"],
            started_on=_iso_date(item["started_on"], f"{label}.started_on"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, LedgerSchemaError):
            raise
        raise LedgerSchemaError(f"invalid {label}") from exc


def _position_to_json(position: PositionSnapshot) -> dict[str, Any]:
    return {
        "symbol": position.symbol,
        "side": position.side.value,
        "quantity": position.quantity,
        "average_cost": position.average_cost,
        "current_price": position.current_price,
        "peak_price": position.peak_price,
        "trough_price": position.trough_price,
        "trailing_active": position.trailing_active,
        "position_mode": position.position_mode,
        "sellable_quantity": position.sellable_quantity,
        "sellable_on": (
            None if position.sellable_on is None else position.sellable_on.isoformat()
        ),
        "borrow_lifecycle": _lifecycle_to_json(position.borrow_lifecycle),
    }


def _position_from_json(symbol: str, value: object) -> PositionSnapshot:
    item = _require_mapping(value, f"position[{symbol}]")
    _require_keys(
        item,
        frozenset(
            {
                "symbol",
                "side",
                "quantity",
                "average_cost",
                "current_price",
                "peak_price",
                "trough_price",
                "trailing_active",
                "position_mode",
                "sellable_quantity",
                "sellable_on",
                "borrow_lifecycle",
            }
        ),
        f"position[{symbol}]",
    )
    try:
        embedded_symbol = item["symbol"]
        if embedded_symbol != symbol:
            raise LedgerSchemaError("position key and symbol differ")
        return PositionSnapshot(
            symbol=symbol,
            side=_enum(PositionSide, item["side"], "position.side"),
            quantity=item["quantity"],
            average_cost=item["average_cost"],
            current_price=item["current_price"],
            peak_price=item["peak_price"],
            trough_price=item["trough_price"],
            trailing_active=item["trailing_active"],
            position_mode=item["position_mode"],
            sellable_quantity=item["sellable_quantity"],
            sellable_on=(
                None
                if item["sellable_on"] is None
                else _iso_date(item["sellable_on"], "position.sellable_on")
            ),
            borrow_lifecycle=_lifecycle_from_json(
                item["borrow_lifecycle"], "position.borrow_lifecycle"
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, LedgerSchemaError):
            raise
        raise LedgerSchemaError(f"invalid position[{symbol}]") from exc


def _carry_to_json(record: CarryAccrualRecord) -> dict[str, Any]:
    return {
        "account_id": record.account_id,
        "cost_type": record.cost_type.value,
        "accrual_date": record.accrual_date.isoformat(),
        "elapsed_days": record.elapsed_days,
        "amount": record.amount,
        "lifecycle_id": record.lifecycle_id,
        "symbol": record.symbol,
    }


def _carry_from_json(value: object) -> CarryAccrualRecord:
    item = _require_mapping(value, "carry_accrual")
    _require_keys(
        item,
        frozenset(
            {
                "account_id",
                "cost_type",
                "accrual_date",
                "elapsed_days",
                "amount",
                "lifecycle_id",
                "symbol",
            }
        ),
        "carry_accrual",
    )
    try:
        return CarryAccrualRecord(
            account_id=item["account_id"],
            cost_type=_enum(CarryCostType, item["cost_type"], "cost_type"),
            accrual_date=_iso_date(item["accrual_date"], "accrual_date"),
            elapsed_days=item["elapsed_days"],
            amount=item["amount"],
            lifecycle_id=item["lifecycle_id"],
            symbol=item["symbol"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, LedgerSchemaError):
            raise
        raise LedgerSchemaError("invalid carry accrual") from exc


def encode_account_snapshot(account: AccountSnapshot) -> dict[str, Any]:
    """Serialize a validated account using stable symbol ordering."""

    if type(account) is not AccountSnapshot:
        raise TypeError("account must be AccountSnapshot")
    return {
        "id": account.id,
        "strategy_id": account.strategy_id,
        "strategy_revision": account.strategy_revision,
        "occurred_at": account.occurred_at.isoformat(),
        "available_cash": account.available_cash,
        "reserved_cash": account.reserved_cash,
        "restricted_short_proceeds": account.restricted_short_proceeds,
        "margin_loan": account.margin_loan,
        "accrued_financing_cost": account.accrued_financing_cost,
        "accrued_borrow_cost": account.accrued_borrow_cost,
        "positions": {
            item.symbol: _position_to_json(item)
            for item in sorted(account.positions, key=lambda held: held.symbol)
        },
        "carry_accruals": [_carry_to_json(item) for item in account.carry_accruals],
        "financing_lifecycle": _lifecycle_to_json(account.financing_lifecycle),
        "portfolio_snapshot_id": account.snapshot_id or account.id,
    }


def decode_account_snapshot(value: object) -> AccountSnapshot:
    """Deserialize and validate one schema-v2 account payload."""

    item = _require_mapping(value, "account")
    _require_keys(item, _ACCOUNT_SNAPSHOT_FIELDS, "account", exact=False)
    positions_payload = _require_mapping(item["positions"], "account.positions")
    accrual_payload = _require_list(item["carry_accruals"], "account.carry_accruals")
    try:
        account = AccountSnapshot(
            id=item["id"],
            strategy_id=item["strategy_id"],
            strategy_revision=item["strategy_revision"],
            occurred_at=_iso_datetime(item["occurred_at"], "account.occurred_at"),
            available_cash=item["available_cash"],
            reserved_cash=item["reserved_cash"],
            restricted_short_proceeds=item["restricted_short_proceeds"],
            margin_loan=item["margin_loan"],
            accrued_financing_cost=item["accrued_financing_cost"],
            accrued_borrow_cost=item["accrued_borrow_cost"],
            positions=tuple(
                _position_from_json(symbol, positions_payload[symbol])
                for symbol in sorted(positions_payload)
            ),
            carry_accruals=tuple(_carry_from_json(raw) for raw in accrual_payload),
            financing_lifecycle=_lifecycle_from_json(
                item["financing_lifecycle"], "account.financing_lifecycle"
            ),
            snapshot_id=item["portfolio_snapshot_id"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, LedgerSchemaError):
            raise
        raise LedgerSchemaError("invalid account snapshot") from exc
    _validate_account(account)
    return account


def _intent_to_json(intent: OrderIntent) -> dict[str, Any]:
    return {
        "id": intent.id,
        "symbol": intent.symbol,
        "position_side": intent.position_side.value,
        "order_side": intent.order_side.value,
        "position_effect": intent.position_effect.value,
        "quantity": intent.quantity,
        "reason": intent.reason,
        "created_snapshot_id": intent.created_snapshot_id,
        "created_market_at": intent.created_market_at.isoformat(),
    }


def _intent_from_json(value: object) -> OrderIntent:
    item = _require_mapping(value, "intent")
    _require_keys(
        item,
        frozenset(
            {
                "id",
                "symbol",
                "position_side",
                "order_side",
                "position_effect",
                "quantity",
                "reason",
                "created_snapshot_id",
                "created_market_at",
            }
        ),
        "intent",
    )
    try:
        return OrderIntent(
            id=item["id"],
            symbol=item["symbol"],
            position_side=_enum(PositionSide, item["position_side"], "position_side"),
            order_side=_enum(OrderSide, item["order_side"], "order_side"),
            position_effect=_enum(
                PositionEffect, item["position_effect"], "position_effect"
            ),
            quantity=item["quantity"],
            reason=item["reason"],
            created_snapshot_id=item["created_snapshot_id"],
            created_market_at=_iso_datetime(
                item["created_market_at"],
                "intent.created_market_at",
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, LedgerSchemaError):
            raise
        raise LedgerSchemaError("invalid order intent") from exc


def _fill_to_json(
    fill: ExecutionFill,
    *,
    progress_fill_id: str,
) -> dict[str, Any]:
    return {
        "progress_fill_id": progress_fill_id,
        "intent_id": fill.intent_id,
        "symbol": fill.symbol,
        "quantity": fill.quantity,
        "price": fill.price,
        "fees": fill.fees,
        "status": fill.status,
    }


def _fill_from_json(value: object) -> tuple[str, ExecutionFill]:
    item = _require_mapping(value, "fill")
    _require_keys(
        item,
        frozenset(
            {
                "progress_fill_id",
                "intent_id",
                "symbol",
                "quantity",
                "price",
                "fees",
                "status",
            }
        ),
        "fill",
    )
    try:
        progress_fill_id = item["progress_fill_id"]
        if type(progress_fill_id) is not str or not progress_fill_id:
            raise LedgerSchemaError("fill.progress_fill_id must be non-empty")
        fill = ExecutionFill(
            intent_id=item["intent_id"],
            symbol=item["symbol"],
            quantity=item["quantity"],
            price=item["price"],
            fees=item["fees"],
            status=item["status"],
        )
        return progress_fill_id, fill
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, LedgerSchemaError):
            raise
        raise LedgerSchemaError("invalid execution fill") from exc


def _event_to_json(event: PortfolioEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "type": event.type,
        "occurred_at": event.occurred_at.isoformat(),
        "data": _json_plain(event.data),
    }


def _event_from_json(value: object) -> PortfolioEvent:
    item = _require_mapping(value, "event")
    _require_keys(item, frozenset({"id", "type", "occurred_at", "data"}), "event")
    try:
        return PortfolioEvent(
            id=item["id"],
            type=item["type"],
            occurred_at=_iso_datetime(item["occurred_at"], "event.occurred_at"),
            data=_require_mapping(item["data"], "event.data"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, LedgerSchemaError):
            raise
        raise LedgerSchemaError("invalid portfolio event") from exc


def _risk_update_to_json(update: PositionRiskUpdate) -> dict[str, Any]:
    return {
        "symbol": update.symbol,
        "side": update.side.value,
        "peak_price": update.peak_price,
        "trough_price": update.trough_price,
        "trailing_active": update.trailing_active,
        "position_mode": update.position_mode,
    }


def _risk_update_from_json(value: object) -> PositionRiskUpdate:
    item = _require_mapping(value, "risk fact update")
    _require_keys(
        item,
        frozenset(
            {
                "symbol",
                "side",
                "peak_price",
                "trough_price",
                "trailing_active",
                "position_mode",
            }
        ),
        "risk fact update",
    )
    try:
        return PositionRiskUpdate(
            symbol=item["symbol"],
            side=PositionSide(item["side"]),
            peak_price=item["peak_price"],
            trough_price=item["trough_price"],
            trailing_active=item["trailing_active"],
            position_mode=item["position_mode"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LedgerSchemaError("invalid risk fact update") from exc


def _risk_fact_to_json(fact: _RiskFact) -> dict[str, Any]:
    return {
        "fact_id": fact.fact_id,
        "strategy_id": fact.strategy_id,
        "strategy_revision": fact.strategy_revision,
        "run_key": fact.run_key,
        "portfolio_snapshot_id": fact.portfolio_snapshot_id,
        "market_snapshot_id": fact.market_snapshot_id,
        "occurred_at": fact.occurred_at.isoformat(),
        "update": _risk_update_to_json(fact.update),
    }


def _risk_fact_from_json(value: object) -> _RiskFact:
    item = _require_mapping(value, "risk fact")
    _require_keys(item, _RISK_FACT_FIELDS, "risk fact")
    for field_name in (
        "fact_id",
        "strategy_id",
        "run_key",
        "portfolio_snapshot_id",
        "market_snapshot_id",
    ):
        if type(item[field_name]) is not str or not item[field_name]:
            raise LedgerSchemaError(f"risk fact {field_name} must be non-empty")
    if type(item["strategy_revision"]) is not int:
        raise LedgerSchemaError("risk fact strategy_revision must be an integer")
    fact = _RiskFact(
        fact_id=item["fact_id"],
        strategy_id=item["strategy_id"],
        strategy_revision=item["strategy_revision"],
        run_key=item["run_key"],
        portfolio_snapshot_id=item["portfolio_snapshot_id"],
        market_snapshot_id=item["market_snapshot_id"],
        occurred_at=_iso_datetime(item["occurred_at"], "risk fact occurred_at"),
        update=_risk_update_from_json(item["update"]),
    )
    expected_id = _stable_risk_fact_id(
        strategy_id=fact.strategy_id,
        strategy_revision=fact.strategy_revision,
        run_key=fact.run_key,
        portfolio_snapshot_id=fact.portfolio_snapshot_id,
        market_snapshot_id=fact.market_snapshot_id,
        update=fact.update,
    )
    if fact.fact_id != expected_id:
        raise LedgerSchemaError("risk fact ID does not match its canonical content")
    return fact


def _revision_transition_result_snapshot_id(
    transition: RevisionTransition,
    cancelled_intent_ids: tuple[str, ...],
) -> str:
    material = json.dumps(
        {
            "transition_id": transition.id,
            "strategy_id": transition.strategy_id,
            "from_revision": transition.from_revision,
            "to_revision": transition.to_revision,
            "source_snapshot_id": transition.expected_snapshot_id,
            "occurred_at": transition.occurred_at.isoformat(),
            "cancelled_intent_ids": list(cancelled_intent_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "revision-snapshot-" + hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()[:24]


def _revision_transition_fact_to_json(
    fact: _RevisionTransitionFact,
) -> dict[str, Any]:
    return {
        "transition_id": fact.transition_id,
        "strategy_id": fact.strategy_id,
        "from_revision": fact.from_revision,
        "to_revision": fact.to_revision,
        "source_snapshot_id": fact.source_snapshot_id,
        "result_snapshot_id": fact.result_snapshot_id,
        "occurred_at": fact.occurred_at.isoformat(),
        "cancelled_intent_ids": list(fact.cancelled_intent_ids),
    }


def _revision_transition_fact_from_json(
    value: object,
) -> _RevisionTransitionFact:
    item = _require_mapping(value, "revision transition fact")
    _require_keys(
        item,
        _REVISION_TRANSITION_FIELDS,
        "revision transition fact",
    )
    cancelled = _require_list(
        item["cancelled_intent_ids"],
        "revision transition cancelled_intent_ids",
    )
    if any(type(intent_id) is not str or not intent_id for intent_id in cancelled):
        raise LedgerSchemaError(
            "revision transition cancelled intent IDs must be non-empty strings"
        )
    if cancelled != sorted(cancelled) or len(set(cancelled)) != len(cancelled):
        raise LedgerSchemaError(
            "revision transition cancelled intent IDs must be unique and sorted"
        )
    try:
        request = RevisionTransition(
            id=item["transition_id"],
            strategy_id=item["strategy_id"],
            expected_snapshot_id=item["source_snapshot_id"],
            from_revision=item["from_revision"],
            to_revision=item["to_revision"],
            occurred_at=_iso_datetime(
                item["occurred_at"],
                "revision transition occurred_at",
            ),
        )
        result_snapshot_id = item["result_snapshot_id"]
        if type(result_snapshot_id) is not str or not result_snapshot_id:
            raise LedgerSchemaError(
                "revision transition result_snapshot_id must be non-empty"
            )
        cancelled_ids = tuple(cancelled)
        if result_snapshot_id != _revision_transition_result_snapshot_id(
            request,
            cancelled_ids,
        ):
            raise LedgerSchemaError(
                "revision transition result snapshot does not match canonical content"
            )
        return _RevisionTransitionFact(
            transition_id=request.id,
            strategy_id=request.strategy_id,
            from_revision=request.from_revision,
            to_revision=request.to_revision,
            source_snapshot_id=request.expected_snapshot_id,
            result_snapshot_id=result_snapshot_id,
            occurred_at=request.occurred_at,
            cancelled_intent_ids=cancelled_ids,
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, LedgerSchemaError):
            raise
        raise LedgerSchemaError("invalid revision transition fact") from exc


def _transition_matches_request(
    fact: _RevisionTransitionFact,
    transition: RevisionTransition,
) -> bool:
    return (
        fact.transition_id == transition.id
        and fact.strategy_id == transition.strategy_id
        and fact.from_revision == transition.from_revision
        and fact.to_revision == transition.to_revision
        and fact.source_snapshot_id == transition.expected_snapshot_id
    )


def _progress_fill_to_json(fill: ExecutionProgressFill) -> dict[str, Any]:
    return {
        "id": fill.id,
        "intent_id": fill.intent_id,
        "symbol": fill.symbol,
        "position_side": fill.position_side.value,
        "order_side": fill.order_side.value,
        "snapshot_id": fill.snapshot_id,
        "occurred_at": fill.occurred_at.isoformat(),
        "quantity": fill.quantity,
        "price": fill.price,
        "fees": fill.fees,
        "commission": fill.commission,
        "status": fill.status,
    }


def _progress_fill_from_json(value: object) -> ExecutionProgressFill:
    item = _require_mapping(value, "execution_progress.fill")
    _require_keys(
        item,
        frozenset(
            {
                "id",
                "intent_id",
                "symbol",
                "position_side",
                "order_side",
                "snapshot_id",
                "occurred_at",
                "quantity",
                "price",
                "fees",
                "commission",
                "status",
            }
        ),
        "execution_progress.fill",
    )
    try:
        return ExecutionProgressFill(
            id=item["id"],
            intent_id=item["intent_id"],
            symbol=item["symbol"],
            position_side=_enum(PositionSide, item["position_side"], "position_side"),
            order_side=_enum(OrderSide, item["order_side"], "order_side"),
            snapshot_id=item["snapshot_id"],
            occurred_at=_iso_datetime(item["occurred_at"], "fill.occurred_at"),
            quantity=item["quantity"],
            price=item["price"],
            fees=item["fees"],
            commission=item["commission"],
            status=item["status"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, LedgerSchemaError):
            raise
        raise LedgerSchemaError("invalid execution progress fill") from exc


def _progress_to_json(progress: OrderExecutionProgress) -> dict[str, Any]:
    return {
        "intent_id": progress.intent_id,
        "symbol": progress.symbol,
        "position_side": progress.position_side.value,
        "order_side": progress.order_side.value,
        "intent_quantity": progress.intent_quantity,
        "execution_policy_fingerprint": progress.execution_policy_fingerprint,
        "fills": [_progress_fill_to_json(item) for item in progress.fills],
        "position_average_cost": progress.position_average_cost,
    }


def _progress_from_json(value: object) -> OrderExecutionProgress:
    item = _require_mapping(value, "execution_progress")
    _require_keys(
        item,
        frozenset(
            {
                "intent_id",
                "symbol",
                "position_side",
                "order_side",
                "intent_quantity",
                "execution_policy_fingerprint",
                "fills",
                "position_average_cost",
            }
        ),
        "execution_progress",
    )
    fills = _require_list(item["fills"], "execution_progress.fills")
    try:
        return OrderExecutionProgress(
            intent_id=item["intent_id"],
            symbol=item["symbol"],
            position_side=_enum(PositionSide, item["position_side"], "position_side"),
            order_side=_enum(OrderSide, item["order_side"], "order_side"),
            intent_quantity=item["intent_quantity"],
            execution_policy_fingerprint=item["execution_policy_fingerprint"],
            fills=tuple(_progress_fill_from_json(raw) for raw in fills),
            position_average_cost=item["position_average_cost"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, LedgerSchemaError):
            raise
        raise LedgerSchemaError("invalid execution progress") from exc


def _json_plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise LedgerError("JSON object keys must be strings")
            result[key] = _json_plain(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_json_plain(item) for item in value]
    if isinstance(value, (PositionSide, OrderSide, PositionEffect, CarryCostType)):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or type(value) in {bool, int, float, str}:
        if type(value) is float and not math.isfinite(value):
            raise LedgerError("non-finite JSON number")
        return value
    raise LedgerError(f"unsupported JSON value: {type(value).__name__}")


def _fingerprint_plain(value: Any) -> Any:
    """Encode immutable domain values without dropping observable fields."""

    return canonical_graph(value)


_RUN_RESULT_DATACLASSES = (
    AccrualLifecycle,
    AccountSnapshot,
    CarryAccrualRecord,
    DecisionBatch,
    ExecutionFill,
    ExecutionProgressFill,
    OrderExecutionProgress,
    OrderIntent,
    PortfolioEvent,
    PositionRiskUpdate,
    PositionSettlementUpdate,
    PositionSnapshot,
    SignalCandidate,
    StageOutput,
    TargetPosition,
)
_RUN_RESULT_ENUMS = (
    CarryCostType,
    OrderSide,
    PositionEffect,
    PositionSide,
)
_RUN_RESULT_DATACLASS_TYPES = {
    f"{item.__module__}.{item.__qualname__}": item
    for item in _RUN_RESULT_DATACLASSES
}
_RUN_RESULT_ENUM_TYPES = {
    f"{item.__module__}.{item.__qualname__}": item
    for item in _RUN_RESULT_ENUMS
}


def _batch_from_canonical_json(value: object) -> DecisionBatch:
    try:
        decoded = decode_canonical_graph(
            value,
            dataclass_types=_RUN_RESULT_DATACLASS_TYPES,
            enum_types=_RUN_RESULT_ENUM_TYPES,
        )
    except CanonicalGraphError as exc:
        raise LedgerSchemaError("invalid canonical run result") from exc
    if type(decoded) is not DecisionBatch:
        raise LedgerSchemaError("canonical run result must be DecisionBatch")
    return decoded


def _validate_account(account: AccountSnapshot) -> None:
    if account.positions:
        prices = {
            item.symbol: (
                item.current_price if item.current_price is not None else item.average_cost
            )
            for item in account.positions
        }
        value_account(account, prices)
    if account.reserved_cash > max(0.0, account.available_cash) + 1e-9:
        raise LedgerError("reserved_cash exceeds available_cash")


def validate_ledger_payload(payload: object) -> dict[str, Any]:
    """Validate a decoded v2 payload without reading or mutating a store."""

    if not isinstance(payload, dict):
        raise LedgerSchemaError("ledger root must be an object")
    _require_keys(payload, frozenset({"version", "accounts"}), "ledger root")
    if payload.get("version") != LEDGER_SCHEMA_VERSION:
        raise LedgerSchemaError(
            f"ledger schema version must be {LEDGER_SCHEMA_VERSION}; migration required"
        )
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        raise LedgerSchemaError("ledger accounts must be an object")
    for strategy_id, raw_account in accounts.items():
        if type(strategy_id) is not str or not strategy_id:
            raise LedgerSchemaError("account strategy ID must be a non-empty string")
        item = _require_mapping(raw_account, f"accounts[{strategy_id}]")
        _require_keys(item, _ACCOUNT_FIELDS, f"accounts[{strategy_id}]")
        decoded = decode_account_snapshot(item)
        if decoded.strategy_id != strategy_id:
            raise LedgerSchemaError("account key does not match strategy_id")
        for key in (
            "open_intents",
            "fills",
            "execution_progress",
            "risk_facts",
            "revision_transitions",
            "events",
            "committed_batches",
            "run_results",
        ):
            _require_list(item[key], f"account.{key}")
        intents = [_intent_from_json(raw) for raw in item["open_intents"]]
        if len({intent.id for intent in intents}) != len(intents):
            raise LedgerSchemaError("account.open_intents contains duplicate IDs")
        persisted_fills = [_fill_from_json(raw) for raw in item["fills"]]
        fill_ids = [progress_fill_id for progress_fill_id, _ in persisted_fills]
        if len(set(fill_ids)) != len(fill_ids):
            raise LedgerSchemaError("account.fills contains duplicate progress fill IDs")
        fills = [fill for _, fill in persisted_fills]
        fill_keys = [
            (
                progress_fill_id,
                fill.intent_id,
                fill.symbol,
                fill.quantity,
                fill.price,
                fill.fees,
                fill.status,
            )
            for progress_fill_id, fill in persisted_fills
        ]
        if len(set(fill_keys)) != len(fill_keys):
            raise LedgerSchemaError("account.fills contains duplicate rows")
        progress = [
            _progress_from_json(raw) for raw in item["execution_progress"]
        ]
        if len({entry.intent_id for entry in progress}) != len(progress):
            raise LedgerSchemaError("account.execution_progress contains duplicate intents")
        progress_details = {
            detail.id: detail
            for entry in progress
            for detail in entry.fills
        }
        if sum(len(entry.fills) for entry in progress) != len(progress_details):
            raise LedgerSchemaError(
                "account.execution_progress contains duplicate progress fill IDs"
            )
        if set(fill_ids) != set(progress_details):
            raise LedgerSchemaError(
                "account fills and execution progress must be globally one-to-one"
            )
        for progress_fill_id, fill in persisted_fills:
            if _fill_summary_key(fill) != _progress_fill_summary_key(
                progress_details[progress_fill_id]
            ):
                raise LedgerSchemaError(
                    "persisted fill does not match its execution progress fill"
                )
        events = [_event_from_json(raw) for raw in item["events"]]
        if len({event.id for event in events}) != len(events):
            raise LedgerSchemaError("account.events contains duplicate IDs")
        for event in events:
            try:
                _validate_event(event)
            except LedgerError as exc:
                raise LedgerSchemaError(f"invalid persisted event: {exc}") from exc
        revision_transitions = [
            _revision_transition_fact_from_json(raw)
            for raw in item["revision_transitions"]
        ]
        transition_ids = [fact.transition_id for fact in revision_transitions]
        if len(set(transition_ids)) != len(transition_ids):
            raise LedgerSchemaError(
                "account.revision_transitions contains duplicate IDs"
            )
        for previous, current in zip(
            revision_transitions,
            revision_transitions[1:],
        ):
            if current.from_revision != previous.to_revision:
                raise LedgerSchemaError(
                    "revision transition facts must form one revision chain"
                )
        if revision_transitions:
            if revision_transitions[-1].to_revision != decoded.strategy_revision:
                raise LedgerSchemaError(
                    "account revision does not match transition fact chain"
                )
            if any(
                fact.strategy_id != strategy_id for fact in revision_transitions
            ):
                raise LedgerSchemaError(
                    "revision transition strategy differs from account"
                )
        transition_events = [
            event for event in events if event.type == "REVISION_TRANSITIONED"
        ]
        if len(transition_events) != len(revision_transitions) or any(
            event != _derived_revision_transition_event(fact)
            for event, fact in zip(
                transition_events,
                revision_transitions,
                strict=True,
            )
        ):
            raise LedgerSchemaError(
                "persisted revision transition events and facts must be one-to-one"
            )
        risk_facts = [_risk_fact_from_json(raw) for raw in item["risk_facts"]]
        risk_fact_ids = [fact.fact_id for fact in risk_facts]
        if len(set(risk_fact_ids)) != len(risk_fact_ids):
            raise LedgerSchemaError("account.risk_facts contains duplicate IDs")
        risk_facts_by_id = {fact.fact_id: fact for fact in risk_facts}
        fill_events = {
            str(event.data["progress_fill_id"]): event
            for event in events
            if event.type in {"ORDER_FILLED", "ORDER_PARTIAL"}
        }
        if len(fill_events) != sum(
            event.type in {"ORDER_FILLED", "ORDER_PARTIAL"} for event in events
        ):
            raise LedgerSchemaError(
                "persisted fill events contain duplicate progress fill IDs"
            )
        if set(fill_events) != set(progress_details):
            raise LedgerSchemaError(
                "persisted fill events and execution progress must be one-to-one"
            )
        for progress_fill_id, detail in progress_details.items():
            if fill_events[progress_fill_id] != _derived_fill_event(detail):
                raise LedgerSchemaError(
                    "persisted fill event does not exactly match execution progress"
                )
        risk_event_fact_ids = [
            str(event.data["risk_fact_id"])
            for event in events
            if event.type == "RISK_CHANGED"
        ]
        if risk_event_fact_ids != risk_fact_ids:
            raise LedgerSchemaError(
                "persisted risk events and canonical risk facts must preserve append order"
            )
        risk_events = {
            fact_id: event
            for fact_id, event in zip(
                risk_event_fact_ids,
                (event for event in events if event.type == "RISK_CHANGED"),
                strict=True,
            )
        }
        if len(risk_events) != len(risk_event_fact_ids):
            raise LedgerSchemaError(
                "persisted risk events contain duplicate canonical fact IDs"
            )
        if set(risk_events) != set(risk_facts_by_id):
            raise LedgerSchemaError(
                "persisted risk events and canonical risk facts must be one-to-one"
            )
        for fact_id, fact in risk_facts_by_id.items():
            if risk_events[fact_id] != _derived_risk_event(fact):
                raise LedgerSchemaError(
                    "persisted RISK_CHANGED event does not exactly match canonical risk fact"
                )
        try:
            _validate_carry_event_pairs(decoded.carry_accruals, events)
        except LedgerError as exc:
            raise LedgerSchemaError(
                f"persisted carry event reconciliation failed: {exc}"
            ) from exc
        committed = item["committed_batches"]
        run_keys: set[str] = set()
        committed_by_run_key: dict[str, Mapping[str, Any]] = {}
        referenced_risk_fact_ids: list[str] = []
        for index, raw_batch in enumerate(committed):
            batch = _require_mapping(raw_batch, f"committed_batches[{index}]")
            _require_keys(
                batch,
                frozenset(
                    {
                        "run_key",
                        "strategy_id",
                        "strategy_revision",
                        "source_snapshot_id",
                        "result_snapshot_id",
                        "market_snapshot_id",
                        "risk_fact_ids",
                        "fingerprint",
                    }
                ),
                f"committed_batches[{index}]",
            )
            run_key = batch.get("run_key")
            if type(run_key) is not str or not run_key:
                raise LedgerSchemaError("committed batch run_key must be non-empty")
            if run_key in run_keys:
                raise LedgerSchemaError("account.committed_batches contains duplicate run keys")
            if batch.get("strategy_id") != strategy_id:
                raise LedgerSchemaError("committed batch strategy_id differs from account")
            if type(batch["strategy_revision"]) is not int:
                raise LedgerSchemaError("committed batch strategy_revision must be an integer")
            for field_name in ("source_snapshot_id", "result_snapshot_id"):
                if type(batch[field_name]) is not str or not batch[field_name]:
                    raise LedgerSchemaError(
                        f"committed batch {field_name} must be non-empty"
                    )
            if batch["market_snapshot_id"] is not None and (
                type(batch["market_snapshot_id"]) is not str
                or not batch["market_snapshot_id"]
            ):
                raise LedgerSchemaError(
                    "committed batch market_snapshot_id must be non-empty or null"
                )
            if type(batch.get("fingerprint")) is not str or not batch["fingerprint"]:
                raise LedgerSchemaError("committed batch fingerprint must be non-empty")
            batch_risk_fact_ids = _require_list(
                batch["risk_fact_ids"],
                f"committed_batches[{index}].risk_fact_ids",
            )
            if any(
                type(fact_id) is not str or not fact_id
                for fact_id in batch_risk_fact_ids
            ):
                raise LedgerSchemaError(
                    "committed batch risk_fact_ids must contain non-empty strings"
                )
            if len(set(batch_risk_fact_ids)) != len(batch_risk_fact_ids):
                raise LedgerSchemaError(
                    "committed batch risk_fact_ids contains duplicate IDs"
                )
            for fact_id in batch_risk_fact_ids:
                fact = risk_facts_by_id.get(fact_id)
                if fact is None:
                    raise LedgerSchemaError(
                        "committed batch references a missing canonical risk fact"
                    )
                if (
                    fact.strategy_id != batch["strategy_id"]
                    or fact.strategy_revision != batch["strategy_revision"]
                    or fact.run_key != batch["run_key"]
                    or fact.portfolio_snapshot_id != batch["source_snapshot_id"]
                    or fact.market_snapshot_id != batch["market_snapshot_id"]
                ):
                    raise LedgerSchemaError(
                        "canonical risk fact batch identity does not match committed batch"
                    )
            referenced_risk_fact_ids.extend(batch_risk_fact_ids)
            run_keys.add(run_key)
            committed_by_run_key[run_key] = batch
        if referenced_risk_fact_ids != risk_fact_ids:
            raise LedgerSchemaError(
                "committed batches and canonical risk facts must be append-only one-to-one"
            )
        run_result_keys: set[str] = set()
        for index, raw_result in enumerate(item["run_results"]):
            result = _require_mapping(raw_result, f"run_results[{index}]")
            _require_keys(
                result,
                frozenset(
                    {
                        "strategy_id",
                        "run_key",
                        "request_fingerprint",
                        "batch_fingerprint",
                        "batch",
                    }
                ),
                f"run_results[{index}]",
            )
            run_key = result["run_key"]
            request_fingerprint = result["request_fingerprint"]
            batch_fingerprint = result["batch_fingerprint"]
            for field_name, value in (
                ("run_key", run_key),
                ("request_fingerprint", request_fingerprint),
                ("batch_fingerprint", batch_fingerprint),
            ):
                if type(value) is not str or not value:
                    raise LedgerSchemaError(
                        f"run result {field_name} must be non-empty"
                    )
            if result["strategy_id"] != strategy_id:
                raise LedgerSchemaError("run result strategy_id differs from account")
            if run_key in run_result_keys:
                raise LedgerSchemaError("account.run_results contains duplicate run keys")
            committed_batch = committed_by_run_key.get(run_key)
            if committed_batch is None:
                raise LedgerSchemaError("run result references an uncommitted run")
            decoded_batch = _batch_from_canonical_json(result["batch"])
            if canonical_graph(decoded_batch) != result["batch"]:
                raise LedgerSchemaError("run result batch graph is not canonical")
            if (
                decoded_batch.strategy_id != strategy_id
                or decoded_batch.run_key != run_key
                or decoded_batch.request_fingerprint != request_fingerprint
                or decoded_batch.strategy_revision
                != committed_batch["strategy_revision"]
                or decoded_batch.portfolio_snapshot_id
                != committed_batch["source_snapshot_id"]
                or decoded_batch.market_snapshot_id
                != committed_batch["market_snapshot_id"]
            ):
                raise LedgerSchemaError(
                    "canonical run result identity differs from committed batch"
                )
            canonical_facts = _canonical_batch_facts(decoded_batch)
            expected_batch_fingerprint = _batch_fingerprint(
                decoded_batch,
                canonical_facts,
            )
            if (
                batch_fingerprint != expected_batch_fingerprint
                or committed_batch["fingerprint"] != expected_batch_fingerprint
            ):
                raise LedgerSchemaError(
                    "canonical run result fingerprint differs from committed batch"
                )
            run_result_keys.add(run_key)
    return payload


def _read_store(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": LEDGER_SCHEMA_VERSION, "accounts": {}}
    except OSError as exc:
        raise LedgerSchemaError(f"cannot read ledger: {exc}") from exc
    try:
        payload = json.loads(
            raw,
            parse_constant=lambda token: (_ for _ in ()).throw(
                LedgerSchemaError(f"nonstandard JSON number: {token}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise LedgerSchemaError(f"ledger JSON cannot be parsed: {exc.msg}") from exc
    return validate_ledger_payload(payload)


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    atomic_replace_bytes(path, encoded)


def _stable_snapshot_id(batch: DecisionBatch) -> str:
    material = "|".join(
        (
            batch.strategy_id,
            batch.run_key,
            batch.portfolio_snapshot_id,
            batch.market_snapshot_id,
        )
    )
    return "snapshot-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _stable_risk_fact_id(
    *,
    strategy_id: str,
    strategy_revision: int,
    run_key: str,
    portfolio_snapshot_id: str,
    market_snapshot_id: str,
    update: PositionRiskUpdate,
) -> str:
    material = {
        "strategy_id": strategy_id,
        "strategy_revision": strategy_revision,
        "run_key": run_key,
        "portfolio_snapshot_id": portfolio_snapshot_id,
        "market_snapshot_id": market_snapshot_id,
        "update": _risk_update_to_json(update),
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return "risk-fact-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _risk_fact_for_batch(
    batch: DecisionBatch,
    update: PositionRiskUpdate,
    occurred_at: datetime,
) -> _RiskFact:
    return _RiskFact(
        fact_id=_stable_risk_fact_id(
            strategy_id=batch.strategy_id,
            strategy_revision=batch.strategy_revision,
            run_key=batch.run_key,
            portfolio_snapshot_id=batch.portfolio_snapshot_id,
            market_snapshot_id=batch.market_snapshot_id,
            update=update,
        ),
        strategy_id=batch.strategy_id,
        strategy_revision=batch.strategy_revision,
        run_key=batch.run_key,
        portfolio_snapshot_id=batch.portfolio_snapshot_id,
        market_snapshot_id=batch.market_snapshot_id,
        occurred_at=occurred_at,
        update=update,
    )


def _unique_typed(
    explicit: Iterable[Any],
    stage_outputs: Iterable[Any],
    *,
    fact_kinds: frozenset[str],
    expected_type: type[Any],
    identity: Any,
) -> tuple[Any, ...]:
    resolved = list(explicit)
    for output in stage_outputs:
        for fact in output.facts:
            if isinstance(fact, Mapping) and fact.get("kind") in fact_kinds:
                items = fact.get("items", ())
                if isinstance(items, (str, bytes, Mapping)):
                    raise LedgerError(f"{fact.get('kind')} items must be iterable")
                resolved.extend(items)
    ordered: list[Any] = []
    seen: dict[Any, Any] = {}
    for item in resolved:
        if type(item) is not expected_type:
            raise LedgerError(
                f"canonical item must be {expected_type.__name__}, got {type(item).__name__}"
            )
        key = identity(item)
        previous = seen.get(key)
        if previous is not None and previous != item:
            raise LedgerError(f"conflicting duplicate canonical item: {key}")
        if previous is None:
            seen[key] = item
            ordered.append(item)
    return tuple(ordered)


def _canonical_batch_facts(batch: DecisionBatch) -> tuple[
    tuple[OrderIntent, ...],
    tuple[ExecutionFill, ...],
    tuple[OrderExecutionProgress, ...],
    tuple[PositionRiskUpdate, ...],
    tuple[PositionSettlementUpdate, ...],
    tuple[CarryAccrualRecord, ...],
]:
    intents = _unique_typed(
        batch.intents,
        batch.stage_outputs,
        fact_kinds=frozenset({"order_intents", "risk_intents"}),
        expected_type=OrderIntent,
        identity=lambda item: item.id,
    )
    fills = _unique_typed(
        batch.fills,
        batch.stage_outputs,
        fact_kinds=frozenset({"execution_fills"}),
        expected_type=ExecutionFill,
        identity=lambda item: (
            item.intent_id,
            item.symbol,
            item.quantity,
            item.price,
            item.fees,
            item.status,
        ),
    )
    progress = _unique_typed(
        batch.execution_progress,
        batch.stage_outputs,
        fact_kinds=frozenset({"execution_progress"}),
        expected_type=OrderExecutionProgress,
        identity=lambda item: item.intent_id,
    )
    updates = _unique_typed(
        batch.position_risk_updates,
        batch.stage_outputs,
        fact_kinds=frozenset({"position_risk_updates"}),
        expected_type=PositionRiskUpdate,
        identity=lambda item: item.symbol,
    )
    settlement_updates = _unique_typed(
        batch.position_settlement_updates,
        batch.stage_outputs,
        fact_kinds=frozenset({"position_settlement_updates"}),
        expected_type=PositionSettlementUpdate,
        identity=lambda item: item.symbol,
    )
    accruals = _unique_typed(
        batch.carry_accruals,
        batch.stage_outputs,
        fact_kinds=frozenset({"carry_accruals"}),
        expected_type=CarryAccrualRecord,
        identity=lambda item: item.idempotency_key,
    )
    return intents, fills, progress, updates, settlement_updates, accruals


def _execution_account_fact(batch: DecisionBatch) -> AccountSnapshot | None:
    accounts: list[AccountSnapshot] = []
    for output in batch.stage_outputs:
        for fact in output.facts:
            if isinstance(fact, Mapping) and fact.get("kind") == "execution_account":
                account = fact.get("account")
                if type(account) is not AccountSnapshot:
                    raise LedgerError("execution_account fact must contain AccountSnapshot")
                accounts.append(account)
    if len(accounts) > 1:
        raise LedgerError("duplicate execution_account canonical fact")
    return None if not accounts else accounts[0]


def _verify_execution_account(
    replayed: AccountSnapshot,
    canonical: AccountSnapshot | None,
) -> AccountSnapshot:
    if canonical is None:
        return replayed
    if canonical != replayed:
        raise LedgerError("execution_account does not match replayed execution progress")
    return replayed


def _validate_fill_summaries(
    fills: Iterable[ExecutionFill],
    progress: Iterable[OrderExecutionProgress],
    existing_progress: Mapping[str, OrderExecutionProgress],
) -> tuple[tuple[ExecutionProgressFill, ExecutionFill], ...]:
    new_details: list[ExecutionProgressFill] = []
    for item in progress:
        prior = existing_progress.get(item.intent_id)
        prior_count = 0 if prior is None else len(prior.fills)
        new_details.extend(item.fills[prior_count:])
    unmatched = list(new_details)
    paired: list[tuple[ExecutionProgressFill, ExecutionFill]] = []
    for fill in fills:
        match = next(
            (
                detail
                for detail in unmatched
                if _progress_fill_summary_key(detail) == _fill_summary_key(fill)
            ),
            None,
        )
        if match is None:
            raise LedgerError("execution fill summary does not match new progress")
        unmatched.remove(match)
        paired.append((match, fill))
    if unmatched:
        raise LedgerError("new execution progress is missing an execution fill summary")
    return tuple(paired)


def _fill_summary_key(fill: ExecutionFill) -> tuple[object, ...]:
    return (
        fill.intent_id,
        fill.symbol,
        fill.quantity,
        fill.price,
        fill.fees,
        fill.status,
    )


def _progress_fill_summary_key(fill: ExecutionProgressFill) -> tuple[object, ...]:
    return (
        fill.intent_id,
        fill.symbol,
        fill.quantity,
        fill.price,
        fill.fees,
        fill.status,
    )


def _batch_fingerprint(
    batch: DecisionBatch,
    facts: tuple[
        tuple[OrderIntent, ...],
        tuple[ExecutionFill, ...],
        tuple[OrderExecutionProgress, ...],
        tuple[PositionRiskUpdate, ...],
        tuple[PositionSettlementUpdate, ...],
        tuple[CarryAccrualRecord, ...],
    ],
) -> str:
    intents, fills, progress, updates, settlement_updates, accruals = facts
    material = {
        "batch": _fingerprint_plain(batch),
        "canonical_facts": {
            "intents": [_fingerprint_plain(item) for item in intents],
            "fills": [_fingerprint_plain(item) for item in fills],
            "execution_progress": [_fingerprint_plain(item) for item in progress],
            "position_risk_updates": [
                {
                    "fact_id": _stable_risk_fact_id(
                        strategy_id=batch.strategy_id,
                        strategy_revision=batch.strategy_revision,
                        run_key=batch.run_key,
                        portfolio_snapshot_id=batch.portfolio_snapshot_id,
                        market_snapshot_id=batch.market_snapshot_id,
                        update=item,
                    ),
                    "update": _fingerprint_plain(item),
                }
                for item in updates
            ],
            "position_settlement_updates": [
                _fingerprint_plain(item) for item in settlement_updates
            ],
            "carry_accruals": [_fingerprint_plain(item) for item in accruals],
            "execution_account": _fingerprint_plain(_execution_account_fact(batch)),
        },
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _charge_fee(account: AccountSnapshot, amount: float) -> AccountSnapshot:
    cash = account.available_cash
    loan = account.margin_loan
    used = min(max(0.0, cash), amount)
    return replace(account, available_cash=cash - used, margin_loan=loan + amount - used)


def _touch_position_after_fill(
    account: AccountSnapshot,
    intent: OrderIntent,
    fill: ExecutionProgressFill,
) -> AccountSnapshot:
    positions: list[PositionSnapshot] = []
    for held in account.positions:
        if held.symbol != intent.symbol:
            positions.append(held)
            continue
        lifecycle = held.borrow_lifecycle
        if held.side is PositionSide.SHORT and lifecycle is None:
            lifecycle = AccrualLifecycle(
                id="borrow-" + hashlib.sha256(
                    f"borrow|{account.id}|{intent.id}|{fill.snapshot_id}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:24],
                started_on=fill.occurred_at.date(),
            )
        positions.append(
            replace(
                held,
                current_price=fill.price,
                borrow_lifecycle=lifecycle,
            )
        )
    financing = account.financing_lifecycle
    if account.margin_loan <= 0:
        financing = None
    elif financing is None:
        financing = AccrualLifecycle(
            id="financing-" + hashlib.sha256(
                f"financing|{account.id}|{intent.id}|{fill.snapshot_id}".encode(
                    "utf-8"
                )
            ).hexdigest()[:24],
            started_on=fill.occurred_at.date(),
        )
    return replace(
        account,
        positions=tuple(positions),
        financing_lifecycle=financing,
        occurred_at=max(account.occurred_at, fill.occurred_at),
    )


def _apply_progress(
    account: AccountSnapshot,
    intents_by_id: Mapping[str, OrderIntent],
    existing_progress: Mapping[str, OrderExecutionProgress],
    progress: Iterable[OrderExecutionProgress],
) -> tuple[AccountSnapshot, dict[str, OrderExecutionProgress]]:
    current = account
    merged = dict(existing_progress)
    for item in progress:
        intent = intents_by_id.get(item.intent_id)
        if intent is None:
            raise LedgerError(f"execution progress references unknown intent: {item.intent_id}")
        if (
            item.symbol != intent.symbol
            or item.position_side is not intent.position_side
            or item.order_side is not intent.order_side
            or item.intent_quantity != intent.quantity
        ):
            raise LedgerError("execution progress does not match its intent")
        previous = merged.get(item.intent_id)
        previous_fills = () if previous is None else previous.fills
        if previous is not None:
            if (
                previous.symbol != item.symbol
                or previous.position_side is not item.position_side
                or previous.order_side is not item.order_side
                or previous.intent_quantity != item.intent_quantity
                or previous.execution_policy_fingerprint
                != item.execution_policy_fingerprint
                or item.fills[: len(previous_fills)] != previous_fills
            ):
                raise LedgerError("execution progress history is not append-only")
        for progress_fill in item.fills[len(previous_fills) :]:
            held = next(
                (position for position in current.positions if position.symbol == intent.symbol),
                None,
            )
            effect = intent.position_effect
            if effect is PositionEffect.OPEN and held is not None:
                effect = PositionEffect.INCREASE
            if effect is PositionEffect.CLOSE and held is not None and progress_fill.quantity < held.quantity:
                effect = PositionEffect.REDUCE
            applied = replace(intent, quantity=progress_fill.quantity, position_effect=effect)
            current = project_account_for_intent(
                current, applied, {intent.symbol: progress_fill.price}
            )
            current = _charge_fee(current, progress_fill.fees)
            current = _touch_position_after_fill(current, intent, progress_fill)
        merged[item.intent_id] = item
    current = replace(
        current,
        positions=tuple(sorted(current.positions, key=lambda item: item.symbol)),
    )
    return current, merged


def _apply_risk_updates(
    account: AccountSnapshot, updates: Iterable[PositionRiskUpdate]
) -> AccountSnapshot:
    by_symbol = {item.symbol: item for item in updates}
    missing = set(by_symbol) - {item.symbol for item in account.positions}
    if missing:
        raise LedgerError("risk update references missing position: " + ", ".join(sorted(missing)))
    positions = []
    for held in account.positions:
        update = by_symbol.get(held.symbol)
        if update is None:
            positions.append(held)
        else:
            if update.side is not held.side:
                raise LedgerError("risk update side differs from held position")
            positions.append(
                replace(
                    held,
                    peak_price=update.peak_price,
                    trough_price=update.trough_price,
                    trailing_active=update.trailing_active,
                    position_mode=update.position_mode,
                )
            )
    return replace(account, positions=tuple(positions))


def _apply_settlement_updates(
    account: AccountSnapshot,
    updates: Iterable[PositionSettlementUpdate],
) -> AccountSnapshot:
    by_symbol = {item.symbol: item for item in updates}
    missing = set(by_symbol) - {item.symbol for item in account.positions}
    if missing:
        raise LedgerError(
            "settlement update references missing position: "
            + ", ".join(sorted(missing))
        )
    positions: list[PositionSnapshot] = []
    for held in account.positions:
        update = by_symbol.get(held.symbol)
        if update is None:
            positions.append(held)
            continue
        if update.side is not held.side or update.quantity != held.quantity:
            raise LedgerError(
                "settlement update identity differs from replayed position"
            )
        positions.append(
            replace(
                held,
                sellable_quantity=update.sellable_quantity,
                sellable_on=update.sellable_on,
            )
        )
    return replace(account, positions=tuple(positions))


def _apply_carry(
    account: AccountSnapshot, accruals: Iterable[CarryAccrualRecord]
) -> AccountSnapshot:
    existing = {item.idempotency_key for item in account.carry_accruals}
    new_records = []
    financing = 0.0
    borrow = 0.0
    for record in accruals:
        if record.account_id != account.id:
            raise LedgerError("carry accrual account_id differs from account")
        if record.idempotency_key in existing:
            continue
        existing.add(record.idempotency_key)
        new_records.append(record)
        if record.cost_type is CarryCostType.FINANCING:
            financing += record.amount
        else:
            borrow += record.amount
    total = financing + borrow
    if not math.isfinite(total):
        raise LedgerError("carry cost total is not finite")
    return replace(
        account,
        available_cash=account.available_cash - total,
        accrued_financing_cost=account.accrued_financing_cost + financing,
        accrued_borrow_cost=account.accrued_borrow_cost + borrow,
        carry_accruals=(*account.carry_accruals, *new_records),
    )


def _validate_carry_event_pairs(
    accruals: Iterable[CarryAccrualRecord],
    events: Iterable[PortfolioEvent],
) -> None:
    records = tuple(accruals)
    carry_events = tuple(
        event
        for event in events
        if event.type in {"FINANCING_COST_ACCRUED", "BORROW_COST_ACCRUED"}
    )
    unmatched = list(carry_events)
    for record in records:
        expected_type = f"{record.cost_type.value}_COST_ACCRUED"
        expected_data = _carry_event_data(record)
        match = next(
            (
                event
                for event in unmatched
                if event.type == expected_type and dict(event.data) == expected_data
            ),
            None,
        )
        if match is None:
            raise LedgerError("carry accrual requires one matching carry event")
        unmatched.remove(match)
    if unmatched:
        raise LedgerError("carry event requires one matching carry accrual")


def _carry_event_data(record: CarryAccrualRecord) -> dict[str, Any]:
    return {
        "account_id": record.account_id,
        "cost_type": record.cost_type.value,
        "lifecycle_id": record.lifecycle_id,
        "symbol": record.symbol,
        "accrual_date": record.accrual_date.isoformat(),
        "elapsed_days": record.elapsed_days,
        "amount": record.amount,
    }


def _require_event_fields(event: PortfolioEvent, expected: frozenset[str]) -> None:
    if set(event.data) != expected:
        raise LedgerError(
            f"{event.type} data fields must be exactly {sorted(expected)}"
        )


def _require_event_string(event: PortfolioEvent, field_name: str) -> None:
    value = event.data[field_name]
    if type(value) is not str or not value:
        raise LedgerError(f"{event.type} {field_name} must be a non-empty string")


def _require_event_number(event: PortfolioEvent, field_name: str) -> None:
    value = event.data[field_name]
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        raise LedgerError(f"{event.type} {field_name} must be finite")


def _validate_fill_event(event: PortfolioEvent) -> None:
    _require_event_fields(
        event,
        frozenset(
            {
                "progress_fill_id",
                "intent_id",
                "symbol",
                "position_side",
                "order_side",
                "snapshot_id",
                "quantity",
                "price",
                "fees",
                "commission",
                "status",
            }
        ),
    )
    for field_name in (
        "progress_fill_id",
        "intent_id",
        "symbol",
        "snapshot_id",
    ):
        _require_event_string(event, field_name)
    if event.data["position_side"] not in {item.value for item in PositionSide}:
        raise LedgerError(f"{event.type} position_side is invalid")
    if event.data["order_side"] not in {item.value for item in OrderSide}:
        raise LedgerError(f"{event.type} order_side is invalid")
    if type(event.data["quantity"]) is not int or event.data["quantity"] <= 0:
        raise LedgerError(f"{event.type} quantity must be a positive integer")
    for field_name in ("price", "fees", "commission"):
        _require_event_number(event, field_name)
    expected_status = "FILLED" if event.type == "ORDER_FILLED" else "PARTIAL"
    if event.data["status"] != expected_status:
        raise LedgerError(f"{event.type} status must be {expected_status}")


def _validate_risk_event(event: PortfolioEvent) -> None:
    _require_event_fields(
        event,
        frozenset(
            {
                "risk_fact_id",
                "strategy_id",
                "strategy_revision",
                "run_key",
                "portfolio_snapshot_id",
                "market_snapshot_id",
                "symbol",
                "side",
                "peak_price",
                "trough_price",
                "trailing_active",
                "position_mode",
            }
        ),
    )
    for field_name in (
        "risk_fact_id",
        "strategy_id",
        "run_key",
        "portfolio_snapshot_id",
        "market_snapshot_id",
        "symbol",
    ):
        _require_event_string(event, field_name)
    if type(event.data["strategy_revision"]) is not int:
        raise LedgerError("RISK_CHANGED strategy_revision must be an integer")
    if event.data["side"] not in {item.value for item in PositionSide}:
        raise LedgerError("RISK_CHANGED side is invalid")
    for field_name in ("peak_price", "trough_price"):
        if event.data[field_name] is not None:
            _require_event_number(event, field_name)
    if type(event.data["trailing_active"]) is not bool:
        raise LedgerError("RISK_CHANGED trailing_active must be a bool")
    if event.data["position_mode"] not in {"NORMAL", "COVER_ONLY"}:
        raise LedgerError("RISK_CHANGED position_mode is invalid")


def _validate_carry_event(event: PortfolioEvent) -> None:
    _require_event_fields(
        event,
        frozenset(
            {
                "account_id",
                "cost_type",
                "lifecycle_id",
                "symbol",
                "accrual_date",
                "elapsed_days",
                "amount",
            }
        ),
    )
    for field_name in ("account_id", "lifecycle_id", "accrual_date"):
        _require_event_string(event, field_name)
    expected_cost_type = event.type.removesuffix("_COST_ACCRUED")
    if event.data["cost_type"] != expected_cost_type:
        raise LedgerError(f"{event.type} cost_type does not match event type")
    if expected_cost_type == CarryCostType.BORROW.value:
        _require_event_string(event, "symbol")
    elif event.data["symbol"] is not None:
        raise LedgerError("FINANCING_COST_ACCRUED symbol must be null")
    if type(event.data["elapsed_days"]) is not int or event.data["elapsed_days"] < 0:
        raise LedgerError(f"{event.type} elapsed_days must be nonnegative")
    _require_event_number(event, "amount")


def _validate_event(event: PortfolioEvent) -> None:
    _json_plain(event.data)
    if event.type == "CASH_ADJUSTED":
        _require_event_fields(event, frozenset({"amount"}))
        _require_event_number(event, "amount")
        return
    if event.type == "ACCOUNT_OPENED":
        _require_event_fields(
            event,
            frozenset(
                {
                    "account_id",
                    "strategy_id",
                    "strategy_revision",
                    "portfolio_snapshot_id",
                    "available_cash",
                }
            ),
        )
        for field_name in ("account_id", "strategy_id", "portfolio_snapshot_id"):
            _require_event_string(event, field_name)
        if type(event.data["strategy_revision"]) is not int:
            raise LedgerError("ACCOUNT_OPENED strategy_revision must be an integer")
        _require_event_number(event, "available_cash")
        return
    if event.type == "REVISION_TRANSITIONED":
        _require_event_fields(
            event,
            frozenset(
                {
                    "transition_id",
                    "strategy_id",
                    "from_revision",
                    "to_revision",
                    "source_snapshot_id",
                    "result_snapshot_id",
                    "cancelled_intent_ids",
                }
            ),
        )
        for field_name in (
            "transition_id",
            "strategy_id",
            "source_snapshot_id",
            "result_snapshot_id",
        ):
            _require_event_string(event, field_name)
        for field_name in ("from_revision", "to_revision"):
            if type(event.data[field_name]) is not int:
                raise LedgerError(
                    f"REVISION_TRANSITIONED {field_name} must be an integer"
                )
        cancelled = event.data["cancelled_intent_ids"]
        if type(cancelled) not in {tuple, list} or any(
            type(item) is not str or not item for item in cancelled
        ):
            raise LedgerError(
                "REVISION_TRANSITIONED cancelled_intent_ids must be strings"
            )
        return
    if event.type in {"ORDER_FILLED", "ORDER_PARTIAL"}:
        _validate_fill_event(event)
        return
    if event.type == "RISK_CHANGED":
        _validate_risk_event(event)
        return
    if event.type in {"FINANCING_COST_ACCRUED", "BORROW_COST_ACCRUED"}:
        _validate_carry_event(event)
        return
    if event.type in {"ORDER_CANCELLED", "ORDER_EXPIRED"}:
        _require_event_fields(
            event,
            frozenset({"intent_id", "symbol", "reason", "snapshot_id"}),
        )
        for field_name in ("intent_id", "symbol", "reason", "snapshot_id"):
            _require_event_string(event, field_name)
        return
    if event.type == "PIPELINE_COMPLETED":
        _require_event_fields(
            event,
            frozenset({"run_key", "market_snapshot_id"}),
        )
        _require_event_string(event, "run_key")
        _require_event_string(event, "market_snapshot_id")
        return
    raise UnknownPortfolioEventError(f"unknown portfolio event type: {event.type}")


def _derived_fill_event(fill: ExecutionProgressFill) -> PortfolioEvent:
    return PortfolioEvent(
        id=f"fill-{fill.id}",
        type="ORDER_FILLED" if fill.status == "FILLED" else "ORDER_PARTIAL",
        occurred_at=fill.occurred_at,
        data={
            "progress_fill_id": fill.id,
            "intent_id": fill.intent_id,
            "symbol": fill.symbol,
            "position_side": fill.position_side.value,
            "order_side": fill.order_side.value,
            "snapshot_id": fill.snapshot_id,
            "quantity": fill.quantity,
            "price": fill.price,
            "fees": fill.fees,
            "commission": fill.commission,
            "status": fill.status,
        },
    )


def _derived_risk_event(fact: _RiskFact) -> PortfolioEvent:
    return PortfolioEvent(
        id="risk-change-" + fact.fact_id.removeprefix("risk-fact-"),
        type="RISK_CHANGED",
        occurred_at=fact.occurred_at,
        data={
            "risk_fact_id": fact.fact_id,
            "strategy_id": fact.strategy_id,
            "strategy_revision": fact.strategy_revision,
            "run_key": fact.run_key,
            "portfolio_snapshot_id": fact.portfolio_snapshot_id,
            "market_snapshot_id": fact.market_snapshot_id,
            "symbol": fact.update.symbol,
            "side": fact.update.side.value,
            "peak_price": fact.update.peak_price,
            "trough_price": fact.update.trough_price,
            "trailing_active": fact.update.trailing_active,
            "position_mode": fact.update.position_mode,
        },
    )


def _derived_revision_transition_event(
    fact: _RevisionTransitionFact,
) -> PortfolioEvent:
    return PortfolioEvent(
        id="revision-transition-event-"
        + hashlib.sha256(fact.transition_id.encode("utf-8")).hexdigest()[:24],
        type="REVISION_TRANSITIONED",
        occurred_at=fact.occurred_at,
        data={
            "transition_id": fact.transition_id,
            "strategy_id": fact.strategy_id,
            "from_revision": fact.from_revision,
            "to_revision": fact.to_revision,
            "source_snapshot_id": fact.source_snapshot_id,
            "result_snapshot_id": fact.result_snapshot_id,
            "cancelled_intent_ids": fact.cancelled_intent_ids,
        },
    )


def _reconcile_batch_events(
    batch: DecisionBatch,
    paired_fills: Iterable[tuple[ExecutionProgressFill, ExecutionFill]],
    risk_facts: Iterable[_RiskFact],
) -> tuple[PortfolioEvent, ...]:
    submitted = tuple(batch.events)
    submitted_ids = [event.id for event in submitted]
    if len(submitted_ids) != len(set(submitted_ids)):
        raise LedgerError("duplicate event ID inside decision batch")
    for event in submitted:
        _validate_event(event)

    derived = (
        *(_derived_fill_event(detail) for detail, _summary in paired_fills),
        *(_derived_risk_event(fact) for fact in risk_facts),
    )
    derived_by_id = {event.id: event for event in derived}
    fact_bound_types = {"ORDER_FILLED", "ORDER_PARTIAL", "RISK_CHANGED"}
    for event in submitted:
        if event.type in {"ORDER_CANCELLED", "ORDER_EXPIRED"}:
            raise LedgerError(
                f"{event.type} requires a typed canonical cancellation fact"
            )
        if event.type == "ACCOUNT_OPENED":
            raise LedgerError("ACCOUNT_OPENED is only valid during account creation")
        if event.type in fact_bound_types and derived_by_id.get(event.id) != event:
            raise LedgerError(
                f"{event.type} event does not match one canonical fact"
            )

    submitted_by_id = {event.id: event for event in submitted}
    for event in derived:
        collision = submitted_by_id.get(event.id)
        if collision is not None and collision != event:
            raise LedgerError(f"conflicting derived event ID: {event.id}")
    return (
        *submitted,
        *(event for event in derived if event.id not in submitted_by_id),
    )


def _apply_events(
    account: AccountSnapshot,
    existing_events: Iterable[PortfolioEvent],
    events: Iterable[PortfolioEvent],
) -> tuple[AccountSnapshot, tuple[PortfolioEvent, ...]]:
    current = account
    ordered = list(existing_events)
    by_id = {event.id: event for event in ordered}
    for event in events:
        _validate_event(event)
        previous = by_id.get(event.id)
        if previous is not None:
            if previous != event:
                raise LedgerError(f"conflicting event ID: {event.id}")
            continue
        if event.type == "CASH_ADJUSTED":
            current = replace(
                current,
                available_cash=current.available_cash + float(event.data["amount"]),
            )
        current = replace(current, occurred_at=max(current.occurred_at, event.occurred_at))
        by_id[event.id] = event
        ordered.append(event)
    return current, tuple(ordered)


class JsonLedgerStore:
    """Process-safe schema-v2 JSON implementation of ``LedgerStore``."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def create_account(self, account: AccountSnapshot) -> AccountSnapshot:
        """Create one fully explicit account, idempotently and under the store lock."""

        if type(account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        if type(account.snapshot_id) is not str or not account.snapshot_id:
            raise LedgerError("account creation requires an explicit snapshot_id")
        _validate_account(account)
        opened = PortfolioEvent(
            id="account-opened-"
            + hashlib.sha256(
                (
                    f"{account.id}|{account.strategy_id}|"
                    f"{account.strategy_revision}|{account.snapshot_id}"
                ).encode("utf-8")
            ).hexdigest()[:24],
            type="ACCOUNT_OPENED",
            occurred_at=account.occurred_at,
            data={
                "account_id": account.id,
                "strategy_id": account.strategy_id,
                "strategy_revision": account.strategy_revision,
                "portfolio_snapshot_id": account.snapshot_id,
                "available_cash": account.available_cash,
            },
        )
        expected = {
            **encode_account_snapshot(account),
            "open_intents": [],
            "fills": [],
            "execution_progress": [],
            "risk_facts": [],
            "revision_transitions": [],
            "events": [_event_to_json(opened)],
            "committed_batches": [],
            "run_results": [],
        }
        with _PROCESS_LOCK, transaction_guard((self.path,)):
            store = _read_store(self.path)
            existing = store["accounts"].get(account.strategy_id)
            if existing is not None:
                if existing != expected:
                    raise LedgerError(
                        "account creation collision with different persistent content"
                    )
                return decode_account_snapshot(existing)
            next_store = {
                "version": LEDGER_SCHEMA_VERSION,
                "accounts": dict(store["accounts"]),
            }
            next_store["accounts"][account.strategy_id] = expected
            validate_ledger_payload(next_store)
            _atomic_write(self.path, next_store)
            return account

    def transition_revision(
        self,
        transition: RevisionTransition,
    ) -> AccountSnapshot:
        """Atomically advance one account revision and cancel all old intents."""

        if type(transition) is not RevisionTransition:
            raise TypeError("transition must be RevisionTransition")
        with _PROCESS_LOCK, transaction_guard((self.path,)):
            store = _read_store(self.path)
            try:
                persisted = _require_mapping(
                    store["accounts"][transition.strategy_id],
                    "account",
                )
            except KeyError as exc:
                raise KeyError(
                    f"portfolio account not found: {transition.strategy_id}"
                ) from exc
            account = decode_account_snapshot(persisted)
            raw_facts = _require_list(
                persisted["revision_transitions"],
                "account.revision_transitions",
            )
            facts = tuple(
                _revision_transition_fact_from_json(raw) for raw in raw_facts
            )
            existing = next(
                (
                    fact
                    for fact in facts
                    if fact.transition_id == transition.id
                ),
                None,
            )
            if existing is not None:
                if not _transition_matches_request(existing, transition):
                    raise LedgerError(
                        "revision transition ID collision with different content"
                    )
                if account.strategy_revision != existing.to_revision:
                    raise LedgerError(
                        "revision transition is historical, not the current account state"
                    )
                return account
            if account.strategy_id != transition.strategy_id:
                raise LedgerError("revision transition strategy differs from account")
            if account.strategy_revision != transition.from_revision:
                raise StalePortfolioSnapshotError(
                    "revision transition source revision is stale"
                )
            if account.snapshot_id != transition.expected_snapshot_id:
                raise StalePortfolioSnapshotError(
                    "revision transition source snapshot is stale"
                )
            open_intents = tuple(
                _intent_from_json(raw)
                for raw in _require_list(
                    persisted["open_intents"],
                    "account.open_intents",
                )
            )
            cancelled_intent_ids = tuple(
                sorted(intent.id for intent in open_intents)
            )
            result_snapshot_id = _revision_transition_result_snapshot_id(
                transition,
                cancelled_intent_ids,
            )
            fact = _RevisionTransitionFact(
                transition_id=transition.id,
                strategy_id=transition.strategy_id,
                from_revision=transition.from_revision,
                to_revision=transition.to_revision,
                source_snapshot_id=transition.expected_snapshot_id,
                result_snapshot_id=result_snapshot_id,
                occurred_at=transition.occurred_at,
                cancelled_intent_ids=cancelled_intent_ids,
            )
            transitioned = replace(
                account,
                strategy_revision=transition.to_revision,
                occurred_at=max(account.occurred_at, transition.occurred_at),
                snapshot_id=result_snapshot_id,
            )
            existing_events = tuple(
                _event_from_json(raw)
                for raw in _require_list(persisted["events"], "account.events")
            )
            transitioned, events = _apply_events(
                transitioned,
                existing_events,
                (_derived_revision_transition_event(fact),),
            )
            raw_account = {
                **dict(persisted),
                **encode_account_snapshot(transitioned),
                "portfolio_snapshot_id": result_snapshot_id,
                "open_intents": [],
                "revision_transitions": [
                    *raw_facts,
                    _revision_transition_fact_to_json(fact),
                ],
                "events": [_event_to_json(event) for event in events],
            }
            next_store = {
                "version": LEDGER_SCHEMA_VERSION,
                "accounts": dict(store["accounts"]),
            }
            next_store["accounts"][transition.strategy_id] = raw_account
            validate_ledger_payload(next_store)
            _atomic_write(self.path, next_store)
            return transitioned

    def load(self, strategy_id: str) -> AccountSnapshot:
        if type(strategy_id) is not str or not strategy_id:
            raise ValueError("strategy_id must be a non-empty string")
        with _PROCESS_LOCK, transaction_guard((self.path,)):
            store = _read_store(self.path)
            try:
                payload = store["accounts"][strategy_id]
            except KeyError as exc:
                raise KeyError(f"portfolio account not found: {strategy_id}") from exc
            return decode_account_snapshot(payload)

    def load_view(self, strategy_id: str) -> PortfolioLedgerView:
        """Return the strict typed service read model under one store lock."""

        if type(strategy_id) is not str or not strategy_id:
            raise ValueError("strategy_id must be a non-empty string")
        with _PROCESS_LOCK, transaction_guard((self.path,)):
            store = _read_store(self.path)
            try:
                payload = _require_mapping(
                    store["accounts"][strategy_id],
                    "account",
                )
            except KeyError as exc:
                raise KeyError(f"portfolio account not found: {strategy_id}") from exc
            account = decode_account_snapshot(payload)
            intents = tuple(
                _intent_from_json(item)
                for item in _require_list(payload["open_intents"], "account.open_intents")
            )
            open_ids = {item.id for item in intents}
            progress = tuple(
                item
                for item in (
                    _progress_from_json(raw)
                    for raw in _require_list(
                        payload["execution_progress"],
                        "account.execution_progress",
                    )
                )
                if item.intent_id in open_ids
            )
            events = tuple(
                _event_from_json(item)
                for item in _require_list(payload["events"], "account.events")[-100:]
            )
            return PortfolioLedgerView(
                account=account,
                open_intents=tuple(sorted(intents, key=lambda item: item.id)),
                execution_progress=tuple(
                    sorted(progress, key=lambda item: item.intent_id)
                ),
                recent_events=events,
            )

    def list_accounts(self) -> tuple[AccountSnapshot, ...]:
        with _PROCESS_LOCK, transaction_guard((self.path,)):
            store = _read_store(self.path)
            return tuple(
                decode_account_snapshot(store["accounts"][strategy_id])
                for strategy_id in sorted(store["accounts"])
            )

    def load_committed_batch(
        self,
        strategy_id: str,
        run_key: str,
        request_fingerprint: str,
    ) -> DecisionBatch | None:
        """Return an exact prior result, or reject reuse of its run key."""

        for value, field_name in (
            (strategy_id, "strategy_id"),
            (run_key, "run_key"),
            (request_fingerprint, "request_fingerprint"),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{field_name} must be a non-empty string")
        with _PROCESS_LOCK, transaction_guard((self.path,)):
            store = _read_store(self.path)
            try:
                persisted = _require_mapping(
                    store["accounts"][strategy_id],
                    "account",
                )
            except KeyError as exc:
                raise KeyError(f"portfolio account not found: {strategy_id}") from exc
            committed = next(
                (
                    item
                    for item in _require_list(
                        persisted["committed_batches"],
                        "account.committed_batches",
                    )
                    if isinstance(item, Mapping) and item.get("run_key") == run_key
                ),
                None,
            )
            if committed is None:
                return None
            result = next(
                (
                    item
                    for item in _require_list(
                        persisted["run_results"],
                        "account.run_results",
                    )
                    if isinstance(item, Mapping) and item.get("run_key") == run_key
                ),
                None,
            )
            if result is None:
                raise LedgerError(
                    "run_key was committed without a replayable request result"
                )
            if result.get("request_fingerprint") != request_fingerprint:
                raise LedgerError(
                    "run_key was already committed with a different request"
                )
            return _batch_from_canonical_json(result["batch"])

    def commit(self, batch: DecisionBatch) -> AccountSnapshot:
        if type(batch) is not DecisionBatch:
            raise TypeError("batch must be DecisionBatch")
        with _PROCESS_LOCK, transaction_guard((self.path,)):
            store = _read_store(self.path)
            try:
                persisted = _require_mapping(
                    store["accounts"][batch.strategy_id], "account"
                )
            except KeyError as exc:
                raise KeyError(
                    f"portfolio account not found: {batch.strategy_id}"
                ) from exc
            account = decode_account_snapshot(persisted)
            committed = _require_list(
                persisted["committed_batches"], "account.committed_batches"
            )
            canonical_facts = _canonical_batch_facts(batch)
            fingerprint = _batch_fingerprint(batch, canonical_facts)
            existing_batch = next(
                (
                    item
                    for item in committed
                    if isinstance(item, Mapping) and item.get("run_key") == batch.run_key
                ),
                None,
            )
            if existing_batch is not None:
                if existing_batch.get("strategy_id") != batch.strategy_id:
                    raise LedgerError("committed batch strategy identity is corrupt")
                if existing_batch.get("fingerprint") != fingerprint:
                    raise LedgerError("run_key was already committed with different facts")
                return account
            expected_snapshot = persisted["portfolio_snapshot_id"]
            if batch.portfolio_snapshot_id != expected_snapshot:
                raise StalePortfolioSnapshotError(
                    f"stale portfolio snapshot: expected {expected_snapshot}, "
                    f"got {batch.portfolio_snapshot_id}"
                )
            if batch.strategy_revision != account.strategy_revision:
                raise LedgerError("batch strategy revision differs from account")

            (
                intents,
                fills,
                progress,
                updates,
                settlement_updates,
                accruals,
            ) = canonical_facts
            _validate_carry_event_pairs(accruals, batch.events)
            existing_intents = tuple(
                _intent_from_json(raw)
                for raw in _require_list(
                    persisted["open_intents"], "account.open_intents"
                )
            )
            intents_by_id = {item.id: item for item in existing_intents}
            for intent in intents:
                previous = intents_by_id.get(intent.id)
                if previous is not None and previous != intent:
                    raise LedgerError(f"conflicting intent ID: {intent.id}")
                intents_by_id[intent.id] = intent

            stored_progress = {
                item.intent_id: item
                for item in (
                    _progress_from_json(raw)
                    for raw in _require_list(
                        persisted["execution_progress"],
                        "account.execution_progress",
                    )
                )
            }
            if fills and not progress:
                raise LedgerError("execution fills require canonical execution progress")
            paired_fills = _validate_fill_summaries(
                fills,
                progress,
                stored_progress,
            )
            current, merged_progress = _apply_progress(
                account, intents_by_id, stored_progress, progress
            )
            current = _apply_settlement_updates(current, settlement_updates)
            current = _verify_execution_account(
                current,
                _execution_account_fact(batch),
            )
            current = _apply_risk_updates(current, updates)
            current = _apply_carry(current, accruals)
            new_risk_facts = tuple(
                _risk_fact_for_batch(batch, update, current.occurred_at)
                for update in updates
            )
            reconciled_events = _reconcile_batch_events(
                batch,
                paired_fills,
                new_risk_facts,
            )
            existing_events = tuple(
                _event_from_json(raw)
                for raw in _require_list(persisted["events"], "account.events")
            )
            current, merged_events = _apply_events(
                current, existing_events, reconciled_events
            )
            new_snapshot_id = _stable_snapshot_id(batch)
            current = replace(current, snapshot_id=new_snapshot_id)
            _validate_account(current)

            completed_ids = {
                item.intent_id for item in merged_progress.values() if item.status == "FILLED"
            }
            raw_account = {
                **encode_account_snapshot(current),
                "portfolio_snapshot_id": new_snapshot_id,
                "open_intents": [
                    _intent_to_json(item)
                    for item in sorted(intents_by_id.values(), key=lambda value: value.id)
                    if item.id not in completed_ids
                ],
                "fills": _merge_fill_rows(
                    _require_list(persisted["fills"], "account.fills"),
                    [
                        _fill_to_json(fill, progress_fill_id=progress_fill.id)
                        for progress_fill, fill in paired_fills
                    ],
                ),
                "execution_progress": [
                    _progress_to_json(item)
                    for item in sorted(
                        merged_progress.values(), key=lambda value: value.intent_id
                    )
                ],
                "risk_facts": [
                    *_require_list(persisted["risk_facts"], "account.risk_facts"),
                    *(_risk_fact_to_json(fact) for fact in new_risk_facts),
                ],
                "revision_transitions": list(
                    _require_list(
                        persisted["revision_transitions"],
                        "account.revision_transitions",
                    )
                ),
                "events": [_event_to_json(item) for item in merged_events],
                "committed_batches": [
                    *committed,
                    {
                        "run_key": batch.run_key,
                        "strategy_id": batch.strategy_id,
                        "strategy_revision": batch.strategy_revision,
                        "source_snapshot_id": batch.portfolio_snapshot_id,
                        "result_snapshot_id": new_snapshot_id,
                        "market_snapshot_id": batch.market_snapshot_id,
                        "risk_fact_ids": [fact.fact_id for fact in new_risk_facts],
                        "fingerprint": fingerprint,
                    },
                ],
                "run_results": [
                    *_require_list(persisted["run_results"], "account.run_results"),
                    *(
                        (
                            {
                                "strategy_id": batch.strategy_id,
                                "run_key": batch.run_key,
                                "request_fingerprint": batch.request_fingerprint,
                                "batch_fingerprint": fingerprint,
                                "batch": canonical_graph(batch),
                            },
                        )
                        if batch.request_fingerprint is not None
                        else ()
                    ),
                ],
            }
            next_store = {
                "version": LEDGER_SCHEMA_VERSION,
                "accounts": dict(store["accounts"]),
            }
            next_store["accounts"][batch.strategy_id] = raw_account
            validate_ledger_payload(next_store)
            _atomic_write(self.path, next_store)
            return current


def _merge_fill_rows(existing: list[Any], new_rows: list[dict[str, Any]]) -> list[Any]:
    result = list(existing)
    by_id: dict[str, Any] = {}
    for item in result:
        if not isinstance(item, Mapping):
            raise LedgerError("persisted fill must be an object")
        progress_fill_id = item.get("progress_fill_id")
        if type(progress_fill_id) is not str or not progress_fill_id:
            raise LedgerError("persisted fill lacks progress_fill_id")
        if progress_fill_id in by_id:
            raise LedgerError("persisted fills contain duplicate progress_fill_id")
        by_id[progress_fill_id] = item
    for item in new_rows:
        progress_fill_id = item["progress_fill_id"]
        previous = by_id.get(progress_fill_id)
        if previous is not None:
            if previous != item:
                raise LedgerError("conflicting persisted progress_fill_id")
            continue
        result.append(item)
        by_id[progress_fill_id] = item
    return result


__all__ = [
    "JsonLedgerStore",
    "LEDGER_SCHEMA_VERSION",
    "LedgerError",
    "LedgerSchemaError",
    "StalePortfolioSnapshotError",
    "UnknownPortfolioEventError",
    "decode_account_snapshot",
    "encode_account_snapshot",
    "validate_ledger_payload",
]
