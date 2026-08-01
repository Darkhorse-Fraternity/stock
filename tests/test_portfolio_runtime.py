from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from stock_recommender.parameters import default_strategy_config
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


if __name__ == "__main__":
    unittest.main()
