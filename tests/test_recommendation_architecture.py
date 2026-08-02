import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from recommendation_fixtures import make_recommendation_plan
from stock_recommender.market_regime import allocation_config
from stock_recommender.parameters import normalize_allocation_config
from stock_recommender.tracking import save_daily_selection


class RecommendationArchitectureTests(unittest.TestCase):
    def test_tracking_uses_structured_plan_without_display_precision_loss(self):
        now = datetime(2026, 7, 23, 0, 0, tzinfo=timezone.utc)
        plan = make_recommendation_plan(
            [
                {
                    "symbol": "600001",
                    "name": "结构化候选",
                    "price": 10.12345,
                    "percent": 1.23456,
                    "score": 0.8765,
                    "signal_features": {"momentum20": 0.04321, "momentum60": 0.08, "trend": 2},
                }
            ],
            now=now,
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "selection.json"
            symbols = save_daily_selection(target, plan, now=now)
            payload = json.loads(target.read_text(encoding="utf-8"))

        saved = payload["recommendations"][0]
        self.assertEqual(symbols, ["600001"])
        self.assertEqual(saved["entry_price"], 10.12345)
        self.assertEqual(saved["initial_change_pct"], 1.23456)
        self.assertEqual(saved["score"], 0.8765)
        self.assertEqual(saved["signal_features"]["momentum20"], 0.04321)

    def test_market_regime_reads_the_strategy_schema_normalizer(self):
        configured = {
            "neutral_exposure_pct": "25",
            "risk_on_exposure_pct": 70,
            "minimum_universe_size": "12",
        }

        self.assertEqual(allocation_config({"allocation": configured}), normalize_allocation_config(configured))

    def test_selected_candidates_must_belong_to_the_plan_candidate_set(self):
        plan = make_recommendation_plan(
            [{"symbol": "600001", "name": "候选", "price": 10.0}],
            now=datetime(2026, 7, 23, 0, 0, tzinfo=timezone.utc),
        )

        with self.assertRaisesRegex(ValueError, "must be present"):
            replace(plan, selected_candidates=({"symbol": "600002", "name": "越界候选", "price": 11.0},))

if __name__ == "__main__":
    unittest.main()
