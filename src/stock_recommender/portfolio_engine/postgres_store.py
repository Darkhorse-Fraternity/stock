"""Optional PostgreSQL adapter for the canonical portfolio ledger.

Psycopg is intentionally imported only while constructing or using this
adapter. JSON-only deployments therefore keep their zero-dependency runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from .contracts import (
    AccountSnapshot,
    DecisionBatch,
    PortfolioLedgerView,
    PortfolioPerformanceLedgerView,
    RevisionTransition,
)
from .ledger import JsonLedgerStore, LEDGER_SCHEMA_VERSION, validate_ledger_payload


_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
_T = TypeVar("_T")


def _driver():
    try:
        import psycopg
        from psycopg import sql
        from psycopg.types.json import Jsonb
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "PostgresLedgerStore requires the 'integration' optional dependency"
        ) from exc
    return psycopg, sql, Jsonb


def _schema_name(value: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("schema must be a valid PostgreSQL identifier")
    if value.startswith("pg_") or value == "information_schema":
        raise ValueError("system PostgreSQL schemas are not valid ledger targets")
    return value


class PostgresLedgerStore(JsonLedgerStore):
    """Transactional PostgreSQL implementation of the current LedgerStore port.

    The canonical JSON graph remains the single persistence contract. All
    mutation semantics are delegated to ``JsonLedgerStore`` so JSON, memory,
    backtest, and PostgreSQL adapters share the same strict validation. A
    database advisory lock and row lock serialize the read/validate/write
    transition, while ``committed_runs`` adds a database-enforced
    ``(strategy_id, run_key)`` uniqueness boundary.
    """

    def __init__(self, database_url: str, *, schema: str) -> None:
        if type(database_url) is not str or not database_url:
            raise ValueError("database_url must be a non-empty string")
        self._database_url = database_url
        self.schema = _schema_name(schema)
        # The parent transaction guard expects a path. It is only an additional
        # local-process mutex; no portfolio data is written to this file.
        digest = hashlib.sha256(self.schema.encode("utf-8")).hexdigest()[:20]
        super().__init__(Path("/tmp") / f"stock-agent-postgres-{digest}.lock")
        self._local = threading.local()
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        psycopg, sql, Jsonb = _driver()
        schema = sql.Identifier(self.schema)
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.ledger_state (
                            singleton SMALLINT PRIMARY KEY CHECK (singleton = 1),
                            schema_version INTEGER NOT NULL,
                            payload JSONB NOT NULL
                        )
                        """
                    ).format(schema)
                )
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.committed_runs (
                            strategy_id TEXT NOT NULL,
                            run_key TEXT NOT NULL,
                            request_fingerprint TEXT,
                            batch_fingerprint TEXT NOT NULL,
                            PRIMARY KEY (strategy_id, run_key)
                        )
                        """
                    ).format(schema)
                )
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.ledger_state
                            (singleton, schema_version, payload)
                        VALUES (1, %s, %s)
                        ON CONFLICT (singleton) DO NOTHING
                        """
                    ).format(schema),
                    (
                        LEDGER_SCHEMA_VERSION,
                        Jsonb(
                            {
                                "version": LEDGER_SCHEMA_VERSION,
                                "accounts": {},
                            }
                        ),
                    ),
                )

    def _active_connection(self):
        connection = getattr(self._local, "connection", None)
        if connection is None:
            raise RuntimeError("PostgreSQL ledger operation has no active transaction")
        return connection

    def _transaction(self, operation: Callable[[], _T]) -> _T:
        if getattr(self._local, "connection", None) is not None:
            return operation()
        psycopg, _sql, _jsonb = _driver()
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                # The lock key is sent as a value, never interpolated SQL.
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.schema,))
            self._local.connection = connection
            try:
                return operation()
            finally:
                del self._local.connection

    def _read_store_payload(
        self,
        *,
        decoded_run_results: dict[str, tuple[DecisionBatch, ...]] | None = None,
    ) -> dict[str, Any]:
        _psycopg, sql, _jsonb = _driver()
        connection = self._active_connection()
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT schema_version, payload "
                    "FROM {}.ledger_state WHERE singleton = 1 FOR UPDATE"
                ).format(sql.Identifier(self.schema))
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL ledger state row is missing")
        schema_version, raw_payload = row
        if schema_version != LEDGER_SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported PostgreSQL ledger schema version: {schema_version}"
            )
        payload = (
            json.loads(raw_payload)
            if isinstance(raw_payload, str)
            else dict(raw_payload)
        )
        validate_ledger_payload(
            payload,
            decoded_run_results=decoded_run_results,
        )
        return payload

    def _write_store_payload(self, payload: Mapping[str, Any]) -> None:
        _psycopg, sql, Jsonb = _driver()
        connection = self._active_connection()
        schema = sql.Identifier(self.schema)
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    UPDATE {}.ledger_state
                    SET schema_version = %s, payload = %s
                    WHERE singleton = 1
                    """
                ).format(schema),
                (LEDGER_SCHEMA_VERSION, Jsonb(dict(payload))),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("PostgreSQL ledger state update was not applied")
            for strategy_id, account in payload["accounts"].items():
                for committed in account["committed_batches"]:
                    run_key = committed["run_key"]
                    request_fingerprint = next(
                        (
                            result.get("request_fingerprint")
                            for result in account["run_results"]
                            if result.get("run_key") == run_key
                        ),
                        None,
                    )
                    cursor.execute(
                        sql.SQL(
                            """
                            INSERT INTO {}.committed_runs
                                (strategy_id, run_key, request_fingerprint, batch_fingerprint)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (strategy_id, run_key) DO NOTHING
                            """
                        ).format(schema),
                        (
                            strategy_id,
                            run_key,
                            request_fingerprint,
                            committed["fingerprint"],
                        ),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            sql.SQL(
                                """
                                SELECT request_fingerprint, batch_fingerprint
                                FROM {}.committed_runs
                                WHERE strategy_id = %s AND run_key = %s
                                """
                            ).format(schema),
                            (strategy_id, run_key),
                        )
                        existing = cursor.fetchone()
                        if existing != (
                            request_fingerprint,
                            committed["fingerprint"],
                        ):
                            raise RuntimeError(
                                "database run-key constraint conflicts with ledger facts"
                            )

    def create_account(self, account: AccountSnapshot) -> AccountSnapshot:
        parent = super()
        return self._transaction(lambda: parent.create_account(account))

    def load(self, strategy_id: str) -> AccountSnapshot:
        parent = super()
        return self._transaction(lambda: parent.load(strategy_id))

    def load_view(self, strategy_id: str) -> PortfolioLedgerView:
        parent = super()
        return self._transaction(lambda: parent.load_view(strategy_id))

    def load_performance_view(
        self,
        strategy_id: str,
    ) -> PortfolioPerformanceLedgerView:
        parent = super()
        return self._transaction(lambda: parent.load_performance_view(strategy_id))

    def transition_revision(
        self,
        transition: RevisionTransition,
    ) -> AccountSnapshot:
        parent = super()
        return self._transaction(lambda: parent.transition_revision(transition))

    def load_committed_batch(
        self,
        strategy_id: str,
        run_key: str,
        request_fingerprint: str,
    ) -> DecisionBatch | None:
        parent = super()
        return self._transaction(
            lambda: parent.load_committed_batch(
                strategy_id,
                run_key,
                request_fingerprint,
            )
        )

    def commit(self, batch: DecisionBatch) -> AccountSnapshot:
        parent = super()
        return self._transaction(lambda: parent.commit(batch))

    def list_accounts(self) -> tuple[AccountSnapshot, ...]:
        parent = super()
        return self._transaction(parent.list_accounts)


__all__ = ["PostgresLedgerStore"]
