"""One-month long/short replay against an isolated Docker PostgreSQL schema."""

from __future__ import annotations

import os
import threading
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from postgres_integration import (
    isolated_postgres_schema,
    non_test_schemas,
    require_isolated_schema_name,
    schema_exists,
)
from stock_recommender.portfolio_engine.borrow import BorrowSecurity, BorrowSnapshot
from stock_recommender.portfolio_engine.config import (
    default_exposure_policy,
    default_margin_policy,
    default_short_policy,
)
from stock_recommender.portfolio_engine.contracts import (
    AccountSnapshot,
    MarketSnapshot,
    PlanRequest,
    PositionSide,
    ProcessRequest,
    RevisionTransition,
    SignalCandidate,
)
from stock_recommender.portfolio_engine.ledger import InMemoryLedgerStore
from stock_recommender.portfolio_engine.request_identity import request_fingerprint
from stock_recommender.portfolio_engine.service import PortfolioEngine


DATABASE_URL = os.getenv("STOCK_AGENT_TEST_DATABASE_URL")
START = datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)
SYMBOLS = tuple(f"L{index}" for index in range(8)) + ("S0", "S1")


class FixedSignals:
    def __init__(self, model_id: str, side: PositionSide, symbols: tuple[str, ...]):
        self.model_id = model_id
        self.side = side
        self.symbols = symbols

    def evaluate(self, rows, event_calendar):
        del rows, event_calendar
        weight = 15.0 if self.side is PositionSide.LONG else 5.0
        return tuple(
            SignalCandidate(
                symbol=symbol,
                side=self.side,
                score=1.0 - index / 100.0,
                requested_weight_pct=weight,
                model_id=self.model_id,
                thesis_id=f"{self.model_id}:{symbol}:month",
            )
            for index, symbol in enumerate(self.symbols)
        )


def long_short_strategy(strategy_id: str = "month-long-short") -> dict:
    exposure = default_exposure_policy()
    exposure.update(
        {
            "mode": "LONG_SHORT",
            "max_positions": 10,
            "max_gross_exposure_pct": 150.0,
            "max_net_exposure_pct": 120.0,
            "max_long_exposure_pct": 120.0,
            "max_short_exposure_pct": 30.0,
        }
    )
    margin = default_margin_policy()
    margin.update(
        {
            "maintenance_margin_pct": 30.0,
            "liquidation_buffer_pct": 10.0,
            "financing_apr_pct": 8.0,
        }
    )
    short = default_short_policy()
    short.update(
        {
            "signal_model": "month-short-v1",
            "estimated_borrow_apr_pct": 10.0,
        }
    )
    return {
        "version": 6,
        "id": strategy_id,
        "revision": 1,
        "market": "us",
        "signal": {"model": "month-long-v1"},
        "lifecycle": {"stage": "paper"},
        "exposure_policy": exposure,
        "margin_policy": margin,
        "short_policy": short,
        "portfolio": {
            "initial_cash": 100_000.0,
            "commission_rate_pct": 0.01,
            "minimum_commission_cny": 0.0,
            "stamp_duty_rate_pct": 0.0,
            "transfer_fee_rate_pct": 0.0,
            "slippage_bps": 0.0,
            # Force partial fills on the first execution, then finish later.
            "max_bar_participation_pct": 5.0,
            "peak_equity": 100_000.0,
        },
    }


def _market(session: int) -> MarketSnapshot:
    occurred_at = START + timedelta(days=session)
    quotes = {}
    for index, symbol in enumerate(SYMBOLS):
        if symbol.startswith("L"):
            price = (
                100.0
                if session <= 1
                else 100.0 + session * (1.0 + index / 10.0)
            )
        else:
            # Session 12 produces a squeeze; later sessions recover.
            price = 200.0 if session == 12 else 100.0 - session * 0.6 + index
        quotes[symbol] = {
            "price": max(5.0, price),
            "bar_open": max(5.0, price),
            "bar_high": max(5.0, price * 1.01),
            "bar_low": max(5.0, price * 0.99),
            "bar_volume": (
                500 if session == 1 and symbol == "L7" else 1_000_000
            ),
            "volume_ratio": 4.0 if session == 12 else 1.0,
            "one_day_return": 12.0 if session == 12 else 0.0,
            "volatility_20d_pct": 20.0,
        }
    return MarketSnapshot(
        id=f"month-market-{session:02d}",
        occurred_at=occurred_at,
        quotes=quotes,
    )


