from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from stock_recommender.portfolio_engine.archived_store import ArchivedLedgerStore
from stock_recommender.portfolio_engine.contracts import (
    AccountSnapshot,
    DecisionBatch,
    ExecutionFill,
    ExecutionProgressFill,
    OrderIntent,
    OrderExecutionProgress,
    OrderSide,
    PortfolioEvent,
    PositionEffect,
    PositionSide,
    stable_execution_intent_id,
    stable_execution_progress_fill_id,
)
from stock_recommender.portfolio_engine.ledger import (
    InMemoryLedgerStore,
    JsonLedgerStore,
)
from stock_recommender.portfolio_engine.postgres_migration import (
    PostgresBootstrapError,
    bootstrap_postgres_from_json,
)


NOW = datetime(2026, 8, 11, 13, 30, tzinfo=timezone.utc)


def cash_batch(
    *,
    run_key: str,
    snapshot_id: str,
    amount: float,
    minute: int,
) -> DecisionBatch:
    return DecisionBatch(
        run_key=run_key,
        strategy_id="strategy",
        strategy_revision=1,
        portfolio_snapshot_id=snapshot_id,
        market_snapshot_id=f"market-{run_key}",
        request_fingerprint=f"request-{run_key}",
        events=(
            PortfolioEvent(
                id=f"cash-{run_key}",
                type="CASH_ADJUSTED",
                occurred_at=NOW + timedelta(minutes=minute),
                data={"amount": amount},
            ),
        ),
    )


def open_intent_batch(*, snapshot_id: str) -> DecisionBatch:
    occurred_at = NOW + timedelta(minutes=1)
    identity = {
        "symbol": "NVDA",
        "position_side": PositionSide.LONG,
        "order_side": OrderSide.BUY,
        "position_effect": PositionEffect.OPEN,
        "quantity": 10,
        "reason": "migration-test",
        "created_snapshot_id": snapshot_id,
        "created_market_at": occurred_at,
    }
    return DecisionBatch(
        run_key="open-run",
        strategy_id="strategy",
        strategy_revision=1,
        portfolio_snapshot_id=snapshot_id,
        market_snapshot_id="market-open-run",
        request_fingerprint="request-open-run",
        intents=(
            OrderIntent(
                id=stable_execution_intent_id(**identity),
                **identity,
            ),
        ),
    )


