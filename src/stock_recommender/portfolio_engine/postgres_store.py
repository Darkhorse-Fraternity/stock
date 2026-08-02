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
    OrderExecutionProgress,
    OrderIntent,
    PortfolioEvent,
    PortfolioLedgerView,
    PortfolioPerformanceLedgerView,
    PositionSide,
    PositionSnapshot,
    PositionEffect,
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
    _carry_to_json,
    _derived_revision_transition_event,
    _event_from_json,
    _event_to_json,
    _execution_account_fact,
    _intent_from_json,
    _intent_to_json,
    _progress_fill_to_json,
    _progress_from_json,
    _progress_to_json,
    _reconcile_batch_events,
    _revision_transition_result_snapshot_id,
    _risk_fact_for_batch,
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
        strategy_revision INTEGER NOT NULL,
        source_snapshot_id TEXT NOT NULL,
        result_snapshot_id TEXT NOT NULL,
        market_snapshot_id TEXT NOT NULL,
        request_fingerprint TEXT,
        batch_fingerprint TEXT NOT NULL,
        committed_at TIMESTAMPTZ NOT NULL,
        batch_payload JSONB NOT NULL,
        PRIMARY KEY (strategy_id, run_key),
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
        event_type TEXT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (strategy_id, event_id),
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
                SELECT account_id, cost_type, accrual_date, elapsed_days,
                       amount, lifecycle_id, symbol
                FROM {}.carry_accruals
                WHERE strategy_id = %s
                ORDER BY accrual_date, run_key NULLS FIRST, ordinal, accrual_id
                """
            ).format(schema),
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
                         event_type,occurred_at,payload)
                        VALUES (%s,%s,NULL,%s,0,%s,%s,%s)
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
                "WHERE i.strategy_id=%s ORDER BY r.committed_at,i.run_key,"
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
                SELECT DISTINCT ON (intent_id) intent_id, payload
                FROM {}.execution_progress WHERE strategy_id=%s
                ORDER BY intent_id, last_occurred_at DESC, run_key DESC
                """
            ).format(schema),
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
                "LEFT JOIN {}.committed_runs AS r "
                "ON r.strategy_id=e.strategy_id AND r.run_key=e.run_key "
                "WHERE e.strategy_id=%s "
                "ORDER BY COALESCE(r.committed_at,e.occurred_at), "
                "e.run_key NULLS FIRST, e.ordinal, e.event_id"
            ).format(schema, schema),
            (strategy_id,),
        )
        events = tuple(_event_from_json(item[0]) for item in cursor.fetchall())
        return intents, progress, open_intents, open_progress, events

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
                cursor.execute(
                    sql.SQL(
                        "SELECT batch_payload FROM {}.committed_runs "
                        "WHERE strategy_id=%s ORDER BY run_key"
                    ).format(sql.Identifier(self.schema)),
                    (strategy_id,),
                )
                batches = tuple(
                    _batch_from_canonical_json(item[0])
                    for item in cursor.fetchall()
                )
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
            row = connection.execute(
                sql.SQL(
                    "SELECT request_fingerprint, batch_payload "
                    "FROM {}.committed_runs WHERE strategy_id=%s AND run_key=%s"
                ).format(sql.Identifier(self.schema)),
                (strategy_id, run_key),
            ).fetchone()
        if row is None:
            return None
        if row[0] != request_fingerprint:
            raise LedgerError("run_key was already committed with a different request")
        return _batch_from_canonical_json(row[1])

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
                         batch_fingerprint,committed_at,batch_payload)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
    ) -> None:
        _psycopg, sql, Jsonb = _driver()
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {}.events
                (strategy_id,event_id,run_key,strategy_revision,ordinal,
                 event_type,occurred_at,payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (strategy_id,event_id) DO NOTHING
                """
            ).format(sql.Identifier(self.schema)),
            (
                strategy_id,
                event.id,
                run_key,
                strategy_revision,
                ordinal,
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
                     symbol,position_side,order_side,position_effect,quantity,
                     reason,created_snapshot_id,created_market_at,
                     cancelled_revision,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s)
                    ON CONFLICT (strategy_id,intent_id) DO NOTHING
                    """
                ).format(schema),
                (
                    batch.strategy_id,
                    intent.id,
                    batch.run_key,
                    batch.strategy_revision,
                    ordinal,
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
                    (strategy_id,run_key,intent_id,ordinal,status,
                     last_occurred_at,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """
                ).format(schema),
                (
                    batch.strategy_id,
                    batch.run_key,
                    item.intent_id,
                    ordinal,
                    item.status,
                    item.fills[-1].occurred_at,
                    Jsonb(_progress_to_json(item)),
                ),
            )
        for ordinal, (detail, _summary) in enumerate(paired_fills):
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.fills
                    (strategy_id,progress_fill_id,run_key,intent_id,ordinal,
                     symbol,position_side,order_side,snapshot_id,occurred_at,
                     quantity,price,fees,commission,status,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """
                ).format(schema),
                (
                    batch.strategy_id,
                    detail.id,
                    batch.run_key,
                    detail.intent_id,
                    ordinal,
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
                     portfolio_snapshot_id,market_snapshot_id,symbol,side,occurred_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """
                ).format(schema),
                (
                    batch.strategy_id,
                    fact.fact_id,
                    batch.run_key,
                    batch.strategy_revision,
                    ordinal,
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
                    (strategy_id,run_key,symbol,ordinal,side,quantity,
                     sellable_quantity,sellable_on,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """
                ).format(schema),
                (
                    batch.strategy_id,
                    batch.run_key,
                    update.symbol,
                    ordinal,
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
                    (strategy_id,accrual_id,run_key,ordinal,account_id,cost_type,
                     lifecycle_id,accrual_date,elapsed_days,amount,symbol,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (strategy_id,accrual_id) DO NOTHING
                    """
                ).format(schema),
                (
                    batch.strategy_id,
                    accrual_id,
                    batch.run_key,
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
        for ordinal, event in enumerate(events):
            self._insert_event(
                cursor,
                event,
                strategy_id=batch.strategy_id,
                strategy_revision=batch.strategy_revision,
                run_key=batch.run_key,
                ordinal=ordinal,
            )


__all__ = ["PostgresLedgerStore"]
