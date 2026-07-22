import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from stock_recommender.market_history import (
    DailyHistoryUnavailableError,
    fetch_daily_history_with_cache,
    save_daily_history_cache,
)


class MarketHistoryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.temporary_directory.name)
        self.now = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
        self.rows = [{"date": date(2026, 7, 21), "open": 10, "close": 10.5, "volume": 1000}]

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_fresh_cache_avoids_network_loader(self):
        save_daily_history_cache("600001", self.rows, cache_dir=self.cache_dir, now=self.now)

        result = fetch_daily_history_with_cache(
            "600001",
            lambda: self.fail("fresh cache should avoid loader"),
            cache_dir=self.cache_dir,
            cache_ttl_seconds=60,
            now=self.now + timedelta(seconds=30),
        )

        self.assertEqual(result[0]["date"], "2026-07-21")
        self.assertEqual(result[0]["close"], 10.5)

    def test_retry_succeeds_and_populates_cache(self):
        calls = []
        sleeps = []

        def loader():
            calls.append(True)
            if len(calls) < 3:
                raise ConnectionError("rate limited")
            return self.rows

        result = fetch_daily_history_with_cache(
            "600001",
            loader,
            cache_dir=self.cache_dir,
            attempts=3,
            backoff_seconds=0.5,
            sleep=sleeps.append,
            now=self.now,
        )

        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0.5, 1.0])
        self.assertEqual(result[0]["date"], "2026-07-21")
        cached = fetch_daily_history_with_cache(
            "600001",
            lambda: self.fail("cache should be populated"),
            cache_dir=self.cache_dir,
            now=self.now,
        )
        self.assertEqual(cached, result)

    def test_stale_cache_is_returned_after_all_retries_fail(self):
        save_daily_history_cache("600001", self.rows, cache_dir=self.cache_dir, now=self.now)
        sleeps = []

        result = fetch_daily_history_with_cache(
            "600001",
            lambda: (_ for _ in ()).throw(ConnectionError("upstream unavailable")),
            cache_dir=self.cache_dir,
            cache_ttl_seconds=1,
            attempts=2,
            backoff_seconds=0.25,
            sleep=sleeps.append,
            now=self.now + timedelta(hours=1),
        )

        self.assertEqual(result[0]["close"], 10.5)
        self.assertEqual(sleeps, [0.25])

    def test_missing_cache_raises_bounded_error_after_retries(self):
        with self.assertRaises(DailyHistoryUnavailableError) as raised:
            fetch_daily_history_with_cache(
                "600001",
                lambda: (_ for _ in ()).throw(ConnectionError("upstream unavailable")),
                cache_dir=self.cache_dir,
                attempts=2,
                backoff_seconds=0,
                now=self.now,
            )

        self.assertIn("已重试 2 次", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
