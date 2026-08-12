"""Portfolio ledger backend selection.

Runtime code depends on the ``LedgerStore`` port, not on a concrete JSON or
PostgreSQL adapter.  Production selects PostgreSQL explicitly; JSON remains a
local development and migration source adapter.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from .portfolio_engine.ledger import JsonLedgerStore, portfolio_ledger_path
from .portfolio_engine.ports import LedgerStore


LEDGER_BACKEND_ENV = "STOCK_AGENT_LEDGER_BACKEND"
LEDGER_DATABASE_URL_ENV = "STOCK_AGENT_PORTFOLIO_DATABASE_URL"
LEDGER_SCHEMA_ENV = "STOCK_AGENT_PORTFOLIO_SCHEMA"
LEDGER_ARCHIVE_PATH_ENV = "STOCK_AGENT_PORTFOLIO_ARCHIVE_PATH"
JSON_LEDGER_MAX_BYTES_ENV = "STOCK_AGENT_JSON_LEDGER_MAX_BYTES"
DEFAULT_POSTGRES_SCHEMA = "stock_agent_portfolio"
DEFAULT_JSON_LEDGER_MAX_BYTES = 16 * 1024 * 1024


def open_portfolio_store(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> LedgerStore:
    """Open the configured ledger adapter and fail closed on bad config."""

    values = os.environ if environ is None else environ
    backend = values.get(LEDGER_BACKEND_ENV, "json").strip().lower()
    if backend == "json":
        target = portfolio_ledger_path(path)
        raw_limit = values.get(
            JSON_LEDGER_MAX_BYTES_ENV,
            str(DEFAULT_JSON_LEDGER_MAX_BYTES),
        ).strip()
        try:
            max_bytes = int(raw_limit)
        except ValueError as exc:
            raise ValueError(f"{JSON_LEDGER_MAX_BYTES_ENV} must be an integer") from exc
        if max_bytes < 1:
            raise ValueError(f"{JSON_LEDGER_MAX_BYTES_ENV} must be positive")
        try:
            size = target.stat().st_size
        except FileNotFoundError:
            size = 0
        if size > max_bytes:
            raise RuntimeError(
                f"JSON portfolio ledger is {size} bytes; refusing the unbounded "
                f"runtime backend above {max_bytes} bytes. Configure PostgreSQL."
            )
        return JsonLedgerStore(target)
    if backend != "postgres":
        raise ValueError(
            f"{LEDGER_BACKEND_ENV} must be 'json' or 'postgres'; got {backend!r}"
        )

    database_url = values.get(LEDGER_DATABASE_URL_ENV, "").strip()
    if not database_url:
        raise ValueError(
            f"{LEDGER_DATABASE_URL_ENV} is required when {LEDGER_BACKEND_ENV}=postgres"
        )
    schema = values.get(LEDGER_SCHEMA_ENV, DEFAULT_POSTGRES_SCHEMA).strip()
    if not schema:
        raise ValueError(f"{LEDGER_SCHEMA_ENV} must not be empty")

    from .portfolio_engine.postgres_store import PostgresLedgerStore

    live: LedgerStore = PostgresLedgerStore(database_url, schema=schema)
    archive_path = values.get(LEDGER_ARCHIVE_PATH_ENV, "").strip()
    if archive_path:
        from .portfolio_engine.archived_store import ArchivedLedgerStore

        return ArchivedLedgerStore(live, archive_path)
    return live


__all__ = [
    "DEFAULT_POSTGRES_SCHEMA",
    "DEFAULT_JSON_LEDGER_MAX_BYTES",
    "JSON_LEDGER_MAX_BYTES_ENV",
    "LEDGER_BACKEND_ENV",
    "LEDGER_ARCHIVE_PATH_ENV",
    "LEDGER_DATABASE_URL_ENV",
    "LEDGER_SCHEMA_ENV",
    "open_portfolio_store",
]