def _borrow(session: int) -> BorrowSnapshot:
    if session == 0:
        return BorrowSnapshot.unavailable("month-borrow-missing")
    securities = {
        symbol: BorrowSecurity(
            symbol=symbol,
            shortable=not (session >= 8 and symbol == "S1"),
            easy_to_borrow=not (session >= 8 and symbol == "S1"),
            borrow_apr_pct=12.0,
            available_quantity=1_000_000,
        )
        for symbol in SYMBOLS
    }
    return BorrowSnapshot(
        id=f"month-borrow-{session:02d}",
        status="AVAILABLE",
        securities=securities,
    )


def _canonical_fingerprint(batch, account, snapshot, daily_events) -> tuple:
    return (
        tuple(
            (
                item.id,
                item.type,
                item.occurred_at.isoformat(),
                tuple(sorted((str(key), repr(value)) for key, value in item.data.items())),
            )
            for item in daily_events
        ),
        tuple(
            (
                item.intent_id,
                item.symbol,
                item.quantity,
                item.price,
                item.fees,
                item.status,
            )
            for item in batch.fills
        ),
        tuple(
            (
                item.symbol,
                item.side.value,
                item.quantity,
                item.average_cost,
                item.current_price,
                item.peak_price,
                item.trough_price,
                item.trailing_active,
                item.position_mode,
                item.sellable_quantity,
                None if item.sellable_on is None else item.sellable_on.isoformat(),
                (
                    None
                    if item.borrow_lifecycle is None
                    else (
                        item.borrow_lifecycle.id,
                        item.borrow_lifecycle.started_on.isoformat(),
                    )
                ),
            )
            for item in sorted(account.positions, key=lambda value: value.symbol)
        ),
        snapshot.metrics.equity,
    )


def replay_22_sessions(store, *, strategy: dict) -> dict:
    strategy = deepcopy(strategy)
    account = store.create_account(
        AccountSnapshot(
            id=f"account-{strategy['id']}",
            strategy_id=strategy["id"],
            strategy_revision=strategy["revision"],
            occurred_at=START,
            available_cash=strategy["portfolio"]["initial_cash"],
            snapshot_id=f"bootstrap-{strategy['id']}",
        )
    )
    engine = PortfolioEngine(
        signal_registry={
            "month-long-v1": FixedSignals(
                "month-long-v1",
                PositionSide.LONG,
                tuple(f"L{index}" for index in range(8)),
            ),
            "month-short-v1": FixedSignals(
                "month-short-v1", PositionSide.SHORT, ("S0", "S1")
            ),
        },
        ledger_store=store,
    )
    fingerprints = []
    maximum_positions = 0
    maximum_gross = 0.0
    maximum_net = 0.0
    maximum_short = 0.0
    event_types = set()
    fill_statuses = set()
    position_modes = set()
    diagnostic_codes = set()
    intent_reasons = set()
    seen_event_ids = set()
    for session in range(22):
        market = _market(session)
        borrow = _borrow(session)
        current = store.load(strategy["id"])
        if session == 10:
            store.transition_revision(
                RevisionTransition(
                    id=f"month-margin-revision-{strategy['id']}",
                    strategy_id=strategy["id"],
                    expected_snapshot_id=current.snapshot_id,
                    from_revision=1,
                    to_revision=2,
                    occurred_at=market.occurred_at,
                )
            )
            strategy["revision"] = 2
            strategy["margin_policy"]["maintenance_margin_pct"] = 70.0
            strategy["margin_policy"]["liquidation_buffer_pct"] = 10.0
            current = store.load(strategy["id"])
        if session == 0:
            batch = engine.plan_and_commit(
                PlanRequest(
                    run_key=f"month-{session:02d}-plan",
                    strategy=strategy,
                    account=current,
                    analyzed_rows=tuple({"symbol": symbol} for symbol in SYMBOLS),
                    market=market,
                    borrow=borrow,
                    event_calendar={symbol: None for symbol in SYMBOLS},
                )
            )
        elif session in {6, 13, 19}:
            batch = engine.plan_and_commit(
                PlanRequest(
                    run_key=f"month-{session:02d}-plan",
                    strategy=strategy,
                    account=current,
                    analyzed_rows=tuple({"symbol": symbol} for symbol in SYMBOLS),
                    market=market,
                    borrow=borrow,
                    event_calendar={symbol: None for symbol in SYMBOLS},
                )
            )
        else:
            batch = engine.process_and_commit(
                ProcessRequest(
                    run_key=f"month-{session:02d}-process",
                    strategy=strategy,
                    account=current,
                    market=market,
                    borrow=borrow,
                )
            )
        committed = store.load(strategy["id"])
        snapshot = engine.performance(strategy["id"], market)
        performance = store.load_performance_view(strategy["id"])
        daily_events = tuple(
            item for item in performance.events if item.id not in seen_event_ids
        )
        seen_event_ids.update(item.id for item in daily_events)
        fingerprints.append(
            _canonical_fingerprint(batch, committed, snapshot, daily_events)
        )
        event_types.update(item.type for item in daily_events)
        fill_statuses.update(item.status for item in batch.fills)
        position_modes.update(item.position_mode for item in committed.positions)
        diagnostic_codes.update(str(item.get("code")) for item in batch.diagnostics)
        intent_reasons.update(item.reason for item in batch.intents)
        metrics = snapshot.metrics
        maximum_positions = max(maximum_positions, len(committed.positions))
        maximum_gross = max(maximum_gross, metrics.gross_exposure_pct)
        maximum_net = max(maximum_net, abs(metrics.net_exposure_pct))
        maximum_short = max(maximum_short, metrics.short_exposure_pct)
    final = store.load(strategy["id"])
    return {
        "sessions": len(fingerprints),
        "fingerprints": tuple(fingerprints),
        "maximum_positions": maximum_positions,
        "maximum_gross_exposure_pct": maximum_gross,
        "maximum_net_exposure_pct": maximum_net,
        "maximum_short_exposure_pct": maximum_short,
        "event_types": frozenset(event_types),
        "fill_statuses": frozenset(fill_statuses),
        "position_modes": frozenset(position_modes),
        "diagnostic_codes": frozenset(diagnostic_codes),
        "intent_reasons": frozenset(intent_reasons),
        "financing_cost": final.accrued_financing_cost,
        "borrow_cost": final.accrued_borrow_cost,
    }


