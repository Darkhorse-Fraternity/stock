import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from stock_recommender.performance import (
    build_recommendation_performance,
    load_recommendation_history,
    upsert_recommendation_history,
)
from stock_recommender.reports import append_performance_link, format_recommendation_snapshot
from stock_recommender.tracking import generate_saved_tracking_report, save_daily_selection


class RecommendationPerformanceTests(unittest.TestCase):
    def test_history_upsert_replaces_same_strategy_day_and_builds_summary(self):
        now = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            first = {
                "trade_date": "2026-07-20",
                "generated_at": "2026-07-20 09:35:00 CST",
                "strategy_id": "strategy-1",
                "strategy_name": "科技 AI",
                "recommendations": [{"symbol": "300001", "name": "测试一", "entry_price": 99}],
            }
            replacement = {
                **first,
                "recommendations": [{"symbol": "300001", "name": "测试一", "entry_price": 100}],
            }
            second = {
                "trade_date": "2026-07-21",
                "generated_at": "2026-07-21 09:35:00 CST",
                "strategy_id": "strategy-1",
                "strategy_name": "科技 AI",
                "recommendations": [{"symbol": "300002", "name": "测试二", "entry_price": 100}],
            }
            upsert_recommendation_history(first, path=path, now=now)
            upsert_recommendation_history(replacement, path=path, now=now)
            upsert_recommendation_history(second, path=path, now=now)

            payload = build_recommendation_performance(
                days=30,
                path=path,
                now=now,
                quote_fetcher=lambda entries: (
                    [
                        {"symbol": "300001", "name": "测试一", "price": 110, "percent": 2, "volume": 2000},
                        {"symbol": "300002", "name": "测试二", "price": 90, "percent": -3, "volume": 3000},
                    ],
                    None,
                ),
            )

        self.assertEqual(payload["summary"]["total_recommendations"], 2)
        self.assertEqual(payload["summary"]["recommendation_days"], 2)
        self.assertEqual(payload["summary"]["win_rate"], 50)
        self.assertEqual(payload["summary"]["average_return_pct"], 0)
        self.assertEqual(payload["summary"]["best"]["symbol"], "300001")
        self.assertEqual(payload["summary"]["worst"]["symbol"], "300002")

    def test_tracking_updates_archived_recommendation(self):
        recommendation_time = datetime(2026, 7, 20, 1, 35, tzinfo=timezone.utc)
        tracking_time = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)
        report = format_recommendation_snapshot(
            [{"symbol": "300001", "name": "测试科技", "price": 100, "percent": 2, "volume": 1000, "turnover": 100000}]
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            history_path = Path(directory) / "history.json"
            save_daily_selection(state_path, report, now=recommendation_time, history_path=history_path)
            generate_saved_tracking_report(
                state_path=state_path,
                history_path=history_path,
                now=tracking_time,
                quote_fetcher=lambda entries: ([{"symbol": "300001", "name": "测试科技", "price": 105, "percent": 7, "volume": 2500, "turnover": 300000}], None),
            )
            history = load_recommendation_history(days=30, path=history_path, now=tracking_time)

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["recommendations"][0]["last_price"], 105)
        self.assertEqual(history[0]["recommendations"][0]["last_volume"], 2500)
        self.assertAlmostEqual(history[0]["recommendations"][0]["return_since_recommendation_pct"], 5)

    def test_performance_link_is_appended_once(self):
        url = "http://192.168.3.216:8765/performance"
        report = append_performance_link("模拟盘报告", url)

        self.assertIn(f"[查看近 30 天推荐表现]({url})", report)
        self.assertEqual(append_performance_link(report, url), report)


if __name__ == "__main__":
    unittest.main()
