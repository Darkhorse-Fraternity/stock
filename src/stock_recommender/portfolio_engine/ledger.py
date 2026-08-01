"""Schema-v2 JSON ledger with atomic, idempotent account transitions."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

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
    PositionEffect,
    PositionRiskUpdate,
    PositionSide,
    PositionSnapshot,
)
from .atomic_io import atomic_replace_bytes, transaction_guard
from .margin import project_account_for_intent
from .valuation import value_account


LEDGER_SCHEMA_VERSION = 2
_PROCESS_LOCK = threading.RLock()
_INFORMATIONAL_EVENT_TYPES = frozenset(
    {
        "ACCOUNT_OPENED",
        "ORDER_INTENDED",
        "ORDER_FILLED",
        "ORDER_PARTIAL",
        "ORDER_CANCELLED",
        "ORDER_EXPIRED",
        "EXIT_TRIGGERED",
        "RISK_CHANGED",
        "STRATEGY_VERSION_ACTIVATED",
        "PIPELINE_COMPLETED",
        "MARGIN_CALL",
        "COVER_ONLY",
        "FORCED_DELEVERAGE",
        "FINANCING_COST_ACCRUED",
        "BORROW_COST_ACCRUED",
    }
)
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
    {"open_intents", "fills", "execution_progress", "events", "committed_batches"}
)


class LedgerError(ValueError):
    """Base class for ledger validation and transition failures."""


class LedgerSchemaError(LedgerError):
    """Raised when persisted JSON is malformed or not schema v2."""


class StalePortfolioSnapshotError(LedgerError):
    """Raised when a new run was evaluated from an obsolete snapshot."""


class UnknownPortfolioEventError(LedgerError):
    """Raised when no exhaustive event handler exists."""


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
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, LedgerSchemaError):
            raise
        raise LedgerSchemaError("invalid order intent") from exc


def _fill_to_json(fill: ExecutionFill) -> dict[str, Any]:
    return {
        "intent_id": fill.intent_id,
        "symbol": fill.symbol,
        "quantity": fill.quantity,
        "price": fill.price,
        "fees": fill.fees,
        "status": fill.status,
    }


def _fill_from_json(value: object) -> ExecutionFill:
    item = _require_mapping(value, "fill")
    _require_keys(
        item,
        frozenset({"intent_id", "symbol", "quantity", "price", "fees", "status"}),
        "fill",
    )
    try:
        return ExecutionFill(
            intent_id=item["intent_id"],
            symbol=item["symbol"],
            quantity=item["quantity"],
            price=item["price"],
            fees=item["fees"],
            status=item["status"],
        )
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

    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__type__": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": {
                field.name: _fingerprint_plain(getattr(value, field.name))
                for field in fields(value)
            },
        }
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise LedgerError("observable mapping keys must be strings")
            result[key] = _fingerprint_plain(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_fingerprint_plain(item) for item in value]
    if isinstance(value, frozenset):
        encoded = [_fingerprint_plain(item) for item in value]
        return sorted(
            encoded,
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
        )
    if isinstance(value, Enum):
        return {
            "__enum__": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": _fingerprint_plain(value.value),
        }
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    if value is None or type(value) in {bool, int, float, str}:
        if type(value) is float and not math.isfinite(value):
            raise LedgerError("non-finite observable number")
        return value
    raise LedgerError(f"unsupported observable value: {type(value).__name__}")


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
            "events",
            "committed_batches",
        ):
            _require_list(item[key], f"account.{key}")
        intents = [_intent_from_json(raw) for raw in item["open_intents"]]
        if len({intent.id for intent in intents}) != len(intents):
            raise LedgerSchemaError("account.open_intents contains duplicate IDs")
        fills = [_fill_from_json(raw) for raw in item["fills"]]
        fill_keys = [
            (
                fill.intent_id,
                fill.symbol,
                fill.quantity,
                fill.price,
                fill.fees,
                fill.status,
            )
            for fill in fills
        ]
        if len(set(fill_keys)) != len(fill_keys):
            raise LedgerSchemaError("account.fills contains duplicate rows")
        progress = [
            _progress_from_json(raw) for raw in item["execution_progress"]
        ]
        if len({entry.intent_id for entry in progress}) != len(progress):
            raise LedgerSchemaError("account.execution_progress contains duplicate intents")
        events = [_event_from_json(raw) for raw in item["events"]]
        if len({event.id for event in events}) != len(events):
            raise LedgerSchemaError("account.events contains duplicate IDs")
        for event in events:
            try:
                _validate_event(event)
            except LedgerError as exc:
                raise LedgerSchemaError(f"invalid persisted event: {exc}") from exc
        committed = item["committed_batches"]
        run_keys: set[str] = set()
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
            run_keys.add(run_key)
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
    accruals = _unique_typed(
        batch.carry_accruals,
        batch.stage_outputs,
        fact_kinds=frozenset({"carry_accruals"}),
        expected_type=CarryAccrualRecord,
        identity=lambda item: item.idempotency_key,
    )
    return intents, fills, progress, updates, accruals


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


def _execution_accounting_core(account: AccountSnapshot) -> dict[str, Any]:
    """Return fields independently replayable without market settlement policy."""

    return {
        "id": account.id,
        "strategy_id": account.strategy_id,
        "strategy_revision": account.strategy_revision,
        "available_cash": account.available_cash,
        "reserved_cash": account.reserved_cash,
        "restricted_short_proceeds": account.restricted_short_proceeds,
        "margin_loan": account.margin_loan,
        "accrued_financing_cost": account.accrued_financing_cost,
        "accrued_borrow_cost": account.accrued_borrow_cost,
        "carry_accruals": account.carry_accruals,
        "positions": tuple(
            (held.symbol, held.side, held.quantity, held.average_cost)
            for held in sorted(account.positions, key=lambda item: item.symbol)
        ),
    }


def _adopt_execution_account(
    source: AccountSnapshot,
    replayed: AccountSnapshot,
    canonical: AccountSnapshot | None,
) -> AccountSnapshot:
    if canonical is None:
        return replayed
    if (
        canonical.id != source.id
        or canonical.strategy_id != source.strategy_id
        or canonical.strategy_revision != source.strategy_revision
        or canonical.snapshot_id != source.snapshot_id
        or _execution_accounting_core(canonical)
        != _execution_accounting_core(replayed)
    ):
        raise LedgerError("execution_account does not match replayed execution progress")
    return canonical


def _validate_fill_summaries(
    fills: Iterable[ExecutionFill],
    progress: Iterable[OrderExecutionProgress],
    existing_progress: Mapping[str, OrderExecutionProgress],
) -> None:
    new_detail_keys: list[tuple[object, ...]] = []
    for item in progress:
        prior = existing_progress.get(item.intent_id)
        prior_count = 0 if prior is None else len(prior.fills)
        new_detail_keys.extend(
            (
                detail.intent_id,
                detail.symbol,
                detail.quantity,
                detail.price,
                detail.fees,
                detail.status,
            )
            for detail in item.fills[prior_count:]
        )
    unmatched = list(new_detail_keys)
    for fill in fills:
        key = (
            fill.intent_id,
            fill.symbol,
            fill.quantity,
            fill.price,
            fill.fees,
            fill.status,
        )
        try:
            unmatched.remove(key)
        except ValueError as exc:
            raise LedgerError("execution fill summary does not match new progress") from exc
    if unmatched:
        raise LedgerError("new execution progress is missing an execution fill summary")


def _batch_fingerprint(
    batch: DecisionBatch,
    facts: tuple[
        tuple[OrderIntent, ...],
        tuple[ExecutionFill, ...],
        tuple[OrderExecutionProgress, ...],
        tuple[PositionRiskUpdate, ...],
        tuple[CarryAccrualRecord, ...],
    ],
) -> str:
    intents, fills, progress, updates, accruals = facts
    material = {
        "batch": _fingerprint_plain(batch),
        "canonical_facts": {
            "intents": [_fingerprint_plain(item) for item in intents],
            "fills": [_fingerprint_plain(item) for item in fills],
            "execution_progress": [_fingerprint_plain(item) for item in progress],
            "position_risk_updates": [_fingerprint_plain(item) for item in updates],
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
                    f"{account.id}|{intent.id}|{fill.snapshot_id}".encode("utf-8")
                ).hexdigest()[:24],
                started_on=fill.occurred_at.date(),
            )
        positions.append(
            replace(
                held,
                current_price=fill.price,
                peak_price=(
                    max(held.peak_price or fill.price, fill.price)
                    if held.side is PositionSide.LONG
                    else None
                ),
                trough_price=(
                    min(held.trough_price or fill.price, fill.price)
                    if held.side is PositionSide.SHORT
                    else None
                ),
                borrow_lifecycle=lifecycle,
            )
        )
    financing = account.financing_lifecycle
    if account.margin_loan <= 0:
        financing = None
    elif financing is None:
        financing = AccrualLifecycle(
            id="financing-" + hashlib.sha256(
                f"{account.id}|{intent.id}|{fill.snapshot_id}".encode("utf-8")
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
    if not records and not carry_events:
        return
    unmatched = list(carry_events)
    for record in records:
        expected_type = f"{record.cost_type.value}_COST_ACCRUED"
        match = next(
            (
                event
                for event in unmatched
                if event.type == expected_type
                and type(event.data.get("amount")) in {int, float}
                and math.isclose(
                    float(event.data["amount"]),
                    record.amount,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
                and event.data.get("symbol", record.symbol) == record.symbol
                and event.data.get("lifecycle_id", record.lifecycle_id)
                == record.lifecycle_id
                and event.data.get("accrual_date", record.accrual_date.isoformat())
                == record.accrual_date.isoformat()
            ),
            None,
        )
        if match is None:
            raise LedgerError("carry accrual requires one matching carry event")
        unmatched.remove(match)
    if unmatched:
        raise LedgerError("carry event requires one matching carry accrual")


def _validate_event(event: PortfolioEvent) -> None:
    _json_plain(event.data)
    if event.type == "CASH_ADJUSTED":
        if set(event.data) != {"amount"}:
            raise LedgerError("CASH_ADJUSTED data must contain only amount")
        amount = event.data["amount"]
        if type(amount) not in {int, float} or not math.isfinite(float(amount)):
            raise LedgerError("CASH_ADJUSTED amount must be finite")
        return
    if event.type in _INFORMATIONAL_EVENT_TYPES:
        return
    raise UnknownPortfolioEventError(f"unknown portfolio event type: {event.type}")


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

    def list_accounts(self) -> tuple[AccountSnapshot, ...]:
        with _PROCESS_LOCK, transaction_guard((self.path,)):
            store = _read_store(self.path)
            return tuple(
                decode_account_snapshot(store["accounts"][strategy_id])
                for strategy_id in sorted(store["accounts"])
            )

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

            intents, fills, progress, updates, accruals = canonical_facts
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
            _validate_fill_summaries(fills, progress, stored_progress)
            current, merged_progress = _apply_progress(
                account, intents_by_id, stored_progress, progress
            )
            current = _adopt_execution_account(
                account,
                current,
                _execution_account_fact(batch),
            )
            current = _apply_risk_updates(current, updates)
            current = _apply_carry(current, accruals)
            existing_events = tuple(
                _event_from_json(raw)
                for raw in _require_list(persisted["events"], "account.events")
            )
            current, merged_events = _apply_events(
                current, existing_events, batch.events
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
                "fills": _merge_json_rows(
                    _require_list(persisted["fills"], "account.fills"),
                    [_fill_to_json(item) for item in fills],
                ),
                "execution_progress": [
                    _progress_to_json(item)
                    for item in sorted(
                        merged_progress.values(), key=lambda value: value.intent_id
                    )
                ],
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
                        "fingerprint": fingerprint,
                    },
                ],
            }
            next_store = {
                "version": LEDGER_SCHEMA_VERSION,
                "accounts": dict(store["accounts"]),
            }
            next_store["accounts"][batch.strategy_id] = raw_account
            _atomic_write(self.path, next_store)
            return current


def _merge_json_rows(existing: list[Any], new_rows: list[dict[str, Any]]) -> list[Any]:
    result = list(existing)
    fingerprints = {
        json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for item in result
    }
    for item in new_rows:
        fingerprint = json.dumps(
            item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        if fingerprint not in fingerprints:
            result.append(item)
            fingerprints.add(fingerprint)
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