class PostgresBootstrapTests(unittest.TestCase):
    def test_check_ignores_completed_progress_outside_open_intents(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "ledger.json"
            archive_path = root / "archive.json"
            source = JsonLedgerStore(source_path)
            account = source.create_account(
                AccountSnapshot(
                    id="account-strategy",
                    strategy_id="strategy",
                    strategy_revision=1,
                    occurred_at=NOW,
                    available_cash=1_000.0,
                    snapshot_id="snapshot-0",
                )
            )
            opened = open_intent_batch(snapshot_id=account.snapshot_id)
            account = source.commit(opened)
            intent = opened.intents[0]
            fill_facts = {
                "intent_id": intent.id,
                "symbol": intent.symbol,
                "position_side": intent.position_side,
                "order_side": intent.order_side,
                "snapshot_id": "market-fill-run",
                "occurred_at": NOW + timedelta(minutes=2),
                "quantity": intent.quantity,
                "price": 100.0,
                "fees": 0.0,
                "commission": 0.0,
                "status": "FILLED",
            }
            progress_fill = ExecutionProgressFill(
                id=stable_execution_progress_fill_id(**fill_facts),
                **fill_facts,
            )
            source.commit(
                DecisionBatch(
                    run_key="fill-run",
                    strategy_id="strategy",
                    strategy_revision=1,
                    portfolio_snapshot_id=account.snapshot_id,
                    market_snapshot_id=progress_fill.snapshot_id,
                    request_fingerprint="request-fill-run",
                    fills=(
                        ExecutionFill(
                            intent_id=intent.id,
                            symbol=intent.symbol,
                            quantity=intent.quantity,
                            price=100.0,
                            fees=0.0,
                            status="FILLED",
                        ),
                    ),
                    execution_progress=(
                        OrderExecutionProgress(
                            intent_id=intent.id,
                            symbol=intent.symbol,
                            position_side=intent.position_side,
                            order_side=intent.order_side,
                            intent_quantity=intent.quantity,
                            execution_policy_fingerprint="migration-test-policy",
                            fills=(progress_fill,),
                        ),
                    ),
                )
            )

            report = bootstrap_postgres_from_json(
                source_path,
                archive_path=archive_path,
                apply=False,
            )

            self.assertEqual(report.open_intent_count, 0)
            self.assertEqual(report.open_progress_count, 0)

    def test_bootstrap_moves_only_current_state_and_merges_frozen_history(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "strategy_portfolios.json"
            archive_path = root / "strategy_portfolios.legacy.json"
            source = JsonLedgerStore(source_path)
            source.create_account(
                AccountSnapshot(
                    id="account-strategy",
                    strategy_id="strategy",
                    strategy_revision=1,
                    occurred_at=NOW,
                    available_cash=1_000.0,
                    snapshot_id="snapshot-0",
                )
            )
            first = cash_batch(
                run_key="run-1",
                snapshot_id="snapshot-0",
                amount=-10.0,
                minute=1,
            )
            archived_account = source.commit(first)
            target = InMemoryLedgerStore(root / "postgres-lock")

            report = bootstrap_postgres_from_json(
                source_path,
                archive_path=archive_path,
                apply=True,
                target_store=target,
            )

            self.assertTrue(report.applied)
            self.assertEqual(report.account_count, 1)
            self.assertEqual(target.load("strategy"), archived_account)
            self.assertLess(archive_path.stat().st_size, source_path.stat().st_size)

            combined = ArchivedLedgerStore(target, archive_path)
            view = combined.load_performance_view("strategy")
            self.assertEqual(view.account, archived_account)
            self.assertEqual([item.run_key for item in view.batches], ["run-1"])
            self.assertEqual(
                [item.type for item in view.events].count("ACCOUNT_OPENED"),
                1,
            )
            self.assertEqual(
                combined.load_archived_committed_batch(
                    "strategy",
                    "run-1",
                    "request-run-1",
                ),
                first,
            )
            second = cash_batch(
                run_key="run-2",
                snapshot_id=archived_account.snapshot_id,
                amount=-5.0,
                minute=2,
            )
            current = combined.commit(second)
            self.assertEqual(current.available_cash, 985.0)
            merged = combined.load_performance_view("strategy")
            self.assertEqual(
                {item.run_key for item in merged.batches},
                {"run-1", "run-2"},
            )

    def test_live_commit_never_reads_large_archive(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "ledger.json"
            archive_path = root / "archive.json"
            source = JsonLedgerStore(source_path)
            account = source.create_account(
                AccountSnapshot(
                    id="account-strategy",
                    strategy_id="strategy",
                    strategy_revision=1,
                    occurred_at=NOW,
                    available_cash=1_000.0,
                    snapshot_id="snapshot-0",
                )
            )
            bootstrap_postgres_from_json(
                source_path,
                archive_path=archive_path,
                apply=True,
                target_store=(target := InMemoryLedgerStore(root / "target-lock")),
            )
            combined = ArchivedLedgerStore(target, archive_path)

            with patch.object(
                combined.archive,
                "load_committed_batch",
                side_effect=AssertionError("archive entered the write path"),
            ):
                self.assertIsNone(
                    combined.load_committed_batch(
                        "strategy",
                        "live-run",
                        "request-live-run",
                    )
                )
                current = combined.commit(
                    cash_batch(
                        run_key="live-run",
                        snapshot_id=account.snapshot_id,
                        amount=-5.0,
                        minute=1,
                    )
                )

            self.assertEqual(current.available_cash, 995.0)

    def test_check_does_not_create_archive_or_require_database(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "ledger.json"
            archive_path = root / "archive.json"
            JsonLedgerStore(source_path).create_account(
                AccountSnapshot(
                    id="account-strategy",
                    strategy_id="strategy",
                    strategy_revision=1,
                    occurred_at=NOW,
                    available_cash=1_000.0,
                    snapshot_id="snapshot-0",
                )
            )

            report = bootstrap_postgres_from_json(
                source_path,
                archive_path=archive_path,
                apply=False,
            )

            self.assertFalse(report.applied)
            self.assertFalse(archive_path.exists())

    def test_nonempty_target_fails_before_archive_write(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "ledger.json"
            archive_path = root / "archive.json"
            source = JsonLedgerStore(source_path)
            account = AccountSnapshot(
                id="account-strategy",
                strategy_id="strategy",
                strategy_revision=1,
                occurred_at=NOW,
                available_cash=1_000.0,
                snapshot_id="snapshot-0",
            )
            source.create_account(account)
            target = InMemoryLedgerStore(root / "target-lock")
            target.create_account(account)

            with self.assertRaisesRegex(PostgresBootstrapError, "not empty"):
                bootstrap_postgres_from_json(
                    source_path,
                    archive_path=archive_path,
                    apply=True,
                    target_store=target,
                )

            self.assertFalse(archive_path.exists())

    def test_active_orders_require_a_seed_capable_target(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "ledger.json"
            archive_path = root / "archive.json"
            source = JsonLedgerStore(source_path)
            account = source.create_account(
                AccountSnapshot(
                    id="account-strategy",
                    strategy_id="strategy",
                    strategy_revision=1,
                    occurred_at=NOW,
                    available_cash=1_000.0,
                    snapshot_id="snapshot-0",
                )
            )
            source.commit(open_intent_batch(snapshot_id=account.snapshot_id))

            with self.assertRaisesRegex(
                PostgresBootstrapError,
                "cannot preserve active order state",
            ):
                bootstrap_postgres_from_json(
                    source_path,
                    archive_path=archive_path,
                    apply=True,
                    target_store=InMemoryLedgerStore(root / "target-lock"),
                )

            self.assertFalse(archive_path.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
