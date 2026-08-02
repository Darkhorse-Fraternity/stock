import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from stock_recommender.tracking import generate_saved_tracking_report, load_daily_selection_state, save_daily_selection
from recommendation_fixtures import make_recommendation_plan


class TrackingMetricTests(unittest.TestCase):
    def test_tracking_reports_return_excess_drawdown_and_volume_delta(self):
        recommendation_time = datetime(2026, 7, 20, 1, 35, tzinfo=timezone.utc)
        first_tracking_time = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)
        second_tracking_time = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
        plan = make_recommendation_plan(
            [{"symbol": "300001", "name": "测试科技", "price": 100, "percent": 2, "volume": 1000, "turnover": 100000}],
            now=recommendation_time,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selection.json"
            save_daily_selection(
                path,
                plan,
                now=recommendation_time,
                benchmark_fetcher=lambda *args, **kwargs: ([{"percent": 2}], None),
            )

            first = generate_saved_tracking_report(
                state_path=path,
                now=first_tracking_time,
                quote_fetcher=lambda entries: ([{"symbol": "300001", "name": "测试科技", "price": 110, "percent": 12, "volume": 2000, "turnover": 200000}], None),
                benchmark_fetcher=lambda *args, **kwargs: ([{"percent": 3}], None),
            )
            second = generate_saved_tracking_report(
                state_path=path,
                now=second_tracking_time,
                quote_fetcher=lambda entries: ([{"symbol": "300001", "name": "测试科技", "price": 105, "percent": 7, "volume": 2600, "turnover": 280000}], None),
                benchmark_fetcher=lambda *args, **kwargs: ([{"percent": 4}], None),
            )
            state = load_daily_selection_state(path, now=second_tracking_time)

        self.assertIn("推荐后 +10.00%", first)
        self.assertIn("相对人工智能 +9.02%", first)
        self.assertIn("采样最大回撤 -4.55%", second)
        self.assertIn("较上次 +600 手", second)
        self.assertEqual(state["recommendations"][0]["max_observed_price"], 110)
        self.assertEqual(state["recommendations"][0]["min_observed_price"], 100)
        self.assertAlmostEqual(state["recommendations"][0]["maximum_sampled_drawdown_pct"], -4.5454545)

    def test_tracking_report_identifies_strategy_links_performance_and_hides_errors(self):
        recommendation_time = datetime(2026, 7, 20, 1, 35, tzinfo=timezone.utc)
        plan = make_recommendation_plan(
            [{"symbol": "300001", "name": "测试科技", "price": 100}],
            now=recommendation_time,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selection.json"
            save_daily_selection(path, plan, now=recommendation_time)
            state = json.loads(path.read_text(encoding="utf-8"))
            state.update(
                {
                    "strategy_id": "tech-ai",
                    "strategy_name": "科技 AI",
                    "strategy_revision": 7,
                }
            )
            path.write_text(json.dumps(state), encoding="utf-8")

            report = generate_saved_tracking_report(
                state_path=path,
                now=datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc),
                quote_fetcher=lambda entries: (_ for _ in ()).throw(
                    RuntimeError("API_TOKEN=INTERNAL_SECRET")
                ),
                performance_url="https://stock.example/strategies/tech-ai/portfolio",
            )

        self.assertIn("策略：科技 AI · v7", report)
        self.assertIn("https://stock.example/strategies/tech-ai/portfolio", report)
        self.assertNotIn("API_TOKEN", report)
        self.assertNotIn("INTERNAL_SECRET", report)


if __name__ == "__main__":
    unittest.main()
