from __future__ import annotations

import unittest
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from stock_recommender.parameters import default_strategy_config
from stock_recommender.portfolio_engine.contracts import (
    AccrualLifecycle,
    AccountSnapshot,
    DecisionBatch,
    OrderIntent,
    OrderSide,
    PositionEffect,
    PositionSide,
    PositionSnapshot,
)
from stock_recommender.portfolio_engine.ledger import JsonLedgerStore
from stock_recommender.portfolio_runtime import (
    EmptyEventCalendarProvider,
    FailClosedBorrowProvider,
    MarketAdapterQuoteProvider,
    format_portfolio_snapshot,
    open_portfolio_runtime,
    process_portfolio_runtime,
)


NOW = datetime(2026, 8, 3, 14, 30, tzinfo=timezone.utc)


def strategy() -> dict:
    value = default_strategy_config()
    value.update(
        {
            "id": "runtime-us",
            "name": "Runtime US",
            "revision": 2,
            "market": "us",
            "updated_at": "2026-08-03T14:00:00+00:00",
        }
    )
    value["parameters"]["market"] = {"enabled": True, "value": "us"}
    value["lifecycle"]["stage"] = "paper"
    return value


class Adapter:
    def __init__(self, rows=None, error=None):
        self.rows = list(rows or [])
        self.error = error
        self.calls = []

    def fetch_watchlist(self, entries, **kwargs):
        self.calls.append((tuple(entries), kwargs))
        return list(self.rows), self.error


