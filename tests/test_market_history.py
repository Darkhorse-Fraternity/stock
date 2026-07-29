import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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

    def test_stale_cache_is_returned_immediately_without_blocking_retries(self):
        save_daily_history_cache("600001", self.rows, cache_dir=self.cache_dir, now=self.now)
        sleeps = []
        calls = []

        result = fetch_daily_history_with_cache(
            "600001",
            lambda: calls.append(True),
            cache_dir=self.cache_dir,
            cache_ttl_seconds=1,
            attempts=2,
            backoff_seconds=0.25,
            sleep=sleeps.append,
            now=self.now + timedelta(hours=1),
        )

        self.assertEqual(result[0]["close"], 10.5)
        self.assertEqual(calls, [])
        self.assertEqual(sleeps, [])

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

    def test_stale_cache_is_revalidated_by_background_worker(self):
        save_daily_history_cache("600001", self.rows, cache_dir=self.cache_dir, now=self.now)
        started = threading.Event()
        refreshed_rows = [{"date": "2026-07-22", "close": 11.0}]

        result = fetch_daily_history_with_cache(
            "600001",
            lambda: (started.set() or refreshed_rows),
            cache_dir=self.cache_dir,
            cache_ttl_seconds=1,
            attempts=1,
            now=self.now + timedelta(hours=1),
            background_refresh=True,
        )

        self.assertEqual(result[0]["close"], 10.5)
        self.assertTrue(started.wait(timeout=1))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            cached = fetch_daily_history_with_cache(
                "600001",
                lambda: self.fail("refreshed cache should be available"),
                cache_dir=self.cache_dir,
                cache_ttl_seconds=60,
                now=self.now + timedelta(hours=1),
            )
            if cached[0]["close"] == 11.0:
                break
            time.sleep(0.01)
        self.assertEqual(cached[0]["close"], 11.0)

    def test_force_refresh_replaces_even_fresh_cache(self):
        save_daily_history_cache("600001", self.rows, cache_dir=self.cache_dir, now=self.now)

        result = fetch_daily_history_with_cache(
            "600001",
            lambda: [{"date": "2026-07-22", "close": 12.0}],
            cache_dir=self.cache_dir,
            cache_ttl_seconds=60,
            attempts=1,
            now=self.now + timedelta(seconds=30),
            force_refresh=True,
        )

        self.assertEqual(result[0]["close"], 12.0)

    def test_cache_keeps_bounded_recent_history(self):
        rows = [
            {"date": self.now.date() - timedelta(days=400 - index), "close": 10 + index}
            for index in range(400)
        ]
        with mock.patch.dict("os.environ", {"STOCK_AGENT_HISTORY_CACHE_MAX_ROWS": "100"}):
            saved = save_daily_history_cache(
                "600001",
                rows,
                cache_dir=self.cache_dir,
                now=self.now,
            )

        self.assertEqual(len(saved), 100)
        self.assertEqual(saved[0]["close"], 310)
        self.assertEqual(saved[-1]["close"], 409)


if __name__ == "__main__":
    unittest.main()
