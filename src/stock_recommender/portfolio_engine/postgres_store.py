"""Normalized optional PostgreSQL portfolio ledger adapter.

The adapter is independent from the JSON ledger. Psycopg remains a lazy
optional dependency so JSON-only production deployments are unaffected.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import datetime
from typing import Any

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
    PortfolioEvent,
    PortfolioLedgerView,
    PortfolioPerformanceLedgerView,
    PositionSide,
    PositionSnapshot,
    PositionEffect,
    PositionRiskUpdate,
    PositionSettlementUpdate,
    RevisionTransition,
)
from .canonical import canonical_graph
from .ledger import (
    LedgerError,
    StalePortfolioSnapshotError,
    _RevisionTransitionFact,
    _apply_carry,
    _apply_events,
    _apply_progress,
    _apply_risk_updates,
    _apply_settlement_updates,
    _batch_from_canonical_json,
    _batch_fingerprint,
    _canonical_batch_facts,
    _carry_from_json,
    _carry_to_json,
    _derived_revision_transition_event,
    _event_from_json,
    _event_to_json,
    _execution_account_fact,
    _intent_from_json,
    _intent_to_json,
    _progress_fill_from_json,
    _progress_fill_to_json,
    _progress_from_json,
    _progress_to_json,
    _reconcile_batch_events,
    _revision_transition_result_snapshot_id,
    _risk_fact_for_batch,
    _risk_update_from_json,
    _risk_update_to_json,
    _stable_snapshot_id,
    _validate_carry_event_pairs,
    _validate_fill_summaries,
    _validate_account,
    _verify_execution_account,
)


_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,62}\Z")


def _driver():
    try:
        import psycopg
        from psycopg import sql
        from psycopg.types.json import Jsonb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "PostgresLedgerStore requires the 'integration' optional dependency"
        ) from exc
    return psycopg, sql, Jsonb


def _schema_name(value: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("schema must be a valid PostgreSQL identifier")
    if value.startswith("pg_") or value == "information_schema":
        raise ValueError("system PostgreSQL schemas are not ledger targets")
    return value


def _public_ordinal(item: Any, items: tuple, *, fact_kind: str) -> int | None:
    matches = [ordinal for ordinal, candidate in enumerate(items) if candidate == item]
    if len(matches) > 1:
        raise LedgerError(f"duplicate public {fact_kind} facts are not supported")
    return None if not matches else matches[0]


def _require_contiguous_ordinals(rows: tuple, *, table: str) -> None:
    ordinals = tuple(int(row[0]) for row in rows)
    if ordinals != tuple(range(len(rows))):
        raise LedgerError(f"{table} ordinals are not contiguous")


def _public_items(rows: tuple[tuple[int | None, Any], ...], *, table: str) -> tuple:
    selected = sorted(
        ((ordinal, item) for ordinal, item in rows if ordinal is not None),
        key=lambda pair: pair[0],
    )
    ordinals = tuple(int(pair[0]) for pair in selected)
    if ordinals != tuple(range(len(selected))):
        raise LedgerError(f"{table} public ordinals are not contiguous")
    return tuple(item for _ordinal, item in selected)


def _settlement_from_json(value: object) -> PositionSettlementUpdate:
    if not isinstance(value, dict):
        raise LedgerError("invalid settlement update payload")
    try:
        sellable_on = value["sellable_on"]
        return PositionSettlementUpdate(
            symbol=value["symbol"],
            side=PositionSide(value["side"]),
            quantity=value["quantity"],
            sellable_quantity=value["sellable_quantity"],
            sellable_on=(
                None
                if sellable_on is None
                else datetime.fromisoformat(f"{sellable_on}T00:00:00").date()
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LedgerError("invalid settlement update payload") from exc


def _execution_fill_from_detail(detail: ExecutionProgressFill) -> ExecutionFill:
    return ExecutionFill(
        intent_id=detail.intent_id,
        symbol=detail.symbol,
        quantity=detail.quantity,
        price=detail.price,
        fees=detail.fees,
        status=detail.status,
    )


def _batch_metadata_payload(batch: DecisionBatch) -> dict:
    return canonical_graph(
        replace(
            batch,
            intents=(),
            fills=(),
            events=(),
            execution_progress=(),
            position_risk_updates=(),
            position_settlement_updates=(),
            carry_accruals=(),
        )
    )


_DDL = (
    """
    CREATE TABLE IF NOT EXISTS {schema}.accounts (
        strategy_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL UNIQUE,
        strategy_revision INTEGER NOT NULL CHECK (strategy_revision > 0),
        occurred_at TIMESTAMPTZ NOT NULL,
        available_cash DOUBLE PRECISION NOT NULL,
        reserved_cash DOUBLE PRECISION NOT NULL,
        restricted_short_proceeds DOUBLE PRECISION NOT NULL,
        margin_loan DOUBLE PRECISION NOT NULL,
        accrued_financing_cost DOUBLE PRECISION NOT NULL,
        accrued_borrow_cost DOUBLE PRECISION NOT NULL,
        snapshot_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.positions (
        strategy_id TEXT NOT NULL REFERENCES {schema}.accounts(strategy_id)
            ON DELETE CASCADE,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('LONG', 'SHORT')),
        quantity INTEGER NOT NULL CHECK (quantity > 0),
        average_cost DOUBLE PRECISION NOT NULL,
        current_price DOUBLE PRECISION,
        peak_price DOUBLE PRECISION,
        trough_price DOUBLE PRECISION,
        trailing_active BOOLEAN NOT NULL,
        position_mode TEXT NOT NULL CHECK (position_mode IN ('NORMAL', 'COVER_ONLY')),
        sellable_quantity INTEGER,
        sellable_on DATE,
        PRIMARY KEY (strategy_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.borrow_lifecycles (
        strategy_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        lifecycle_id TEXT NOT NULL,
        started_on DATE NOT NULL,
        PRIMARY KEY (strategy_id, symbol),
        UNIQUE (strategy_id, lifecycle_id),
        FOREIGN KEY (strategy_id, symbol)
            REFERENCES {schema}.positions(strategy_id, symbol) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.financing_lifecycles (
        strategy_id TEXT PRIMARY KEY REFERENCES {schema}.accounts(strategy_id)
            ON DELETE CASCADE,
        lifecycle_id TEXT NOT NULL,
        started_on DATE NOT NULL,
        UNIQUE (strategy_id, lifecycle_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.committed_runs (
        strategy_id TEXT NOT NULL REFERENCES {schema}.accounts(strategy_id)
            ON DELETE CASCADE,
        run_key TEXT NOT NULL,
        commit_ordinal BIGINT GENERATED ALWAYS AS IDENTITY,
        strategy_revision INTEGER NOT NULL,
        source_snapshot_id TEXT NOT NULL,
        result_snapshot_id TEXT NOT NULL,
        market_snapshot_id TEXT NOT NULL,
        request_fingerprint TEXT,
        batch_fingerprint TEXT NOT NULL,
        committed_at TIMESTAMPTZ NOT NULL,
        metadata_payload JSONB NOT NULL,
        batch_payload JSONB NOT NULL,
        PRIMARY KEY (strategy_id, run_key),
        UNIQUE (strategy_id, commit_ordinal),
        UNIQUE (strategy_id, run_key, batch_fingerprint)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.order_intents (
        strategy_id TEXT NOT NULL,
        intent_id TEXT NOT NULL,
        run_key TEXT NOT NULL,
        strategy_revision INTEGER NOT NULL,
        ordinal INTEGER NOT NULL,
        batch_ordinal INTEGER,
        symbol TEXT NOT NULL,
        position_side TEXT NOT NULL,
        order_side TEXT NOT NULL,
        position_effect TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        reason TEXT NOT NULL,
        created_snapshot_id TEXT NOT NULL,
        created_market_at TIMESTAMPTZ NOT NULL,
        cancelled_revision INTEGER,
        payload JSONB NOT NULL,
        PRIMARY KEY (strategy_id, intent_id),
        UNIQUE (strategy_id, run_key, ordinal),
        FOREIGN KEY (strategy_id, run_key)
            REFERENCES {schema}.committed_runs(strategy_id, run_key) ON DELETE CASCADE,
        FOREIGN KEY (strategy_id)
            REFERENCES {schema}.accounts(strategy_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.execution_progress (
        strategy_id TEXT NOT NULL,
        run_key TEXT NOT NULL,
        intent_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        batch_ordinal INTEGER,
        status TEXT NOT NULL,
        last_occurred_at TIMESTAMPTZ NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (strategy_id, run_key, intent_id),
        UNIQUE (strategy_id, run_key, ordinal),
        FOREIGN KEY (strategy_id, run_key)
            REFERENCES {schema}.committed_runs(strategy_id, run_key) ON DELETE CASCADE,
        FOREIGN KEY (strategy_id, intent_id)
            REFERENCES {schema}.order_intents(strategy_id, intent_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.fills (
        strategy_id TEXT NOT NULL,
        progress_fill_id TEXT NOT NULL,
        run_key TEXT NOT NULL,
        intent_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        batch_ordinal INTEGER,
        symbol TEXT NOT NULL,
        position_side TEXT NOT NULL,
        order_side TEXT NOT NULL,
        snapshot_id TEXT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        quantity INTEGER NOT NULL,
        price DOUBLE PRECISION NOT NULL,
        fees DOUBLE PRECISION NOT NULL,
        commission DOUBLE PRECISION NOT NULL,
        status TEXT NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (strategy_id, progress_fill_id),
        UNIQUE (strategy_id, run_key, ordinal),
        FOREIGN KEY (strategy_id, run_key)
            REFERENCES {schema}.committed_runs(strategy_id, run_key) ON DELETE CASCADE,
        FOREIGN KEY (strategy_id, intent_id)
            REFERENCES {schema}.order_intents(strategy_id, intent_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.position_risk_facts (
        strategy_id TEXT NOT NULL,
        fact_id TEXT NOT NULL,
        run_key TEXT NOT NULL,
        strategy_revision INTEGER NOT NULL,
        ordinal INTEGER NOT NULL,
        batch_ordinal INTEGER,
        portfolio_snapshot_id TEXT NOT NULL,
        market_snapshot_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (strategy_id, fact_id),
        UNIQUE (strategy_id, run_key, ordinal),
        FOREIGN KEY (strategy_id, run_key)
            REFERENCES {schema}.committed_runs(strategy_id, run_key) ON DELETE CASCADE,
        FOREIGN KEY (strategy_id)
            REFERENCES {schema}.accounts(strategy_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.position_risk_updates (
        strategy_id TEXT NOT NULL,
        fact_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        peak_price DOUBLE PRECISION,
        trough_price DOUBLE PRECISION,
        trailing_active BOOLEAN NOT NULL,
        position_mode TEXT NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (strategy_id, fact_id),
        FOREIGN KEY (strategy_id, fact_id)
            REFERENCES {schema}.position_risk_facts(strategy_id, fact_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.settlement_updates (
        strategy_id TEXT NOT NULL,
        run_key TEXT NOT NULL,
        symbol TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        batch_ordinal INTEGER,
        side TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        sellable_quantity INTEGER,
        sellable_on DATE,
        payload JSONB NOT NULL,
        PRIMARY KEY (strategy_id, run_key, symbol),
        UNIQUE (strategy_id, run_key, ordinal),
        FOREIGN KEY (strategy_id, run_key)
            REFERENCES {schema}.committed_runs(strategy_id, run_key) ON DELETE CASCADE,
        FOREIGN KEY (strategy_id)
            REFERENCES {schema}.accounts(strategy_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.carry_accruals (
        strategy_id TEXT NOT NULL,
        accrual_id TEXT NOT NULL,
        run_key TEXT,
        ordinal INTEGER NOT NULL,
        batch_ordinal INTEGER,
        account_id TEXT NOT NULL,
        cost_type TEXT NOT NULL,
        lifecycle_id TEXT NOT NULL,
        accrual_date DATE NOT NULL,
        elapsed_days INTEGER NOT NULL,
        amount DOUBLE PRECISION NOT NULL,
        symbol TEXT,
        payload JSONB NOT NULL,
        PRIMARY KEY (strategy_id, accrual_id),
        UNIQUE (strategy_id, cost_type, lifecycle_id, accrual_date, symbol),
        FOREIGN KEY (strategy_id, run_key)
            REFERENCES {schema}.committed_runs(strategy_id, run_key) ON DELETE CASCADE,
        FOREIGN KEY (strategy_id)
            REFERENCES {schema}.accounts(strategy_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.events (
        strategy_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        run_key TEXT,
        strategy_revision INTEGER NOT NULL,
        ordinal INTEGER NOT NULL,
        batch_ordinal INTEGER,
        source_kind TEXT NOT NULL CHECK (source_kind IN ('BATCH', 'DERIVED', 'SYSTEM')),
        event_ordinal BIGINT GENERATED ALWAYS AS IDENTITY,
        event_type TEXT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (strategy_id, event_id),
        UNIQUE (event_ordinal),
        UNIQUE (strategy_id, run_key, ordinal),
        FOREIGN KEY (strategy_id, run_key)
            REFERENCES {schema}.committed_runs(strategy_id, run_key) ON DELETE CASCADE,
        FOREIGN KEY (strategy_id)
            REFERENCES {schema}.accounts(strategy_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.revision_transitions (
        strategy_id TEXT NOT NULL REFERENCES {schema}.accounts(strategy_id)
            ON DELETE CASCADE,
        transition_id TEXT NOT NULL,
        from_revision INTEGER NOT NULL,
        to_revision INTEGER NOT NULL,
        source_snapshot_id TEXT NOT NULL,
        result_snapshot_id TEXT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        cancelled_intent_ids JSONB NOT NULL,
        PRIMARY KEY (strategy_id, transition_id),
        UNIQUE (strategy_id, from_revision, to_revision)
    )
    """,
)


class PostgresLedgerStore:
    """Normalized SQL implementation of the current LedgerStore protocol."""

    def __init__(self, database_url: str, *, schema: str) -> None:
        if type(database_url) is not str or not database_url:
            raise ValueError("database_url must be a non-empty string")
        self._database_url = database_url
        self.schema = _schema_name(schema)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        psycopg, sql, _Jsonb = _driver()
        identifier = sql.Identifier(self.schema)
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                for statement in _DDL:
                    cursor.execute(
                        sql.SQL(statement).format(schema=identifier)
                    )

    def _load_account_cursor(self, cursor: Any, strategy_id: str) -> AccountSnapshot:
        _psycopg, sql, _Jsonb = _driver()
        schema = sql.Identifier(self.schema)
        cursor.execute(
            sql.SQL(
                """
                SELECT account_id, strategy_revision, occurred_at,
                       available_cash, reserved_cash, restricted_short_proceeds,
                       margin_loan, accrued_financing_cost, accrued_borrow_cost,
                       snapshot_id
                FROM {}.accounts WHERE strategy_id = %s
                """
            ).format(schema),
            (strategy_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(f"portfolio account not found: {strategy_id}")
        cursor.execute(
            sql.SQL(
                """
                SELECT p.symbol, p.side, p.quantity, p.average_cost,
                       p.current_price, p.peak_price, p.trough_price,
                       p.trailing_active, p.position_mode,
                       p.sellable_quantity, p.sellable_on,
                       b.lifecycle_id, b.started_on
                FROM {}.positions AS p
                LEFT JOIN {}.borrow_lifecycles AS b
                  ON b.strategy_id = p.strategy_id AND b.symbol = p.symbol
                WHERE p.strategy_id = %s ORDER BY p.symbol
                """
            ).format(schema, schema),
            (strategy_id,),
        )
        positions = tuple(
            PositionSnapshot(
                symbol=item[0],
                side=PositionSide(item[1]),
                quantity=item[2],
                average_cost=item[3],
                current_price=item[4],
                peak_price=item[5],
                trough_price=item[6],
                trailing_active=item[7],
                position_mode=item[8],
                sellable_quantity=item[9],
                sellable_on=item[10],
                borrow_lifecycle=(
                    None
                    if item[11] is None
                    else AccrualLifecycle(id=item[11], started_on=item[12])
                ),
            )
            for item in cursor.fetchall()
        )
        cursor.execute(
            sql.SQL(
                "SELECT lifecycle_id, started_on "
                "FROM {}.financing_lifecycles WHERE strategy_id = %s"
            ).format(schema),
            (strategy_id,),
        )
        financing_row = cursor.fetchone()
        financing = (
            None
            if financing_row is None
            else AccrualLifecycle(
                id=financing_row[0],
                started_on=financing_row[1],
            )
        )
        cursor.execute(
            sql.SQL(
                """
                SELECT c.account_id, c.cost_type, c.accrual_date, c.elapsed_days,
                       c.amount, c.lifecycle_id, c.symbol
                FROM {}.carry_accruals AS c
                LEFT JOIN {}.committed_runs AS r
                  ON r.strategy_id=c.strategy_id AND r.run_key=c.run_key
                WHERE c.strategy_id = %s
                ORDER BY (c.run_key IS NOT NULL), r.commit_ordinal,
                         c.ordinal, c.accrual_id
                """
            ).format(schema, schema),
            (strategy_id,),
        )
        carry = tuple(
            CarryAccrualRecord(
                account_id=item[0],
                cost_type=CarryCostType(item[1]),
                accrual_date=item[2],
                elapsed_days=item[3],
                amount=item[4],
                lifecycle_id=item[5],
                symbol=item[6],
            )
            for item in cursor.fetchall()
        )
        account = AccountSnapshot(
            id=row[0],
            strategy_id=strategy_id,
            strategy_revision=row[1],
            occurred_at=row[2],
            available_cash=row[3],
            reserved_cash=row[4],
            restricted_short_proceeds=row[5],
            margin_loan=row[6],
            accrued_financing_cost=row[7],
            accrued_borrow_cost=row[8],
            snapshot_id=row[9],
            positions=positions,
            carry_accruals=carry,
            financing_lifecycle=financing,
        )
        _validate_account(account)
        return account

    def _write_current_account(self, cursor: Any, account: AccountSnapshot) -> None:
        _psycopg, sql, _Jsonb = _driver()
        schema = sql.Identifier(self.schema)
        cursor.execute(
            sql.SQL(
                """
                UPDATE {}.accounts SET
                    account_id=%s, strategy_revision=%s, occurred_at=%s,
                    available_cash=%s, reserved_cash=%s,
                    restricted_short_proceeds=%s, margin_loan=%s,
                    accrued_financing_cost=%s, accrued_borrow_cost=%s,
                    snapshot_id=%s
                WHERE strategy_id=%s
                """
            ).format(schema),
            (
                account.id,
                account.strategy_revision,
                account.occurred_at,
                account.available_cash,
                account.reserved_cash,
                account.restricted_short_proceeds,
                account.margin_loan,
                account.accrued_financing_cost,
                account.accrued_borrow_cost,
                account.snapshot_id or account.id,
                account.strategy_id,
            ),
        )
        cursor.execute(
            sql.SQL("DELETE FROM {}.positions WHERE strategy_id=%s").format(schema),
            (account.strategy_id,),
        )
        for position in account.positions:
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.positions VALUES
                    (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """
                ).format(schema),
                (
                    account.strategy_id,
                    position.symbol,
                    position.side.value,
                    position.quantity,
                    position.average_cost,
                    position.current_price,
                    position.peak_price,
                    position.trough_price,
                    position.trailing_active,
                    position.position_mode,
                    position.sellable_quantity,
                    position.sellable_on,
                ),
            )
            if position.borrow_lifecycle is not None:
                cursor.execute(
                    sql.SQL(
                        "INSERT INTO {}.borrow_lifecycles VALUES (%s,%s,%s,%s)"
                    ).format(schema),
                    (
                        account.strategy_id,
                        position.symbol,
                        position.borrow_lifecycle.id,
                        position.borrow_lifecycle.started_on,
                    ),
                )
        cursor.execute(
            sql.SQL(
                "DELETE FROM {}.financing_lifecycles WHERE strategy_id=%s"
            ).format(schema),
            (account.strategy_id,),
        )
        if account.financing_lifecycle is not None:
            cursor.execute(
                sql.SQL(
                    "INSERT INTO {}.financing_lifecycles VALUES (%s,%s,%s)"
                ).format(schema),
                (
                    account.strategy_id,
                    account.financing_lifecycle.id,
                    account.financing_lifecycle.started_on,
                ),
            )

    def create_account(self, account: AccountSnapshot) -> AccountSnapshot:
        if type(account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        if not account.snapshot_id:
            raise LedgerError("account creation requires an explicit snapshot_id")
        _validate_account(account)
        psycopg, sql, Jsonb = _driver()
        schema = sql.Identifier(self.schema)
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        "SELECT strategy_id FROM {}.accounts "
                        "WHERE strategy_id=%s FOR UPDATE"
                    ).format(schema),
                    (account.strategy_id,),
                )
                if cursor.fetchone() is not None:
                    existing = self._load_account_cursor(cursor, account.strategy_id)
                    if existing != account:
                        raise LedgerError(
                            "account creation collision with different persistent content"
                        )
                    return existing
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.accounts VALUES
                        (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """
                    ).format(schema),
                    (
                        account.strategy_id,
                        account.id,
                        account.strategy_revision,
                        account.occurred_at,
                        account.available_cash,
                        account.reserved_cash,
                        account.restricted_short_proceeds,
                        account.margin_loan,
                        account.accrued_financing_cost,
                        account.accrued_borrow_cost,
                        account.snapshot_id,
                    ),
                )
                self._write_current_account(cursor, account)
                carry_keys = [item.idempotency_key for item in account.carry_accruals]
                if len(set(carry_keys)) != len(carry_keys):
                    raise LedgerError("account carry history contains duplicate facts")
                for ordinal, accrual in enumerate(account.carry_accruals):
                    if accrual.account_id != account.id:
                        raise LedgerError("account carry history has a different account_id")
                    material = "|".join(str(item) for item in accrual.idempotency_key)
                    accrual_id = "carry-" + hashlib.sha256(
                        material.encode("utf-8")
                    ).hexdigest()[:24]
                    cursor.execute(
                        sql.SQL(
                            """
                            INSERT INTO {}.carry_accruals
                            (strategy_id,accrual_id,run_key,ordinal,batch_ordinal,
                             account_id,cost_type,lifecycle_id,accrual_date,
                             elapsed_days,amount,symbol,payload)
                            VALUES (%s,%s,NULL,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s)
                            """
                        ).format(schema),
                        (
                            account.strategy_id,
                            accrual_id,
                            ordinal,
                            accrual.account_id,
                            accrual.cost_type.value,
                            accrual.lifecycle_id,
                            accrual.accrual_date,
                            accrual.elapsed_days,
                            accrual.amount,
                            accrual.symbol,
                            Jsonb(_carry_to_json(accrual)),
                        ),
                    )
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
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.events
                        (strategy_id,event_id,run_key,strategy_revision,ordinal,
                         batch_ordinal,source_kind,event_type,occurred_at,payload)
                        VALUES (%s,%s,NULL,%s,0,NULL,'SYSTEM',%s,%s,%s)
                        """
                    ).format(schema),
                    (
                        account.strategy_id,
                        opened.id,
                        account.strategy_revision,
                        opened.type,
                        opened.occurred_at,
                        Jsonb(_event_to_json(opened)),
                    ),
                )
        return account

    def load(self, strategy_id: str) -> AccountSnapshot:
        if type(strategy_id) is not str or not strategy_id:
            raise ValueError("strategy_id must be a non-empty string")
        psycopg, _sql, _Jsonb = _driver()
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                return self._load_account_cursor(cursor, strategy_id)

    def _load_intents_progress_events(self, cursor: Any, strategy_id: str):
        _psycopg, sql, _Jsonb = _driver()
        schema = sql.Identifier(self.schema)
        cursor.execute(
            sql.SQL(
                "SELECT i.payload, i.cancelled_revision "
                "FROM {}.order_intents AS i JOIN {}.committed_runs AS r "
                "ON r.strategy_id=i.strategy_id AND r.run_key=i.run_key "
                "WHERE i.strategy_id=%s ORDER BY r.commit_ordinal,"
                "i.ordinal,i.intent_id"
            ).format(schema, schema),
            (strategy_id,),
        )
        intent_rows = cursor.fetchall()
        intents = tuple(_intent_from_json(item[0]) for item in intent_rows)
        active_ids = {
            intent.id
            for intent, row in zip(intents, intent_rows)
            if row[1] is None
        }
        cursor.execute(
            sql.SQL(
                """
                SELECT intent_id, payload FROM (
                    SELECT p.intent_id, p.payload, r.commit_ordinal, p.ordinal,
                           ROW_NUMBER() OVER (
                               PARTITION BY p.intent_id
                               ORDER BY r.commit_ordinal DESC, p.ordinal DESC
                           ) AS latest
                    FROM {}.execution_progress AS p
                    JOIN {}.committed_runs AS r
                      ON r.strategy_id=p.strategy_id AND r.run_key=p.run_key
                    WHERE p.strategy_id=%s
                ) AS ranked
                WHERE latest=1
                ORDER BY intent_id
                """
            ).format(schema, schema),
            (strategy_id,),
        )
        progress = tuple(_progress_from_json(item[1]) for item in cursor.fetchall())
        completed = {item.intent_id for item in progress if item.status == "FILLED"}
        open_intents = tuple(
            sorted(
                (
                    item
                    for item in intents
                    if item.id in active_ids - completed
                ),
                key=lambda item: item.id,
            )
        )
        open_progress = tuple(
            item for item in progress if item.intent_id in {x.id for x in open_intents}
        )
        cursor.execute(
            sql.SQL(
                "SELECT e.payload FROM {}.events AS e "
                "WHERE e.strategy_id=%s ORDER BY e.event_ordinal"
            ).format(schema),
            (strategy_id,),
        )
        events = tuple(_event_from_json(item[0]) for item in cursor.fetchall())
        return intents, progress, open_intents, open_progress, events

    def _load_normalized_batch_cursor(self, cursor: Any, row: tuple) -> DecisionBatch:
        (
            strategy_id,
            run_key,
            _commit_ordinal,
            strategy_revision,
            source_snapshot_id,
            _result_snapshot_id,
            market_snapshot_id,
            request_fingerprint,
            batch_fingerprint,
            metadata_payload,
            audit_payload,
        ) = row
        skeleton = _batch_from_canonical_json(metadata_payload)
        if any(
            (
                skeleton.intents,
                skeleton.fills,
                skeleton.events,
                skeleton.execution_progress,
                skeleton.position_risk_updates,
                skeleton.position_settlement_updates,
                skeleton.carry_accruals,
            )
        ):
            raise LedgerError("committed run metadata contains normalized facts")
        if (
            skeleton.strategy_id != strategy_id
            or skeleton.run_key != run_key
            or skeleton.strategy_revision != strategy_revision
            or skeleton.portfolio_snapshot_id != source_snapshot_id
            or skeleton.market_snapshot_id != market_snapshot_id
            or skeleton.request_fingerprint != request_fingerprint
        ):
            raise LedgerError("committed run metadata does not match SQL columns")

        _psycopg, sql, _Jsonb = _driver()
        schema = sql.Identifier(self.schema)
        key = (strategy_id, run_key)

        cursor.execute(
            sql.SQL(
                """
                SELECT ordinal,batch_ordinal,intent_id,strategy_revision,symbol,
                       position_side,order_side,position_effect,quantity,reason,
                       created_snapshot_id,created_market_at,payload
                FROM {}.order_intents
                WHERE strategy_id=%s AND run_key=%s
                ORDER BY ordinal
                """
            ).format(schema),
            key,
        )
        intent_rows = tuple(cursor.fetchall())
        _require_contiguous_ordinals(intent_rows, table="order_intents")
        intents = []
        intent_public = []
        for item in intent_rows:
            intent = _intent_from_json(item[12])
            if item[12] != _intent_to_json(intent) or (
                intent.id,
                strategy_revision,
                intent.symbol,
                intent.position_side.value,
                intent.order_side.value,
                intent.position_effect.value,
                intent.quantity,
                intent.reason,
                intent.created_snapshot_id,
                intent.created_market_at,
            ) != item[2:12]:
                raise LedgerError("order_intents columns differ from payload")
            intents.append(intent)
            intent_public.append((item[1], intent))

        cursor.execute(
            sql.SQL(
                """
                SELECT ordinal,batch_ordinal,intent_id,status,last_occurred_at,payload
                FROM {}.execution_progress
                WHERE strategy_id=%s AND run_key=%s
                ORDER BY ordinal
                """
            ).format(schema),
            key,
        )
        progress_rows = tuple(cursor.fetchall())
        _require_contiguous_ordinals(progress_rows, table="execution_progress")
        progress = []
        progress_public = []
        for item in progress_rows:
            entry = _progress_from_json(item[5])
            if item[5] != _progress_to_json(entry) or (
                entry.intent_id,
                entry.status,
                entry.fills[-1].occurred_at,
            ) != item[2:5]:
                raise LedgerError("execution_progress columns differ from payload")
            progress.append(entry)
            progress_public.append((item[1], entry))

        cursor.execute(
            sql.SQL(
                """
                SELECT ordinal,batch_ordinal,progress_fill_id,intent_id,symbol,
                       position_side,order_side,snapshot_id,occurred_at,quantity,
                       price,fees,commission,status,payload
                FROM {}.fills
                WHERE strategy_id=%s AND run_key=%s
                ORDER BY ordinal
                """
            ).format(schema),
            key,
        )
        fill_rows = tuple(cursor.fetchall())
        _require_contiguous_ordinals(fill_rows, table="fills")
        fill_details = []
        fill_summaries = []
        fill_public = []
        for item in fill_rows:
            detail = _progress_fill_from_json(item[14])
            if item[14] != _progress_fill_to_json(detail) or (
                detail.id,
                detail.intent_id,
                detail.symbol,
                detail.position_side.value,
                detail.order_side.value,
                detail.snapshot_id,
                detail.occurred_at,
                detail.quantity,
                detail.price,
                detail.fees,
                detail.commission,
                detail.status,
            ) != item[2:14]:
                raise LedgerError("fills columns differ from payload")
            summary = _execution_fill_from_detail(detail)
            fill_details.append(detail)
            fill_summaries.append(summary)
            fill_public.append((item[1], summary))

        cursor.execute(
            sql.SQL(
                """
                SELECT f.ordinal,f.batch_ordinal,f.fact_id,f.strategy_revision,
                       f.portfolio_snapshot_id,f.market_snapshot_id,f.symbol,
                       f.side,f.occurred_at,u.symbol,u.side,u.peak_price,
                       u.trough_price,u.trailing_active,u.position_mode,u.payload
                FROM {}.position_risk_facts AS f
                JOIN {}.position_risk_updates AS u
                  ON u.strategy_id=f.strategy_id AND u.fact_id=f.fact_id
                WHERE f.strategy_id=%s AND f.run_key=%s
                ORDER BY f.ordinal
                """
            ).format(schema, schema),
            key,
        )
        risk_rows = tuple(cursor.fetchall())
        _require_contiguous_ordinals(risk_rows, table="position_risk_facts")
        risk_updates = []
        risk_public = []
        for item in risk_rows:
            update = _risk_update_from_json(item[15])
            if item[15] != _risk_update_to_json(update) or (
                update.symbol,
                update.side.value,
                update.peak_price,
                update.trough_price,
                update.trailing_active,
                update.position_mode,
            ) != item[9:15]:
                raise LedgerError("position risk columns differ from payload")
            risk_updates.append(update)
            risk_public.append((item[1], update))

        cursor.execute(
            sql.SQL(
                """
                SELECT ordinal,batch_ordinal,symbol,side,quantity,
                       sellable_quantity,sellable_on,payload
                FROM {}.settlement_updates
                WHERE strategy_id=%s AND run_key=%s
                ORDER BY ordinal
                """
            ).format(schema),
            key,
        )
        settlement_rows = tuple(cursor.fetchall())
        _require_contiguous_ordinals(settlement_rows, table="settlement_updates")
        settlement = []
        settlement_public = []
        for item in settlement_rows:
            update = _settlement_from_json(item[7])
            expected_payload = {
                "symbol": update.symbol,
                "side": update.side.value,
                "quantity": update.quantity,
                "sellable_quantity": update.sellable_quantity,
                "sellable_on": (
                    None if update.sellable_on is None else update.sellable_on.isoformat()
                ),
            }
            if item[7] != expected_payload or (
                update.symbol,
                update.side.value,
                update.quantity,
                update.sellable_quantity,
                update.sellable_on,
            ) != item[2:7]:
                raise LedgerError("settlement columns differ from payload")
            settlement.append(update)
            settlement_public.append((item[1], update))

        cursor.execute(
            sql.SQL(
                """
                SELECT ordinal,batch_ordinal,account_id,cost_type,accrual_date,
                       elapsed_days,amount,lifecycle_id,symbol,payload
                FROM {}.carry_accruals
                WHERE strategy_id=%s AND run_key=%s
                ORDER BY ordinal
                """
            ).format(schema),
            key,
        )
        carry_rows = tuple(cursor.fetchall())
        _require_contiguous_ordinals(carry_rows, table="carry_accruals")
        carry = []
        carry_public = []
        for item in carry_rows:
            accrual = _carry_from_json(item[9])
            if item[9] != _carry_to_json(accrual) or (
                accrual.account_id,
                accrual.cost_type.value,
                accrual.accrual_date,
                accrual.elapsed_days,
                accrual.amount,
                accrual.lifecycle_id,
                accrual.symbol,
            ) != item[2:9]:
                raise LedgerError("carry columns differ from payload")
            carry.append(accrual)
            carry_public.append((item[1], accrual))

        cursor.execute(
            sql.SQL(
                """
                SELECT ordinal,batch_ordinal,source_kind,event_id,event_type,
                       occurred_at,payload
                FROM {}.events
                WHERE strategy_id=%s AND run_key=%s
                ORDER BY ordinal
                """
            ).format(schema),
            key,
        )
        event_rows = tuple(cursor.fetchall())
        _require_contiguous_ordinals(event_rows, table="events")
        stored_events = []
        batch_event_public = []
        for item in event_rows:
            event = _event_from_json(item[6])
            if item[6] != _event_to_json(event) or (
                event.id,
                event.type,
                event.occurred_at,
            ) != item[3:6]:
                raise LedgerError("event columns differ from payload")
            if item[2] == "BATCH":
                if item[1] is None:
                    raise LedgerError("batch event requires a public ordinal")
                batch_event_public.append((item[1], event))
            elif item[2] != "DERIVED" or item[1] is not None:
                raise LedgerError("batch-derived event has invalid source metadata")
            stored_events.append(event)

        batch = replace(
            skeleton,
            intents=_public_items(tuple(intent_public), table="order_intents"),
            fills=_public_items(tuple(fill_public), table="fills"),
            events=_public_items(tuple(batch_event_public), table="events"),
            execution_progress=_public_items(
                tuple(progress_public), table="execution_progress"
            ),
            position_risk_updates=_public_items(
                tuple(risk_public), table="position_risk_facts"
            ),
            position_settlement_updates=_public_items(
                tuple(settlement_public), table="settlement_updates"
            ),
            carry_accruals=_public_items(tuple(carry_public), table="carry_accruals"),
        )
        if _stable_snapshot_id(batch) != _result_snapshot_id:
            raise LedgerError("committed run result_snapshot_id is inconsistent")
        facts = _canonical_batch_facts(batch)
        normalized_facts = (
            tuple(intents),
            tuple(fill_summaries),
            tuple(progress),
            tuple(risk_updates),
            tuple(settlement),
            tuple(carry),
        )
        if facts != normalized_facts:
            raise LedgerError("normalized facts do not match batch metadata")

        risk_facts = []
        for item, update in zip(risk_rows, risk_updates):
            fact = _risk_fact_for_batch(batch, update, item[8])
            if (
                fact.fact_id,
                batch.strategy_revision,
                batch.portfolio_snapshot_id,
                batch.market_snapshot_id,
                update.symbol,
                update.side.value,
                fact.occurred_at,
            ) != item[2:9]:
                raise LedgerError("position risk fact columns are inconsistent")
            risk_facts.append(fact)
        expected_events = _reconcile_batch_events(
            batch,
            tuple(zip(fill_details, fill_summaries)),
            tuple(risk_facts),
        )
        if tuple(stored_events) != expected_events:
            raise LedgerError("stored events do not match reconstructed batch events")
        if canonical_graph(batch) != audit_payload:
            raise LedgerError("normalized batch differs from audit payload")
        if _batch_fingerprint(batch, facts) != batch_fingerprint:
            raise LedgerError("normalized batch fingerprint mismatch")
        return batch

    def _load_batches_cursor(self, cursor: Any, strategy_id: str) -> tuple[DecisionBatch, ...]:
        _psycopg, sql, _Jsonb = _driver()
        cursor.execute(
            sql.SQL(
                """
                SELECT strategy_id,run_key,commit_ordinal,strategy_revision,
                       source_snapshot_id,result_snapshot_id,market_snapshot_id,
                       request_fingerprint,batch_fingerprint,metadata_payload,
                       batch_payload
                FROM {}.committed_runs
                WHERE strategy_id=%s ORDER BY commit_ordinal
                """
            ).format(sql.Identifier(self.schema)),
            (strategy_id,),
        )
        return tuple(
            self._load_normalized_batch_cursor(cursor, row)
            for row in cursor.fetchall()
        )

    def load_view(self, strategy_id: str) -> PortfolioLedgerView:
        psycopg, _sql, _Jsonb = _driver()
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                account = self._load_account_cursor(cursor, strategy_id)
                _all_intents, _all_progress, intents, progress, events = (
                    self._load_intents_progress_events(cursor, strategy_id)
                )
        return PortfolioLedgerView(
            account=account,
            open_intents=intents,
            execution_progress=progress,
            recent_events=events[-100:],
        )

    def load_performance_view(
        self,
        strategy_id: str,
    ) -> PortfolioPerformanceLedgerView:
        psycopg, sql, _Jsonb = _driver()
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                account = self._load_account_cursor(cursor, strategy_id)
                intents, progress, _open, _open_progress, events = (
                    self._load_intents_progress_events(cursor, strategy_id)
                )
                batches = self._load_batches_cursor(cursor, strategy_id)
        return PortfolioPerformanceLedgerView(
            account=account,
            intents=intents,
            execution_progress=tuple(
                sorted(progress, key=lambda item: item.fills[-1].occurred_at)
            ),
            events=events,
            batches=batches,
        )

    def list_accounts(self) -> tuple[AccountSnapshot, ...]:
        psycopg, sql, _Jsonb = _driver()
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SELECT strategy_id FROM {}.accounts ORDER BY strategy_id").format(
                        sql.Identifier(self.schema)
                    )
                )
                strategy_ids = tuple(item[0] for item in cursor.fetchall())
                return tuple(
                    self._load_account_cursor(cursor, strategy_id)
                    for strategy_id in strategy_ids
                )

    def load_committed_batch(
        self,
        strategy_id: str,
        run_key: str,
        request_fingerprint: str,
    ) -> DecisionBatch | None:
        psycopg, sql, _Jsonb = _driver()
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        SELECT strategy_id,run_key,commit_ordinal,strategy_revision,
                               source_snapshot_id,result_snapshot_id,
                               market_snapshot_id,request_fingerprint,
                               batch_fingerprint,metadata_payload,batch_payload
                        FROM {}.committed_runs
                        WHERE strategy_id=%s AND run_key=%s
                        """
                    ).format(sql.Identifier(self.schema)),
                    (strategy_id, run_key),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                if row[7] != request_fingerprint:
                    raise LedgerError(
                        "run_key was already committed with a different request"
                    )
                return self._load_normalized_batch_cursor(cursor, row)

    def transition_revision(self, transition: RevisionTransition) -> AccountSnapshot:
        if type(transition) is not RevisionTransition:
            raise TypeError("transition must be RevisionTransition")
        psycopg, sql, Jsonb = _driver()
        schema = sql.Identifier(self.schema)
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        "SELECT strategy_id FROM {}.accounts "
                        "WHERE strategy_id=%s FOR UPDATE"
                    ).format(schema),
                    (transition.strategy_id,),
                )
                if cursor.fetchone() is None:
                    raise KeyError(
                        f"portfolio account not found: {transition.strategy_id}"
                    )
                account = self._load_account_cursor(cursor, transition.strategy_id)
                cursor.execute(
                    sql.SQL(
                        """
                        SELECT from_revision,to_revision,source_snapshot_id,
                               result_snapshot_id,occurred_at,cancelled_intent_ids
                        FROM {}.revision_transitions
                        WHERE strategy_id=%s AND transition_id=%s
                        """
                    ).format(schema),
                    (transition.strategy_id, transition.id),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if (
                        existing[0] != transition.from_revision
                        or existing[1] != transition.to_revision
                        or existing[2] != transition.expected_snapshot_id
                        or existing[4] != transition.occurred_at
                    ):
                        raise LedgerError(
                            "revision transition ID collision with different content"
                        )
                    if account.strategy_revision != existing[1]:
                        raise LedgerError(
                            "revision transition is historical, not current account state"
                        )
                    return account
                if account.strategy_revision != transition.from_revision:
                    raise StalePortfolioSnapshotError(
                        "revision transition source revision is stale"
                    )
                if account.snapshot_id != transition.expected_snapshot_id:
                    raise StalePortfolioSnapshotError(
                        "revision transition source snapshot is stale"
                    )
                _intents, _progress, open_intents, _open_progress, existing_events = (
                    self._load_intents_progress_events(cursor, transition.strategy_id)
                )
                cancelled_ids = tuple(sorted(item.id for item in open_intents))
                result_snapshot_id = _revision_transition_result_snapshot_id(
                    transition,
                    cancelled_ids,
                )
                fact = _RevisionTransitionFact(
                    transition_id=transition.id,
                    strategy_id=transition.strategy_id,
                    from_revision=transition.from_revision,
                    to_revision=transition.to_revision,
                    source_snapshot_id=transition.expected_snapshot_id,
                    result_snapshot_id=result_snapshot_id,
                    occurred_at=transition.occurred_at,
                    cancelled_intent_ids=cancelled_ids,
                )
                transitioned = replace(
                    account,
                    strategy_revision=transition.to_revision,
                    occurred_at=max(account.occurred_at, transition.occurred_at),
                    snapshot_id=result_snapshot_id,
                )
                event = _derived_revision_transition_event(fact)
                transitioned, _events = _apply_events(
                    transitioned,
                    existing_events,
                    (event,),
                )
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.revision_transitions
                        (strategy_id,transition_id,from_revision,to_revision,
                         source_snapshot_id,result_snapshot_id,occurred_at,
                         cancelled_intent_ids)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        """
                    ).format(schema),
                    (
                        transition.strategy_id,
                        transition.id,
                        transition.from_revision,
                        transition.to_revision,
                        transition.expected_snapshot_id,
                        result_snapshot_id,
                        transition.occurred_at,
                        Jsonb(list(cancelled_ids)),
                    ),
                )
                if cancelled_ids:
                    cursor.execute(
                        sql.SQL(
                            """
                            UPDATE {}.order_intents SET cancelled_revision=%s
                            WHERE strategy_id=%s AND intent_id = ANY(%s)
                            """
                        ).format(schema),
                        (
                            transition.to_revision,
                            transition.strategy_id,
                            list(cancelled_ids),
                        ),
                    )
                self._insert_event(
                    cursor,
                    event,
                    strategy_id=transition.strategy_id,
                    strategy_revision=transition.to_revision,
                    run_key=None,
                    ordinal=0,
                )
                self._write_current_account(cursor, transitioned)
                return transitioned

    def commit(self, batch: DecisionBatch) -> AccountSnapshot:
        if type(batch) is not DecisionBatch:
            raise TypeError("batch must be DecisionBatch")
        canonical_facts = _canonical_batch_facts(batch)
        fingerprint = _batch_fingerprint(batch, canonical_facts)
        psycopg, sql, Jsonb = _driver()
        schema = sql.Identifier(self.schema)
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        "SELECT strategy_id FROM {}.accounts "
                        "WHERE strategy_id=%s FOR UPDATE"
                    ).format(schema),
                    (batch.strategy_id,),
                )
                if cursor.fetchone() is None:
                    raise KeyError(
                        f"portfolio account not found: {batch.strategy_id}"
                    )
                account = self._load_account_cursor(cursor, batch.strategy_id)
                cursor.execute(
                    sql.SQL(
                        """
                        SELECT request_fingerprint,batch_fingerprint
                        FROM {}.committed_runs
                        WHERE strategy_id=%s AND run_key=%s
                        """
                    ).format(schema),
                    (batch.strategy_id, batch.run_key),
                )
                existing_run = cursor.fetchone()
                if existing_run is not None:
                    if (
                        existing_run[0] != batch.request_fingerprint
                        or existing_run[1] != fingerprint
                    ):
                        raise LedgerError(
                            "run_key was already committed with different facts"
                        )
                    return account
                if batch.portfolio_snapshot_id != account.snapshot_id:
                    raise StalePortfolioSnapshotError(
                        f"stale portfolio snapshot: expected {account.snapshot_id}, "
                        f"got {batch.portfolio_snapshot_id}"
                    )
                if batch.strategy_revision != account.strategy_revision:
                    raise LedgerError("batch strategy revision differs from account")
                (
                    existing_intents,
                    existing_progress,
                    _open_intents,
                    _open_progress,
                    existing_events,
                ) = self._load_intents_progress_events(cursor, batch.strategy_id)
                transition = self._apply_canonical_batch(
                    account=account,
                    batch=batch,
                    canonical_facts=canonical_facts,
                    existing_intents=existing_intents,
                    existing_progress=existing_progress,
                    existing_events=existing_events,
                )
                current = transition["account"]
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.committed_runs
                        (strategy_id,run_key,strategy_revision,source_snapshot_id,
                         result_snapshot_id,market_snapshot_id,request_fingerprint,
                         batch_fingerprint,committed_at,metadata_payload,batch_payload)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """
                    ).format(schema),
                    (
                        batch.strategy_id,
                        batch.run_key,
                        batch.strategy_revision,
                        batch.portfolio_snapshot_id,
                        current.snapshot_id,
                        batch.market_snapshot_id,
                        batch.request_fingerprint,
                        fingerprint,
                        current.occurred_at,
                        Jsonb(_batch_metadata_payload(batch)),
                        Jsonb(canonical_graph(batch)),
                    ),
                )
                self._insert_batch_facts(
                    cursor,
                    batch=batch,
                    canonical_facts=canonical_facts,
                    paired_fills=transition["paired_fills"],
                    risk_facts=transition["risk_facts"],
                    events=transition["reconciled_events"],
                )
                self._write_current_account(cursor, current)
                return current

    def _apply_canonical_batch(
        self,
        *,
        account: AccountSnapshot,
        batch: DecisionBatch,
        canonical_facts: tuple,
        existing_intents: tuple[OrderIntent, ...],
        existing_progress: tuple[OrderExecutionProgress, ...],
        existing_events: tuple[PortfolioEvent, ...],
    ) -> dict[str, Any]:
        intents, fills, progress, updates, settlement_updates, accruals = (
            canonical_facts
        )
        _validate_carry_event_pairs(accruals, batch.events)
        intents_by_id = {item.id: item for item in existing_intents}
        for intent in intents:
            previous = intents_by_id.get(intent.id)
            if previous is not None and previous != intent:
                raise LedgerError(f"conflicting intent ID: {intent.id}")
            intents_by_id[intent.id] = intent
        stored_progress = {item.intent_id: item for item in existing_progress}
        if fills and not progress:
            raise LedgerError("execution fills require canonical execution progress")
        paired_fills = _validate_fill_summaries(
            fills,
            progress,
            stored_progress,
        )
        current, _merged_progress = _apply_progress(
            account,
            intents_by_id,
            stored_progress,
            progress,
        )
        current = _apply_settlement_updates(current, settlement_updates)
        current = _verify_execution_account(
            current,
            _execution_account_fact(batch),
        )
        current = _apply_risk_updates(current, updates)
        current = _apply_carry(current, accruals)
        risk_facts = tuple(
            _risk_fact_for_batch(batch, update, current.occurred_at)
            for update in updates
        )
        reconciled_events = _reconcile_batch_events(
            batch,
            paired_fills,
            risk_facts,
        )
        current, _merged_events = _apply_events(
            current,
            existing_events,
            reconciled_events,
        )
        current = replace(current, snapshot_id=_stable_snapshot_id(batch))
        _validate_account(current)
        return {
            "account": current,
            "paired_fills": paired_fills,
            "risk_facts": risk_facts,
            "reconciled_events": reconciled_events,
        }

    def _insert_event(
        self,
        cursor: Any,
        event: PortfolioEvent,
        *,
        strategy_id: str,
        strategy_revision: int,
        run_key: str | None,
        ordinal: int,
        batch_ordinal: int | None = None,
        source_kind: str = "SYSTEM",
    ) -> None:
        _psycopg, sql, Jsonb = _driver()
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {}.events
                (strategy_id,event_id,run_key,strategy_revision,ordinal,
                 batch_ordinal,source_kind,event_type,occurred_at,payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (strategy_id,event_id) DO NOTHING
                """
            ).format(sql.Identifier(self.schema)),
            (
                strategy_id,
                event.id,
                run_key,
                strategy_revision,
                ordinal,
                batch_ordinal,
                source_kind,
                event.type,
                event.occurred_at,
                Jsonb(_event_to_json(event)),
            ),
        )

    def _insert_batch_facts(
        self,
        cursor: Any,
        *,
        batch: DecisionBatch,
        canonical_facts: tuple,
        paired_fills: tuple,
        risk_facts: tuple,
        events: tuple[PortfolioEvent, ...],
    ) -> None:
        _psycopg, sql, Jsonb = _driver()
        schema = sql.Identifier(self.schema)
        intents, _fills, progress, updates, settlement_updates, accruals = (
            canonical_facts
        )
        for ordinal, intent in enumerate(intents):
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.order_intents
                    (strategy_id,intent_id,run_key,strategy_revision,ordinal,
                     batch_ordinal,symbol,position_side,order_side,position_effect,quantity,
                     reason,created_snapshot_id,created_market_at,
                     cancelled_revision,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s)
                    ON CONFLICT (strategy_id,intent_id) DO NOTHING
                    """
                ).format(schema),
                (
                    batch.strategy_id,
                    intent.id,
                    batch.run_key,
                    batch.strategy_revision,
                    ordinal,
                    _public_ordinal(intent, batch.intents, fact_kind="order_intent"),
                    intent.symbol,
                    intent.position_side.value,
                    intent.order_side.value,
                    intent.position_effect.value,
                    intent.quantity,
                    intent.reason,
                    intent.created_snapshot_id,
                    intent.created_market_at,
                    Jsonb(_intent_to_json(intent)),
                ),
            )
        for ordinal, item in enumerate(progress):
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.execution_progress
                    (strategy_id,run_key,intent_id,ordinal,batch_ordinal,status,
                     last_occurred_at,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """
                ).format(schema),
                (
                    batch.strategy_id,
                    batch.run_key,
                    item.intent_id,
                    ordinal,
                    _public_ordinal(
                        item,
                        batch.execution_progress,
                        fact_kind="execution_progress",
                    ),
                    item.status,
                    item.fills[-1].occurred_at,
                    Jsonb(_progress_to_json(item)),
                ),
            )
        for ordinal, (detail, summary) in enumerate(paired_fills):
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.fills
                    (strategy_id,progress_fill_id,run_key,intent_id,ordinal,
                     batch_ordinal,symbol,position_side,order_side,snapshot_id,occurred_at,
                     quantity,price,fees,commission,status,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """
                ).format(schema),
                (
                    batch.strategy_id,
                    detail.id,
                    batch.run_key,
                    detail.intent_id,
                    ordinal,
                    _public_ordinal(summary, batch.fills, fact_kind="fill"),
                    detail.symbol,
                    detail.position_side.value,
                    detail.order_side.value,
                    detail.snapshot_id,
                    detail.occurred_at,
                    detail.quantity,
                    detail.price,
                    detail.fees,
                    detail.commission,
                    detail.status,
                    Jsonb(_progress_fill_to_json(detail)),
                ),
            )
        for ordinal, (fact, update) in enumerate(zip(risk_facts, updates)):
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.position_risk_facts
                    (strategy_id,fact_id,run_key,strategy_revision,ordinal,
                     batch_ordinal,portfolio_snapshot_id,market_snapshot_id,
                     symbol,side,occurred_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """
                ).format(schema),
                (
                    batch.strategy_id,
                    fact.fact_id,
                    batch.run_key,
                    batch.strategy_revision,
                    ordinal,
                    _public_ordinal(
                        update,
                        batch.position_risk_updates,
                        fact_kind="position_risk_update",
                    ),
                    batch.portfolio_snapshot_id,
                    batch.market_snapshot_id,
                    update.symbol,
                    update.side.value,
                    fact.occurred_at,
                ),
            )
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.position_risk_updates
                    (strategy_id,fact_id,symbol,side,peak_price,trough_price,
                     trailing_active,position_mode,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """
                ).format(schema),
                (
                    batch.strategy_id,
                    fact.fact_id,
                    update.symbol,
                    update.side.value,
                    update.peak_price,
                    update.trough_price,
                    update.trailing_active,
                    update.position_mode,
                    Jsonb(_risk_update_to_json(update)),
                ),
            )
        for ordinal, update in enumerate(settlement_updates):
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.settlement_updates
                    (strategy_id,run_key,symbol,ordinal,batch_ordinal,side,quantity,
                     sellable_quantity,sellable_on,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """
                ).format(schema),
                (
                    batch.strategy_id,
                    batch.run_key,
                    update.symbol,
                    ordinal,
                    _public_ordinal(
                        update,
                        batch.position_settlement_updates,
                        fact_kind="settlement_update",
                    ),
                    update.side.value,
                    update.quantity,
                    update.sellable_quantity,
                    update.sellable_on,
                    Jsonb(
                        {
                            "symbol": update.symbol,
                            "side": update.side.value,
                            "quantity": update.quantity,
                            "sellable_quantity": update.sellable_quantity,
                            "sellable_on": (
                                None
                                if update.sellable_on is None
                                else update.sellable_on.isoformat()
                            ),
                        }
                    ),
                ),
            )
        for ordinal, accrual in enumerate(accruals):
            material = "|".join(str(item) for item in accrual.idempotency_key)
            accrual_id = "carry-" + hashlib.sha256(
                material.encode("utf-8")
            ).hexdigest()[:24]
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.carry_accruals
                    (strategy_id,accrual_id,run_key,ordinal,batch_ordinal,
                     account_id,cost_type,
                     lifecycle_id,accrual_date,elapsed_days,amount,symbol,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (strategy_id,accrual_id) DO NOTHING
                    """
                ).format(schema),
                (
                    batch.strategy_id,
                    accrual_id,
                    batch.run_key,
                    ordinal,
                    _public_ordinal(
                        accrual,
                        batch.carry_accruals,
                        fact_kind="carry_accrual",
                    ),
                    accrual.account_id,
                    accrual.cost_type.value,
                    accrual.lifecycle_id,
                    accrual.accrual_date,
                    accrual.elapsed_days,
                    accrual.amount,
                    accrual.symbol,
                    Jsonb(_carry_to_json(accrual)),
                ),
            )
        for ordinal, event in enumerate(events):
            self._insert_event(
                cursor,
                event,
                strategy_id=batch.strategy_id,
                strategy_revision=batch.strategy_revision,
                run_key=batch.run_key,
                ordinal=ordinal,
                batch_ordinal=_public_ordinal(
                    event,
                    batch.events,
                    fact_kind="event",
                ),
                source_kind=("BATCH" if event in batch.events else "DERIVED"),
            )


__all__ = ["PostgresLedgerStore"]