class PortfolioRuntimeTests(unittest.TestCase):
    def test_runtime_boundaries_reject_naive_time_and_normalize_aware_time_to_utc(self):
        naive = NOW.replace(tzinfo=None)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            FailClosedBorrowProvider().snapshot(("AAPL",), naive)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            EmptyEventCalendarProvider().sessions_until_events(("AAPL",), naive)
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                open_portfolio_runtime(
                    strategy(),
                    path=Path(temporary) / "portfolio-v2.json",
                    adapter=Adapter(),
                    occurred_at=naive,
                )
        offset_time = NOW.astimezone(timezone(timedelta(hours=8)))
        snapshot = MarketAdapterQuoteProvider(
            Adapter([{"symbol": "AAPL", "price": 200}]), strategy()
        ).snapshot(("AAPL",), offset_time)
        self.assertEqual(snapshot.occurred_at, NOW)
        self.assertIs(snapshot.occurred_at.tzinfo, timezone.utc)

    def test_quote_adapter_builds_strict_bar_snapshot(self):
        adapter = Adapter(
            [
                {
                    "symbol": "AAPL",
                    "price": 200,
                    "open": 198,
                    "bar_high": "not-a-number",
                    "high": 202,
                    "low": 197,
                    "volume": 1_000_000,
                }
            ]
        )
        snapshot = MarketAdapterQuoteProvider(adapter, strategy()).snapshot(
            ("AAPL",),
            NOW,
        )
        self.assertEqual(
            dict(snapshot.quotes["AAPL"]),
            {
                "price": 200.0,
                "bar_open": 198.0,
                "bar_high": 202.0,
                "bar_low": 197.0,
                "bar_volume": 1_000_000.0,
            },
        )
        self.assertEqual(adapter.calls[0][0], ({"symbol": "AAPL"},))

    def test_snapshot_id_is_bound_to_complete_canonical_quote_content(self):
        first_rows = [
            {
                "symbol": "AAPL",
                "price": 200,
                "open": 198,
                "high": 202,
                "low": 197,
                "volume": 1_000_000,
            }
        ]
        reordered_rows = [
            {
                "volume": 1_000_000,
                "low": 197,
                "high": 202,
                "open": 198,
                "price": 200,
                "symbol": "AAPL",
            }
        ]
        changed_rows = [{**first_rows[0], "price": 201}]
        first = MarketAdapterQuoteProvider(Adapter(first_rows), strategy()).snapshot(
            ("AAPL",), NOW
        )
        reordered = MarketAdapterQuoteProvider(
            Adapter(reordered_rows), strategy()
        ).snapshot(("AAPL",), NOW)
        changed = MarketAdapterQuoteProvider(Adapter(changed_rows), strategy()).snapshot(
            ("AAPL",), NOW
        )
        self.assertEqual(first.id, reordered.id)
        self.assertNotEqual(first.id, changed.id)

    def test_runtime_bootstraps_processes_and_renders_typed_snapshot(self):
        adapter = Adapter()
        with TemporaryDirectory() as temporary:
            engine, account = open_portfolio_runtime(
                strategy(),
                path=Path(temporary) / "portfolio-v2.json",
                adapter=adapter,
                occurred_at=NOW,
            )
            batch, snapshot = process_portfolio_runtime(
                strategy(),
                engine=engine,
                account=account,
                occurred_at=NOW,
            )
        self.assertEqual(batch.fills, ())
        self.assertEqual(snapshot.positions, ())
        self.assertNotEqual(snapshot.account.snapshot_id, account.snapshot_id)
        report = format_portfolio_snapshot(
            {
                **strategy(),
                "exposure_policy": {
                    **strategy()["exposure_policy"],
                    "max_positions": 7,
                },
            },
            snapshot,
            performance_url="https://stock.example/runtime-us",
        )
        self.assertIn("Runtime US · v2", report)
        self.assertIn("持仓：0/7", report)
        self.assertIn("当前空仓", report)
        self.assertIn("https://stock.example/runtime-us", report)

    def test_runtime_atomically_transitions_revision_and_cancels_old_intents(self):
        adapter = Adapter()
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "portfolio-v2.json"
            engine, original = open_portfolio_runtime(
                strategy(),
                path=path,
                adapter=adapter,
                occurred_at=NOW,
            )
            old_intent = OrderIntent(
                id="runtime-old-revision-intent",
                symbol="AAPL",
                position_side=PositionSide.LONG,
                order_side=OrderSide.BUY,
                position_effect=PositionEffect.OPEN,
                quantity=1,
                reason="OLD_REVISION",
                created_snapshot_id="old-market",
                created_market_at=NOW - timedelta(minutes=1),
            )
            committed = engine.commit(
                DecisionBatch(
                    run_key="runtime-r2-plan",
                    strategy_id="runtime-us",
                    strategy_revision=2,
                    portfolio_snapshot_id=original.snapshot_id,
                    market_snapshot_id="old-market",
                    intents=(old_intent,),
                )
            )
            revised = {**strategy(), "revision": 3}
            _, transitioned = open_portfolio_runtime(
                revised,
                path=path,
                adapter=adapter,
                occurred_at=NOW + timedelta(minutes=1),
            )

            self.assertEqual(transitioned.strategy_revision, 3)
            self.assertNotEqual(transitioned.snapshot_id, committed.snapshot_id)
            self.assertEqual(transitioned.available_cash, committed.available_cash)
            view = JsonLedgerStore(path).load_view("runtime-us")
            self.assertEqual(view.open_intents, ())
            self.assertEqual(view.execution_progress, ())
            self.assertEqual(view.recent_events[-1].type, "REVISION_TRANSITIONED")
            with self.assertRaisesRegex(ValueError, "newer|downgrade"):
                open_portfolio_runtime(
                    strategy(),
                    path=path,
                    adapter=adapter,
                    occurred_at=NOW + timedelta(minutes=2),
                )

    def test_leveraged_revision_can_transition_to_long_only_and_keep_processing(self):
        adapter = Adapter(
            [
                {
                    "symbol": "AAPL",
                    "price": 100.0,
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "volume": 1_000_000,
                }
            ]
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "portfolio-v2.json"
            store = JsonLedgerStore(path)
            opened = store.create_account(
                AccountSnapshot(
                    id="account-runtime-us",
                    strategy_id="runtime-us",
                    strategy_revision=3,
                    occurred_at=NOW,
                    available_cash=0.0,
                    margin_loan=200.0,
                    financing_lifecycle=AccrualLifecycle(
                        id="runtime-financing",
                        started_on=NOW.date(),
                    ),
                    positions=(
                        PositionSnapshot(
                            symbol="AAPL",
                            side=PositionSide.LONG,
                            quantity=12,
                            average_cost=100.0,
                            current_price=100.0,
                            sellable_quantity=12,
                        ),
                    ),
                    snapshot_id="runtime-leveraged-r3",
                )
            )
            old_increase = OrderIntent(
                id="runtime-leveraged-old-increase",
                symbol="AAPL",
                position_side=PositionSide.LONG,
                order_side=OrderSide.BUY,
                position_effect=PositionEffect.INCREASE,
                quantity=1,
                reason="OLD_LEVERAGED_REVISION",
                created_snapshot_id="runtime-old-market",
                created_market_at=NOW,
            )
            store.commit(
                DecisionBatch(
                    run_key="runtime-leveraged-r3-plan",
                    strategy_id="runtime-us",
                    strategy_revision=3,
                    portfolio_snapshot_id=opened.snapshot_id,
                    market_snapshot_id="runtime-old-market",
                    intents=(old_increase,),
                )
            )
            revised = {**strategy(), "revision": 4}

            engine, transitioned = open_portfolio_runtime(
                revised,
                path=path,
                adapter=adapter,
                occurred_at=NOW + timedelta(minutes=1),
            )
            batch, snapshot = process_portfolio_runtime(
                revised,
                engine=engine,
                account=transitioned,
                occurred_at=NOW + timedelta(minutes=2),
            )
            report = format_portfolio_snapshot(revised, snapshot)

            self.assertEqual(transitioned.strategy_revision, 4)
            self.assertEqual(transitioned.positions, opened.positions)
            self.assertTrue(
                all(fill.intent_id != old_increase.id for fill in batch.fills)
            )
            self.assertEqual(snapshot.account.strategy_revision, 4)
            self.assertIn("AAPL", report)

    def test_concurrent_revision_open_uses_one_canonical_strategy_timestamp(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "portfolio-v2.json"
            open_portfolio_runtime(
                strategy(),
                path=path,
                adapter=Adapter(),
                occurred_at=NOW,
            )
            revised = {
                **strategy(),
                "revision": 3,
                "updated_at": "2026-08-04T08:15:30+08:00",
            }
            barrier = threading.Barrier(2)
            captured = []
            accounts = []
            errors = []
            original = JsonLedgerStore.transition_revision

            def synchronized_transition(store, transition):
                captured.append(transition)
                barrier.wait(timeout=5)
                return original(store, transition)

            def open_at(occurred_at):
                try:
                    _, account = open_portfolio_runtime(
                        revised,
                        path=path,
                        adapter=Adapter(),
                        occurred_at=occurred_at,
                    )
                    accounts.append(account)
                except Exception as exc:
                    errors.append(exc)

            with patch.object(
                JsonLedgerStore,
                "transition_revision",
                synchronized_transition,
            ):
                threads = [
                    threading.Thread(
                        target=open_at,
                        args=(NOW + timedelta(minutes=minute),),
                    )
                    for minute in (1, 2)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())

            self.assertEqual(errors, [])
            self.assertEqual(len(accounts), 2)
            self.assertEqual(len(captured), 2)
            self.assertEqual(captured[0], captured[1])
            self.assertEqual(
                captured[0].occurred_at,
                datetime(2026, 8, 4, 0, 15, 30, tzinfo=timezone.utc),
            )
            self.assertEqual(accounts[0].strategy_revision, 3)
            self.assertEqual(accounts[1].strategy_revision, 3)

    def test_revision_open_requires_an_explicit_aware_strategy_timestamp(self):
        for invalid in (None, "2026-08-04T08:15:30"):
            with self.subTest(updated_at=invalid), TemporaryDirectory() as temporary:
                path = Path(temporary) / "portfolio-v2.json"
                open_portfolio_runtime(
                    strategy(),
                    path=path,
                    adapter=Adapter(),
                    occurred_at=NOW,
                )
                revised = {**strategy(), "revision": 3, "updated_at": invalid}

                with self.assertRaisesRegex(ValueError, "updated_at|timezone-aware"):
                    open_portfolio_runtime(
                        revised,
                        path=path,
                        adapter=Adapter(),
                        occurred_at=NOW + timedelta(minutes=1),
                    )
                self.assertEqual(
                    JsonLedgerStore(path).load("runtime-us").strategy_revision,
                    2,
                )


if __name__ == "__main__":
    unittest.main()
