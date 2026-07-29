import unittest
from datetime import date, datetime, timedelta, timezone

from stock_recommender.universe_provider import UniverseQuoteBatch
from stock_recommender.warmup import warm_board_history_cache


class FakeProvider:
    def __init__(self, rows):
        self.rows = rows

    def fetch(self, board_code, *, board_name, now=None):
        return UniverseQuoteBatch(
            rows=tuple(self.rows),
            mode="primary",
            board_code=board_code,
            board_name=board_name,
            snapshot_count=len(self.rows),
        )


class HistoryWarmupTests(unittest.TestCase):
    def test_warmup_refreshes_complete_universe_without_strategy_writes(self):
        rows = [
            {"symbol": "600001", "name": "测试一"},
            {"symbol": "600002", "name": "测试二"},
            {"symbol": "600003", "name": "测试三"},
        ]
        calls = []

        def history(symbol, **kwargs):
            calls.append((symbol, kwargs))
            if symbol == "600003":
                raise ConnectionError("upstream unavailable")
            start = date(2025, 1, 1)
            return [
                {"date": start + timedelta(days=index), "close": 10 + index * 0.1}
                for index in range(70)
            ]

        result = warm_board_history_cache(
            board_code="BK0800",
            board_name="人工智能",
            provider=FakeProvider(rows),
            history_fetcher=history,
            workers=2,
            now=datetime(2026, 7, 29, 7, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["universe_count"], 3)
        self.assertEqual(result["ready_count"], 2)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual({symbol for symbol, _ in calls}, {"600001", "600002", "600003"})
        self.assertTrue(all(options["force_refresh"] for _, options in calls))
        self.assertTrue(all(options["attempts"] == 1 for _, options in calls))


if __name__ == "__main__":
    unittest.main()
