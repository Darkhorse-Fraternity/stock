import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from stock_recommender.context import collect_recommendation_plan
from stock_recommender.data_sources import fetch_sina_fallback_quotes
from stock_recommender.universe_provider import (
    BoardUniverseProvider,
    BoardUniverseSnapshotStore,
)


class FakeBytesResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.payload


def board_rows(count: int) -> list[dict]:
    return [
        {
            "symbol": f"60{index:04d}",
            "name": f"板块股票{index}",
            "sector": "人工智能",
            "price": 10 + index / 10,
            "percent": index / 100,
            "change": 0.1,
            "volume": 100_000,
            "turnover": 100_000_000,
            "source": "完整板块测试源",
        }
        for index in range(1, count + 1)
    ]


def signal_history(symbol: str) -> list[dict]:
    offset = int(symbol[-2:]) / 1000
    start = date(2025, 1, 1)
    return [
        {
            "date": start + timedelta(days=index),
            "open": 10 + index * (0.04 + offset),
            "close": 10 + index * (0.04 + offset),
            "high": 10.2 + index * (0.04 + offset),
            "low": 9.8 + index * (0.04 + offset),
            "volume": 100_000 + index * 100,
        }
        for index in range(100)
    ]


class BoardUniverseProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = BoardUniverseSnapshotStore(Path(self.temporary_directory.name))
        self.now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_primary_success_persists_complete_snapshot_then_realtime_fallback_uses_it(self):
        rows = board_rows(36)
        primary = BoardUniverseProvider(
            primary_fetcher=lambda board_code, **kwargs: (rows, None),
            quote_fetcher=lambda **kwargs: self.fail("primary success must not use quote fallback"),
            snapshot_store=self.store,
        )
        first = primary.fetch("BK0800", board_name="人工智能", now=self.now)
        seen = []

        def realtime_quotes(*, symbols, **kwargs):
            seen.extend(item["symbol"] for item in symbols)
            return [
                {
                    **item,
                    "price": 11,
                    "percent": 1,
                    "turnover": 100_000_000,
                    "source": "快照实时行情",
                }
                for item in symbols
            ], None

        fallback = BoardUniverseProvider(
            primary_fetcher=lambda board_code, **kwargs: ([], "eastmoney ssl eof"),
            quote_fetcher=realtime_quotes,
            snapshot_store=self.store,
        )
        second = fallback.fetch("BK0800", board_name="人工智能", now=self.now + timedelta(hours=1))

        self.assertEqual(first.mode, "primary")
        self.assertEqual(first.snapshot_count, 36)
        self.assertEqual(second.mode, "snapshot_realtime")
        self.assertEqual(len(second.rows), 36)
        self.assertEqual(len(seen), 36)
        self.assertIn("600036", seen)

    def test_missing_snapshot_blocks_instead_of_using_static_symbols(self):
        called = False

        def fallback(**kwargs):
            nonlocal called
            called = True
            return board_rows(10), None

        provider = BoardUniverseProvider(
            primary_fetcher=lambda board_code, **kwargs: ([], "primary down"),
            quote_fetcher=fallback,
            snapshot_store=self.store,
        )
        result = provider.fetch("BK0800", board_name="人工智能", now=self.now)

        self.assertFalse(called)
        self.assertEqual(result.mode, "unavailable")
        self.assertEqual(result.rows, ())
        self.assertIn("没有可用的完整板块股票池快照", result.error)

    def test_partial_primary_response_is_never_saved_as_complete_snapshot(self):
        provider = BoardUniverseProvider(
            primary_fetcher=lambda board_code, **kwargs: (
                board_rows(5),
                "板块行情只返回 5/36 只",
            ),
            quote_fetcher=lambda **kwargs: self.fail("partial primary must not become fallback metadata"),
            snapshot_store=self.store,
        )

        result = provider.fetch("BK0800", board_name="人工智能", now=self.now)

        self.assertEqual(result.mode, "unavailable")
        self.assertEqual(result.rows, ())
        self.assertIsNone(self.store.load("BK0800"))
        self.assertIn("只返回 5/36", result.error)

    def test_board_coverage_gate_blocks_seven_rows_but_accepts_complete_sample(self):
        blocked_provider = BoardUniverseProvider(
            primary_fetcher=lambda board_code, **kwargs: (board_rows(7), None),
            snapshot_store=self.store,
        )
        blocked = collect_recommendation_plan(
            now=self.now,
            universe_provider=blocked_provider,
            history_fetcher=signal_history,
        )

        ready_provider = BoardUniverseProvider(
            primary_fetcher=lambda board_code, **kwargs: (board_rows(36), None),
            snapshot_store=self.store,
        )
        ready = collect_recommendation_plan(
            now=self.now,
            universe_provider=ready_provider,
            history_fetcher=signal_history,
        )

        self.assertEqual(blocked.data_quality["status"], "BLOCKED")
        self.assertEqual(blocked.market_regime["state"], "UNKNOWN")
        self.assertEqual(blocked.selected_candidates, ())
        self.assertEqual(ready.data_quality["status"], "READY")
        self.assertEqual(ready.data_quality["history_ready_count"], 36)
        self.assertGreater(len(ready.selected_candidates), 0)

    def test_sina_quotes_are_batched_for_complete_board_snapshot(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            requested = request.full_url.split("list=", 1)[1].split(",")
            lines = []
            for item in requested:
                symbol = item[-6:]
                lines.append(
                    f'var hq_str_{item}="股票{symbol},10,10,10.1,10.2,9.9,0,0,10000,101000,'
                    + ",".join(["0"] * 20)
                    + ',2026-07-29,10:00:00,00";'
                )
            return FakeBytesResponse("".join(lines).encode("gb18030"))

        symbols = [f"60000{index}" for index in range(1, 6)]
        with mock.patch.dict(
            "os.environ",
            {"STOCK_AGENT_SINA_QUOTE_BATCH_SIZE": "2"},
        ):
            rows, error = fetch_sina_fallback_quotes(symbols=symbols, urlopen_func=opener)

        self.assertIsNone(error)
        self.assertEqual(len(rows), 5)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
