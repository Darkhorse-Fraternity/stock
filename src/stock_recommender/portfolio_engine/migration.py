"""Offline, atomic migrations for strategy schema v6 and ledger schema v2."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import threading
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import (
    default_exposure_policy,
    default_margin_policy,
    default_short_policy,
    normalize_exposure_policy,
    normalize_margin_policy,
    normalize_short_policy,
    validate_strategy_policies,
)
from .atomic_io import (
    OriginalFile,
    atomic_replace_many,
    fsync_directory,
    transaction_guard,
)
from .contracts import AccountSnapshot, PositionSide, PositionSnapshot
from .ledger import (
    LEDGER_SCHEMA_VERSION,
    LedgerError,
    decode_account_snapshot,
    encode_account_snapshot,
    validate_ledger_payload,
)
from .valuation import value_account


STRATEGY_SCHEMA_VERSION = 6
LEGACY_STRATEGY_SCHEMA_VERSION = 5
LEGACY_PORTFOLIO_SCHEMA_VERSION = 1
_OPEN_ORDER_STATES = frozenset({"INTENDED", "ACCEPTED", "PARTIAL"})
_MIGRATION_LOCK = threading.RLock()
_SOURCE_SNAPSHOT_ATTEMPTS = 3


class MigrationError(ValueError):
    """Raised when migration cannot prove a safe, parity-preserving result."""


@dataclass(frozen=True)
class MigrationReport:
    kind: str
    path: Path
    source_version: int
    target_version: int
    changed: bool
    applied: bool
    record_count: int
    nav_parity: bool = True
    backup_path: Path | None = None


@dataclass(frozen=True)
class CombinedMigrationReport:
    strategy: MigrationReport
    portfolio: MigrationReport


@dataclass(frozen=True)
class _PreparedMigration:
    report: MigrationReport
    payload: dict[str, Any]
    encoded: bytes


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MigrationError(f"{label} must be an object")
    return value


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise MigrationError(f"{label} must be an array")
    return value


def _nonempty_string(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MigrationError(f"{label} must be a non-empty trimmed string")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise MigrationError(f"{label} must be an integer >= {minimum}")
    return value


def _finite_number(value: object, label: str, *, minimum: float | None = None) -> float:
    if type(value) not in {int, float}:
        raise MigrationError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise MigrationError(f"{label} must be a finite number >= {minimum}")
    return number


def _aware_datetime(value: object, label: str) -> datetime:
    if type(value) is not str:
        raise MigrationError(f"{label} must be an ISO datetime")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationError(f"{label} must be an ISO datetime") from exc
    if result.tzinfo is None:
        raise MigrationError(f"{label} must include a timezone")
    return result


def _optional_date(value: object, label: str) -> date | None:
    if value is None:
        return None
    if type(value) is not str:
        raise MigrationError(f"{label} must be an ISO date or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise MigrationError(f"{label} must be an ISO date or null") from exc


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise MigrationError(f"migrated payload is not valid JSON: {exc}") from exc
    return (text + "\n").encode("utf-8")


def _parse_json(raw: bytes, path: Path) -> dict[str, Any]:
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MigrationError(f"{path} is not UTF-8") from exc
    def reject_constant(token: str) -> None:
        raise MigrationError(f"{path} contains non-standard JSON number: {token}")

    try:
        payload = json.loads(decoded, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise MigrationError(f"{path} contains malformed JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise MigrationError(f"{path} root must be an object")
    return payload


def _read_bytes(path: Path) -> bytes:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as exc:
        raise MigrationError(f"migration source does not exist: {path}") from exc
    except OSError as exc:
        raise MigrationError(f"cannot inspect migration source {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MigrationError(f"migration source must be a regular non-symlink file: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise MigrationError(f"cannot read migration source {path}: {exc}") from exc


class _SourceChanged(RuntimeError):
    """Internal signal that a source pathname changed during lock acquisition."""


def _source_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _inspect_source(path: Path) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as exc:
        raise MigrationError(f"migration source does not exist: {path}") from exc
    except OSError as exc:
        raise MigrationError(f"cannot inspect migration source {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MigrationError(f"migration source must be a regular non-symlink file: {path}")
    return metadata


def _source_order(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _verify_source_path(path: Path, descriptor_metadata: os.stat_result) -> None:
    try:
        path_metadata = os.lstat(path)
    except OSError as exc:
        raise _SourceChanged(f"migration source changed while being locked: {path}") from exc
    if (
        stat.S_ISLNK(path_metadata.st_mode)
        or not stat.S_ISREG(path_metadata.st_mode)
        or (path_metadata.st_dev, path_metadata.st_ino)
        != (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
    ):
        raise _SourceChanged(f"migration source changed while being locked: {path}")


@contextmanager
def _source_snapshot(paths: Iterable[Path], *, exclusive: bool):
    """Read one generation while holding locks on the source files themselves."""

    ordered = tuple(sorted(tuple(paths), key=_source_order))
    lock_operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    with _MIGRATION_LOCK:
        retained_stack: ExitStack | None = None
        originals: dict[Path, bytes] | None = None
        for _attempt in range(_SOURCE_SNAPSHOT_ATTEMPTS):
            stack = ExitStack()
            try:
                handles = {}
                descriptor_metadata = {}
                identities: set[tuple[int, int]] = set()
                for path in ordered:
                    inspected = _inspect_source(path)
                    flags = os.O_RDONLY
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    try:
                        descriptor = os.open(path, flags)
                    except FileNotFoundError as exc:
                        raise _SourceChanged(
                            f"migration source changed while being opened: {path}"
                        ) from exc
                    except OSError as exc:
                        raise MigrationError(
                            f"cannot open migration source {path}: {exc}"
                        ) from exc
                    try:
                        handle = os.fdopen(descriptor, "rb")
                    except BaseException:
                        os.close(descriptor)
                        raise
                    stack.enter_context(handle)
                    try:
                        fcntl.flock(handle.fileno(), lock_operation)
                        stack.callback(fcntl.flock, handle.fileno(), fcntl.LOCK_UN)
                    except OSError as exc:
                        raise MigrationError(
                            f"cannot lock migration source {path}: {exc}"
                        ) from exc
                    opened = os.fstat(handle.fileno())
                    if not stat.S_ISREG(opened.st_mode):
                        raise MigrationError(
                            f"migration source must be a regular non-symlink file: {path}"
                        )
                    identity = (opened.st_dev, opened.st_ino)
                    if identity in identities:
                        raise MigrationError(
                            "strategy and portfolio paths must be different files"
                        )
                    identities.add(identity)
                    if identity != (inspected.st_dev, inspected.st_ino):
                        raise _SourceChanged(
                            f"migration source changed while being opened: {path}"
                        )
                    handles[path] = handle
                    descriptor_metadata[path] = opened

                for path in ordered:
                    _verify_source_path(path, descriptor_metadata[path])

                candidate: dict[Path, bytes] = {}
                for path in ordered:
                    handle = handles[path]
                    before = os.fstat(handle.fileno())
                    handle.seek(0)
                    candidate[path] = handle.read()
                    after = os.fstat(handle.fileno())
                    if _source_signature(before) != _source_signature(after):
                        raise _SourceChanged(
                            f"migration source changed while being read: {path}"
                        )
                    _verify_source_path(path, after)
                retained_stack = stack
                originals = candidate
                break
            except _SourceChanged:
                stack.close()
                continue
            except BaseException:
                stack.close()
                raise

        if retained_stack is None or originals is None:
            raise MigrationError(
                "migration sources changed repeatedly; retry for a consistent snapshot"
            )
        try:
            yield originals
        finally:
            retained_stack.close()


def _strategy_sections(strategy: Mapping[str, Any], *, current: bool) -> None:
    strategy_id = _nonempty_string(strategy.get("id"), "strategy.id")
    revision = strategy.get("revision")
    _integer(revision, f"strategy[{strategy_id}].revision", minimum=1)
    required = [
        "lifecycle",
        "signal",
        "allocation",
        "validation",
        "portfolio",
        "delivery",
        "parameters",
    ]
    if current:
        required.extend(("exposure_policy", "margin_policy", "short_policy"))
    missing = [key for key in required if not isinstance(strategy.get(key), Mapping)]
    if missing:
        raise MigrationError(
            f"strategy[{strategy_id}] has invalid sections: {', '.join(missing)}"
        )


def _validate_strategy_v6(payload: object) -> dict[str, Any]:
    store = _mapping(payload, "strategy store")
    if store.get("version") != STRATEGY_SCHEMA_VERSION:
        raise MigrationError(
            f"strategy schema must be {STRATEGY_SCHEMA_VERSION} after migration"
        )
    strategies = _list(store.get("strategies"), "strategy store.strategies")
    identifiers: set[str] = set()
    for index, raw in enumerate(strategies):
        strategy = _mapping(raw, f"strategies[{index}]")
        if strategy.get("version") != STRATEGY_SCHEMA_VERSION:
            raise MigrationError("strategy version must match store version")
        _strategy_sections(strategy, current=True)
        identifier = str(strategy["id"])
        if identifier in identifiers:
            raise MigrationError(f"duplicate strategy id: {identifier}")
        identifiers.add(identifier)
        try:
            validate_strategy_policies(strategy)
        except (TypeError, ValueError) as exc:
            raise MigrationError(f"strategy[{identifier}] policy is invalid: {exc}") from exc
        normalized = (
            normalize_exposure_policy(strategy["exposure_policy"]),
            normalize_margin_policy(strategy["margin_policy"]),
            normalize_short_policy(strategy["short_policy"]),
        )
        originals = (
            dict(strategy["exposure_policy"]),
            dict(strategy["margin_policy"]),
            dict(strategy["short_policy"]),
        )
        if originals != normalized:
            raise MigrationError(
                f"strategy[{identifier}] policies are incomplete or non-canonical"
            )
    active = store.get("active_strategy_id")
    if active is not None and (type(active) is not str or active not in identifiers):
        raise MigrationError("active_strategy_id must reference an existing strategy")
    return dict(store)


def _convert_strategy_store(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("version") != LEGACY_STRATEGY_SCHEMA_VERSION:
        raise MigrationError(
            "strategy store must use schema 5 or 6; unsupported migration source"
        )
    strategies = _list(payload.get("strategies"), "strategy store.strategies")
    identifiers: set[str] = set()
    converted: list[dict[str, Any]] = []
    for index, raw in enumerate(strategies):
        strategy = _mapping(raw, f"strategies[{index}]")
        if strategy.get("version") != LEGACY_STRATEGY_SCHEMA_VERSION:
            raise MigrationError("strategy version must match store version")
        _strategy_sections(strategy, current=False)
        identifier = str(strategy["id"])
        if identifier in identifiers:
            raise MigrationError(f"duplicate strategy id: {identifier}")
        identifiers.add(identifier)
        item = deepcopy(dict(strategy))
        item["version"] = STRATEGY_SCHEMA_VERSION
        item["exposure_policy"] = default_exposure_policy()
        item["margin_policy"] = default_margin_policy()
        item["short_policy"] = default_short_policy()
        converted.append(item)
    active = payload.get("active_strategy_id")
    if active is not None and (type(active) is not str or active not in identifiers):
        raise MigrationError("active_strategy_id must reference an existing strategy")
    result = deepcopy(payload)
    result["version"] = STRATEGY_SCHEMA_VERSION
    result["strategies"] = converted
    _validate_strategy_v6(result)
    return result


def _prepare_strategy(path: Path, raw: bytes) -> _PreparedMigration:
    payload = _parse_json(raw, path)
    version = payload.get("version")
    if type(version) is not int:
        raise MigrationError("strategy schema version must be an integer")
    if version == STRATEGY_SCHEMA_VERSION:
        converted = _validate_strategy_v6(payload)
        changed = False
    else:
        converted = _convert_strategy_store(payload)
        changed = True
    report = MigrationReport(
        kind="strategy",
        path=path,
        source_version=version,
        target_version=STRATEGY_SCHEMA_VERSION,
        changed=changed,
        applied=False,
        record_count=len(converted["strategies"]),
    )
    return _PreparedMigration(report, converted, _json_bytes(converted))


def _stable_identifier(prefix: str, value: object) -> str:
    material = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return prefix + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _position_from_v1(symbol: str, raw: object) -> PositionSnapshot:
    item = _mapping(raw, f"position[{symbol}]")
    embedded = item.get("symbol", symbol)
    if embedded != symbol:
        raise MigrationError(f"position key does not match symbol: {symbol}")
    quantity = _integer(item.get("quantity"), f"position[{symbol}].quantity", minimum=1)
    average_cost = _finite_number(
        item.get("average_cost"), f"position[{symbol}].average_cost", minimum=0.000000001
    )
    current_price = _finite_number(
        item.get("current_price", average_cost),
        f"position[{symbol}].current_price",
        minimum=0.000000001,
    )
    peak_price = _finite_number(
        item.get("peak_price") or max(average_cost, current_price),
        f"position[{symbol}].peak_price",
        minimum=0.000000001,
    )
    sellable_quantity = _integer(
        item.get("sellable_quantity", quantity),
        f"position[{symbol}].sellable_quantity",
    )
    try:
        return PositionSnapshot(
            symbol=symbol,
            side=PositionSide.LONG,
            quantity=quantity,
            average_cost=average_cost,
            current_price=current_price,
            peak_price=peak_price,
            trough_price=None,
            trailing_active=item.get("trailing_active", False),
            position_mode="NORMAL",
            sellable_quantity=sellable_quantity,
            sellable_on=_optional_date(
                item.get("sellable_on"), f"position[{symbol}].sellable_on"
            ),
            borrow_lifecycle=None,
        )
    except (TypeError, ValueError) as exc:
        raise MigrationError(f"position[{symbol}] is invalid: {exc}") from exc


def _order_intent_from_v1(
    raw: object,
    *,
    positions: Mapping[str, PositionSnapshot],
    snapshot_id: str,
    created_market_at: datetime,
) -> dict[str, Any] | None:
    order = _mapping(raw, "order")
    status = _nonempty_string(order.get("status"), "order.status")
    if status not in _OPEN_ORDER_STATES:
        return None
    symbol = _nonempty_string(order.get("symbol"), "order.symbol")
    side = _nonempty_string(order.get("side"), "order.side").upper()
    if side not in {"BUY", "SELL"}:
        raise MigrationError(f"unsupported v1 order side: {side}")
    quantity = _integer(order.get("quantity"), "order.quantity", minimum=1)
    filled = _integer(order.get("filled_quantity", 0), "order.filled_quantity")
    if filled >= quantity:
        raise MigrationError("open order must have positive remaining quantity")
    remaining = quantity - filled
    held = positions.get(symbol)
    if side == "BUY":
        effect = "INCREASE" if held is not None else "OPEN"
    else:
        if held is None:
            raise MigrationError(f"SELL order has no LONG position: {symbol}")
        if remaining > held.quantity:
            raise MigrationError(f"SELL order exceeds LONG position: {symbol}")
        effect = "CLOSE" if remaining == held.quantity else "REDUCE"
    reason = str(order.get("reason") or "migrated v1 order").strip()
    if not reason:
        reason = "migrated v1 order"
    identifier = order.get("id")
    if type(identifier) is not str or not identifier.strip():
        identifier = _stable_identifier("order-", order)
    return {
        "id": identifier,
        "symbol": symbol,
        "position_side": "LONG",
        "order_side": side,
        "position_effect": effect,
        "quantity": remaining,
        "reason": reason,
        "created_snapshot_id": snapshot_id,
        "created_market_at": created_market_at.isoformat(),
    }


def _currency_quantum(account: Mapping[str, Any]) -> Decimal:
    currency = str(account.get("currency") or "CNY").upper()
    if currency in {"JPY", "KRW"}:
        return Decimal("1")
    if currency in {"BHD", "KWD", "OMR"}:
        return Decimal("0.001")
    return Decimal("0.01")


def _minor_units(value: float, quantum: Decimal) -> Decimal:
    try:
        return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ValueError) as exc:
        raise MigrationError("NAV cannot be represented at currency precision") from exc


def _convert_account_v1(strategy_id: str, raw: object, now: datetime) -> dict[str, Any]:
    account = _mapping(raw, f"account[{strategy_id}]")
    embedded_strategy = account.get("strategy_id", strategy_id)
    if embedded_strategy != strategy_id:
        raise MigrationError("account key does not match strategy_id")
    account_id = _nonempty_string(account.get("id", strategy_id), "account.id")
    revision = _integer(account.get("strategy_revision", 1), "strategy_revision", minimum=1)
    occurred_raw = account.get("updated_at") or account.get("created_at")
    occurred_at = now if occurred_raw is None else _aware_datetime(occurred_raw, "updated_at")
    available_cash = _finite_number(account.get("cash"), "cash")
    reserved_cash = _finite_number(
        account.get("reserved_cash", 0.0), "reserved_cash", minimum=0.0
    )
    positions_payload = _mapping(account.get("positions", {}), "account.positions")
    positions: dict[str, PositionSnapshot] = {}
    for symbol, raw_position in positions_payload.items():
        normalized_symbol = _nonempty_string(symbol, "position symbol")
        positions[normalized_symbol] = _position_from_v1(normalized_symbol, raw_position)
    snapshot_id = _stable_identifier(
        "snapshot-v1-", {"strategy_id": strategy_id, "account": account}
    )
    snapshot = AccountSnapshot(
        id=account_id,
        strategy_id=strategy_id,
        strategy_revision=revision,
        occurred_at=occurred_at,
        available_cash=available_cash,
        restricted_short_proceeds=0.0,
        margin_loan=0.0,
        accrued_financing_cost=0.0,
        accrued_borrow_cost=0.0,
        positions=tuple(positions[symbol] for symbol in sorted(positions)),
        carry_accruals=(),
        financing_lifecycle=None,
        reserved_cash=reserved_cash,
        snapshot_id=snapshot_id,
    )
    orders = _list(account.get("orders", []), "account.orders")
    open_intents = []
    for raw_order in orders:
        intent = _order_intent_from_v1(
            raw_order,
            positions=positions,
            snapshot_id=snapshot_id,
            created_market_at=occurred_at,
        )
        if intent is not None:
            open_intents.append(intent)
    intent_ids = [intent["id"] for intent in open_intents]
    if len(set(intent_ids)) != len(intent_ids):
        raise MigrationError("open orders contain duplicate IDs")
    aggregate_sells: dict[str, int] = {}
    for intent in open_intents:
        if intent["order_side"] == "SELL":
            symbol = intent["symbol"]
            aggregate_sells[symbol] = aggregate_sells.get(symbol, 0) + intent["quantity"]
    for symbol, quantity in aggregate_sells.items():
        if quantity > positions[symbol].quantity:
            raise MigrationError(f"aggregate SELL orders exceed LONG position: {symbol}")
    run_keys = _list(account.get("committed_run_keys", []), "committed_run_keys")
    committed_batches = []
    for run_key in run_keys:
        value = _nonempty_string(run_key, "committed_run_key")
        committed_batches.append(
            {
                "run_key": value,
                "strategy_id": strategy_id,
                "strategy_revision": revision,
                "source_snapshot_id": snapshot_id,
                "result_snapshot_id": snapshot_id,
                "market_snapshot_id": None,
                "risk_fact_ids": [],
                "fingerprint": _stable_identifier("legacy-", value),
            }
        )
    result = {
        **encode_account_snapshot(snapshot),
        "open_intents": sorted(open_intents, key=lambda item: item["id"]),
        "fills": [],
        "execution_progress": [],
        "risk_facts": [],
        "revision_transitions": [],
        "events": [],
        "committed_batches": committed_batches,
    }
    decoded = decode_account_snapshot(result)
    prices = {
        position.symbol: position.current_price or position.average_cost
        for position in decoded.positions
    }
    post_nav = value_account(decoded, prices).metrics.equity
    source_nav = _finite_number(
        account.get("latest_nav", available_cash + sum(
            position.quantity * (position.current_price or position.average_cost)
            for position in positions.values()
        )),
        "latest_nav",
    )
    quantum = _currency_quantum(account)
    if _minor_units(source_nav, quantum) != _minor_units(post_nav, quantum):
        raise MigrationError(
            f"NAV parity failed for {strategy_id}: before={source_nav}, after={post_nav}"
        )
    return result


def _prepare_portfolio(path: Path, raw: bytes, now: datetime) -> _PreparedMigration:
    payload = _parse_json(raw, path)
    version = payload.get("version")
    if type(version) is not int:
        raise MigrationError("portfolio schema version must be an integer")
    if version == LEDGER_SCHEMA_VERSION:
        try:
            converted = validate_ledger_payload(payload)
        except LedgerError as exc:
            raise MigrationError(f"invalid current ledger: {exc}") from exc
        changed = False
    elif version == LEGACY_PORTFOLIO_SCHEMA_VERSION:
        accounts = _mapping(payload.get("accounts"), "portfolio store.accounts")
        converted_accounts = {
            _nonempty_string(strategy_id, "account strategy id"): _convert_account_v1(
                strategy_id, raw_account, now
            )
            for strategy_id, raw_account in accounts.items()
        }
        converted = {"version": LEDGER_SCHEMA_VERSION, "accounts": converted_accounts}
        try:
            validate_ledger_payload(converted)
        except LedgerError as exc:
            raise MigrationError(f"migrated ledger validation failed: {exc}") from exc
        changed = True
    else:
        raise MigrationError(
            "portfolio store must use schema 1 or 2; unsupported migration source"
        )
    report = MigrationReport(
        kind="portfolio",
        path=path,
        source_version=version,
        target_version=LEDGER_SCHEMA_VERSION,
        changed=changed,
        applied=False,
        record_count=len(converted["accounts"]),
        nav_parity=True,
    )
    return _PreparedMigration(report, converted, _json_bytes(converted))


@contextmanager
def _locked_paths(paths: Iterable[Path]):
    with _MIGRATION_LOCK, transaction_guard(paths):
        yield


@contextmanager
def _combined_apply_snapshot(paths: Iterable[Path]):
    targets = tuple(paths)
    with _locked_paths(targets), _source_snapshot(
        targets, exclusive=True
    ) as originals:
        yield originals


def _timestamp(now: datetime) -> str:
    if type(now) is not datetime or now.tzinfo is None:
        raise MigrationError("migration timestamp must be a timezone-aware datetime")
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _backup_path(path: Path, stamp: str) -> Path:
    return path.with_name(path.name + f".bak.{stamp}")


def _write_backup(path: Path, raw: bytes, stamp: str) -> Path:
    backup = _backup_path(path, stamp)
    try:
        source_metadata = os.lstat(path)
    except OSError as exc:
        raise MigrationError(f"cannot inspect migration source {path}: {exc}") from exc
    if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISREG(source_metadata.st_mode):
        raise MigrationError(f"migration source must be a regular non-symlink file: {path}")
    source_mode = stat.S_IMODE(source_metadata.st_mode)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(backup, flags, source_mode)
        os.fchmod(descriptor, source_mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        try:
            backup_metadata = os.lstat(backup)
        except OSError as exc:
            raise MigrationError(f"cannot inspect migration backup {backup}: {exc}") from exc
        if stat.S_ISLNK(backup_metadata.st_mode) or not stat.S_ISREG(
            backup_metadata.st_mode
        ):
            raise MigrationError(
                f"migration backup must be a regular non-symlink file: {backup}"
            )
        backup_mode = stat.S_IMODE(backup_metadata.st_mode)
        if backup_mode & ~source_mode:
            raise MigrationError(f"migration backup permissions are too broad: {backup}")
        if _read_bytes(backup) != raw:
            raise MigrationError(f"timestamped backup already exists with other data: {backup}")
    except OSError as exc:
        raise MigrationError(f"cannot create migration backup {backup}: {exc}") from exc
    fsync_directory(path.parent)
    return backup


def _apply_prepared(
    prepared: tuple[_PreparedMigration, ...],
    originals: Mapping[Path, bytes],
    now: datetime,
) -> tuple[MigrationReport, ...]:
    changed = tuple(item for item in prepared if item.report.changed)
    if not changed:
        return tuple(item.report for item in prepared)
    stamp = _timestamp(now)
    backups: dict[Path, Path] = {}
    try:
        for item in prepared:
            path = item.report.path
            backups[path] = _write_backup(path, originals[path], stamp)
        atomic_replace_many(
            {item.report.path: item.encoded for item in changed},
            originals={
                item.report.path: OriginalFile(True, originals[item.report.path])
                for item in changed
            },
            recovery_backups={
                item.report.path: backups[item.report.path]
                for item in changed
            },
            durable=True,
        )
    except BaseException as exc:
        raise MigrationError(f"atomic migration failed: {exc}") from exc
    reports = []
    for item in prepared:
        report = item.report
        reports.append(
            replace(
                report,
                applied=report.changed,
                backup_path=backups.get(report.path),
            )
        )
    return tuple(reports)


def migrate_strategy_store(
    path: str | Path,
    *,
    apply: bool = False,
    now: datetime | None = None,
) -> MigrationReport:
    target = Path(path).expanduser()
    current = now or datetime.now().astimezone()
    if not apply:
        with _source_snapshot((target,), exclusive=False) as originals:
            prepared = _prepare_strategy(target, originals[target])
            return prepared.report
    with _locked_paths((target,)), _source_snapshot(
        (target,), exclusive=True
    ) as originals:
        prepared = _prepare_strategy(target, originals[target])
        return _apply_prepared((prepared,), originals, current)[0]


def migrate_portfolio_store(
    path: str | Path,
    *,
    apply: bool = False,
    now: datetime | None = None,
) -> MigrationReport:
    target = Path(path).expanduser()
    current = now or datetime.now().astimezone()
    if not apply:
        with _source_snapshot((target,), exclusive=False) as originals:
            prepared = _prepare_portfolio(target, originals[target], current)
            return prepared.report
    with _locked_paths((target,)), _source_snapshot(
        (target,), exclusive=True
    ) as originals:
        prepared = _prepare_portfolio(target, originals[target], current)
        return _apply_prepared((prepared,), originals, current)[0]


def migrate_stores(
    strategy_path: str | Path,
    portfolio_path: str | Path,
    *,
    apply: bool = False,
    now: datetime | None = None,
) -> CombinedMigrationReport:
    strategy_target = Path(strategy_path).expanduser()
    portfolio_target = Path(portfolio_path).expanduser()
    if _source_order(strategy_target) == _source_order(portfolio_target):
        raise MigrationError("strategy and portfolio paths must be different files")
    current = now or datetime.now().astimezone()
    targets = (strategy_target, portfolio_target)
    lock_context = (
        _source_snapshot(targets, exclusive=False)
        if not apply
        else _combined_apply_snapshot(targets)
    )
    with lock_context as originals:
        strategy = _prepare_strategy(strategy_target, originals[strategy_target])
        portfolio = _prepare_portfolio(
            portfolio_target, originals[portfolio_target], current
        )
        if not apply:
            reports = (strategy.report, portfolio.report)
        else:
            reports = _apply_prepared(
                (strategy, portfolio), originals, current
            )
        return CombinedMigrationReport(
            strategy=reports[0],
            portfolio=reports[1],
        )


__all__ = [
    "CombinedMigrationReport",
    "MigrationError",
    "MigrationReport",
    "migrate_portfolio_store",
    "migrate_stores",
    "migrate_strategy_store",
]
