"""One-way bootstrap from the legacy hot JSON ledger to PostgreSQL.

The current account snapshot moves to PostgreSQL.  The validated JSON payload
is copied byte-for-byte semantically into a compact, immutable lifecycle
archive; it is never used for live writes after cutover.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import atomic_replace_bytes, transaction_guard
from .contracts import PortfolioLedgerView
from .ledger import (
    _intent_from_json,
    _progress_from_json,
    decode_account_snapshot,
    validate_ledger_payload,
)
from .ports import LedgerStore


class PostgresBootstrapError(RuntimeError):
    """Raised when a cutover cannot prove source or target correctness."""


@dataclass(frozen=True)
class PostgresBootstrapReport:
    source_path: str
    archive_path: str
    source_sha256: str
    archive_sha256: str | None
    account_count: int
    open_intent_count: int
    open_progress_count: int
    applied: bool


def _decode_source(raw: bytes) -> dict:
    try:
        payload = json.loads(
            raw,
            parse_constant=lambda token: (_ for _ in ()).throw(
                PostgresBootstrapError(f"nonstandard JSON number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PostgresBootstrapError(f"legacy ledger is not valid JSON: {exc}") from exc
    return validate_ledger_payload(payload)


def _compact_bytes(payload: dict) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _decode_open_view(item: dict) -> PortfolioLedgerView:
    intents = tuple(
        sorted(
            (_intent_from_json(value) for value in item["open_intents"]),
            key=lambda value: value.id,
        )
    )
    open_ids = {value.id for value in intents}
    progress = tuple(
        sorted(
            (
                decoded
                for decoded in (
                    _progress_from_json(value)
                    for value in item["execution_progress"]
                )
                if decoded.intent_id in open_ids
            ),
            key=lambda value: value.intent_id,
        )
    )
    return PortfolioLedgerView(
        account=decode_account_snapshot(item),
        open_intents=intents,
        execution_progress=progress,
    )


def bootstrap_postgres_from_json(
    source_path: str | Path,
    *,
    archive_path: str | Path,
    apply: bool,
    database_url: str | None = None,
    schema: str | None = None,
    target_store: LedgerStore | None = None,
) -> PostgresBootstrapReport:
    """Validate, archive, and bootstrap current accounts into an empty store."""

    source = Path(source_path).expanduser()
    archive = Path(archive_path).expanduser()
    if source.resolve(strict=False) == archive.resolve(strict=False):
        raise PostgresBootstrapError("archive path must differ from source path")

    if apply and target_store is None:
        if not database_url or not schema:
            raise PostgresBootstrapError(
                "database_url and schema are required without target_store"
            )
        from .postgres_store import PostgresLedgerStore

        target_store = PostgresLedgerStore(database_url, schema=schema)
    if apply and target_store is not None and target_store.list_accounts():
        raise PostgresBootstrapError("PostgreSQL target schema is not empty")

    with transaction_guard((source, archive)):
        try:
            source_bytes = source.read_bytes()
        except OSError as exc:
            raise PostgresBootstrapError(f"cannot read legacy ledger: {exc}") from exc
        payload = _decode_source(source_bytes)
        accounts = tuple(
            decode_account_snapshot(item)
            for _, item in sorted(payload["accounts"].items())
        )
        views = tuple(
            _decode_open_view(item)
            for _, item in sorted(payload["accounts"].items())
        )
        open_intent_count = sum(len(view.open_intents) for view in views)
        open_progress_count = sum(len(view.execution_progress) for view in views)
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        if not apply:
            return PostgresBootstrapReport(
                source_path=str(source),
                archive_path=str(archive),
                source_sha256=source_hash,
                archive_sha256=None,
                account_count=len(accounts),
                open_intent_count=open_intent_count,
                open_progress_count=open_progress_count,
                applied=False,
            )

        seed_open_state = getattr(target_store, "bootstrap_open_state", None)
        if (open_intent_count or open_progress_count) and not callable(seed_open_state):
            raise PostgresBootstrapError(
                "target store cannot preserve active order state during cutover"
            )

        compact = _compact_bytes(payload)
        _decode_source(compact)
        if archive.exists():
            try:
                existing_archive = archive.read_bytes()
            except OSError as exc:
                raise PostgresBootstrapError(f"cannot read existing archive: {exc}") from exc
            if existing_archive != compact:
                raise PostgresBootstrapError(
                    f"archive exists with different contents: {archive}"
                )
        else:
            atomic_replace_bytes(archive, compact)
        archive_hash = hashlib.sha256(compact).hexdigest()

    if target_store is None:  # pragma: no cover - guarded above
        raise PostgresBootstrapError("PostgreSQL target store is unavailable")
    for account, view in zip(accounts, views):
        created = target_store.create_account(account)
        if created != account:
            raise PostgresBootstrapError(
                f"PostgreSQL bootstrap differs for strategy {account.strategy_id}"
            )
        if view.open_intents or view.execution_progress:
            seeded = seed_open_state(view)
            if (
                seeded.account != view.account
                or seeded.open_intents != view.open_intents
                or seeded.execution_progress != view.execution_progress
            ):
                raise PostgresBootstrapError(
                    "PostgreSQL active-order bootstrap differs for strategy "
                    f"{account.strategy_id}"
                )
    imported = {item.strategy_id: item for item in target_store.list_accounts()}
    expected = {item.strategy_id: item for item in accounts}
    if imported != expected:
        raise PostgresBootstrapError("PostgreSQL account verification failed")

    return PostgresBootstrapReport(
        source_path=str(source),
        archive_path=str(archive),
        source_sha256=source_hash,
        archive_sha256=archive_hash,
        account_count=len(accounts),
        open_intent_count=open_intent_count,
        open_progress_count=open_progress_count,
        applied=True,
    )


__all__ = [
    "PostgresBootstrapError",
    "PostgresBootstrapReport",
    "bootstrap_postgres_from_json",
]