@unittest.skipUnless(DATABASE_URL, "requires Docker PostgreSQL")
class MonthLongShortIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from stock_recommender.portfolio_engine.postgres_store import PostgresLedgerStore

        cls.PostgresLedgerStore = PostgresLedgerStore

    def test_schema_name_guard_refuses_existing_names(self):
        for unsafe in ("public", "pg_catalog", "stock_agent_test_nope", "x; DROP SCHEMA public"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                require_isolated_schema_name(unsafe)

    def test_cleanup_after_success_assertion_exception_and_client_disconnect(self):
        before = non_test_schemas(DATABASE_URL)
        created = []

        with isolated_postgres_schema(DATABASE_URL) as schema:
            created.append(schema)
            self.assertTrue(schema_exists(DATABASE_URL, schema))

        failed_schema = None
        with self.assertRaises(AssertionError):
            with isolated_postgres_schema(DATABASE_URL) as schema:
                failed_schema = schema
                self.fail("deliberate assertion failure")
        created.append(failed_schema)

        raised_schema = None
        with self.assertRaisesRegex(RuntimeError, "deliberate body exception"):
            with isolated_postgres_schema(DATABASE_URL) as schema:
                raised_schema = schema
                raise RuntimeError("deliberate body exception")
        created.append(raised_schema)

        disconnected_schema = None
        with isolated_postgres_schema(DATABASE_URL) as schema:
            disconnected_schema = schema
            import psycopg

            connection = psycopg.connect(DATABASE_URL)
            backend_pid = connection.info.backend_pid
            with psycopg.connect(DATABASE_URL, autocommit=True) as controller:
                controller.execute("SELECT pg_terminate_backend(%s)", (backend_pid,))
            with self.assertRaises(psycopg.OperationalError):
                connection.execute("SELECT 1")
            connection.close()
        created.append(disconnected_schema)

        for schema in created:
            self.assertIsNotNone(schema)
            self.assertFalse(schema_exists(DATABASE_URL, schema))
        self.assertEqual(non_test_schemas(DATABASE_URL), before)

    def test_one_month_replay_is_isolated_and_respects_all_caps(self):
        before = non_test_schemas(DATABASE_URL)
        with isolated_postgres_schema(DATABASE_URL) as schema:
            pg_store = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            pg_result = replay_22_sessions(
                pg_store,
                strategy=long_short_strategy(),
            )
            with TemporaryDirectory(prefix="stock-month-memory-") as temporary:
                memory_result = replay_22_sessions(
                    InMemoryLedgerStore(Path(temporary) / "ledger.lock"),
                    strategy=long_short_strategy(),
                )
            self.assertEqual(pg_result["sessions"], 22)
            self.assertLessEqual(pg_result["maximum_positions"], 10)
            self.assertLessEqual(pg_result["maximum_gross_exposure_pct"], 150.0)
            self.assertLessEqual(pg_result["maximum_net_exposure_pct"], 120.0)
            self.assertLessEqual(pg_result["maximum_short_exposure_pct"], 30.0)
            self.assertEqual(pg_result["fingerprints"], memory_result["fingerprints"])
            self.assertIn("PARTIAL", pg_result["fill_statuses"])
            self.assertGreater(pg_result["financing_cost"], 0.0)
            self.assertGreater(pg_result["borrow_cost"], 0.0)
            self.assertIn("COVER_ONLY", pg_result["position_modes"])
            self.assertIn("BORROW_DATA_MISSING", pg_result["diagnostic_codes"])
            self.assertNotIn(
                "DELEVERAGING_PROJECTION_FAILED",
                pg_result["diagnostic_codes"],
            )
            self.assertTrue(
                {"SHORT_STOP_LOSS", "SHORT_SQUEEZE"}
                & pg_result["intent_reasons"]
            )
            self.assertIn("FINANCING_COST_ACCRUED", pg_result["event_types"])
            self.assertIn("BORROW_COST_ACCRUED", pg_result["event_types"])
        self.assertFalse(schema_exists(DATABASE_URL, schema))
        self.assertEqual(non_test_schemas(DATABASE_URL), before)

    def test_concurrent_duplicate_commit_is_idempotent(self):
        with isolated_postgres_schema(DATABASE_URL) as schema:
            store = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            strategy = long_short_strategy("month-concurrent")
            store.create_account(
                AccountSnapshot(
                    id="account-month-concurrent",
                    strategy_id=strategy["id"],
                    strategy_revision=1,
                    occurred_at=START,
                    available_cash=100_000.0,
                    snapshot_id="bootstrap-month-concurrent",
                )
            )
            engine = PortfolioEngine(
                signal_registry={
                    "month-long-v1": FixedSignals(
                        "month-long-v1", PositionSide.LONG, ("L0",)
                    ),
                    "month-short-v1": FixedSignals(
                        "month-short-v1", PositionSide.SHORT, ("S0",)
                    ),
                }
            )
            source = store.load(strategy["id"])
            request = PlanRequest(
                    run_key="same-run",
                    strategy=strategy,
                    account=source,
                    analyzed_rows=({"symbol": "L0"}, {"symbol": "S0"}),
                    market=_market(1),
                    borrow=_borrow(1),
                    event_calendar={"L0": None, "S0": None},
            )
            batch = replace(
                engine.evaluate(request),
                request_fingerprint=request_fingerprint(request),
            )
            results = []
            errors = []

            def commit_once():
                try:
                    results.append(store.commit(batch))
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=commit_once) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertFalse(errors)
            self.assertEqual(len(results), 4)
            self.assertEqual(len({item.snapshot_id for item in results}), 1)
            self.assertEqual(
                store.load_committed_batch(
                    strategy["id"], "same-run", batch.request_fingerprint
                ),
                batch,
            )
            import psycopg
            from psycopg import sql

            with psycopg.connect(DATABASE_URL) as connection:
                count = connection.execute(
                    sql.SQL(
                        "SELECT COUNT(*) FROM {}.committed_runs "
                        "WHERE strategy_id = %s AND run_key = %s"
                    ).format(sql.Identifier(schema)),
                    (strategy["id"], "same-run"),
                ).fetchone()[0]
            self.assertEqual(count, 1)
            with self.assertRaises(psycopg.errors.UniqueViolation):
                with psycopg.connect(DATABASE_URL) as connection:
                    connection.execute(
                        sql.SQL(
                            """
                            INSERT INTO {}.committed_runs
                                (strategy_id, run_key, request_fingerprint, batch_fingerprint)
                            VALUES (%s, %s, %s, %s)
                            """
                        ).format(sql.Identifier(schema)),
                        (
                            strategy["id"],
                            "same-run",
                            batch.request_fingerprint,
                            "deliberate-duplicate",
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
