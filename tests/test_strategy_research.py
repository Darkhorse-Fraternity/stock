import unittest
from pathlib import Path

from stock_recommender.parameters import load_strategy_config
from stock_recommender.strategy_research import apply_research_variant, default_research_variants, rank_robust_candidates


class StrategyResearchTests(unittest.TestCase):
    def test_default_variants_are_bounded_and_keep_max_positions(self):
        variants = default_research_variants()

        self.assertEqual(len(variants), 20)
        self.assertEqual(len({item["id"] for item in variants}), 20)
        self.assertTrue(all(1 <= item["top_n"] <= 10 for item in variants))

    def test_variant_application_does_not_mutate_strategy(self):
        strategy = load_strategy_config(path=Path("/missing"))
        original_days = strategy["portfolio"]["signal_invalid_days"]

        candidate = apply_research_variant(strategy, default_research_variants()[-1])

        self.assertEqual(strategy["portfolio"]["signal_invalid_days"], original_days)
        self.assertEqual(candidate["portfolio"]["max_positions"], 10)
        self.assertNotEqual(candidate["portfolio"]["signal_invalid_days"], original_days)

    def test_robust_ranking_uses_cross_period_ranks_not_raw_return_scale(self):
        def item(identifier, value):
            return {
                "id": identifier,
                "factor_profile": identifier,
                "portfolio_profile": "test",
                "factor_weights": {},
                "top_n": 3,
                "signal_invalid_days": 5,
                "cumulative_return_pct": value,
                "maximum_drawdown_pct": -5,
            }

        ranked = rank_robust_candidates(
            [
                {"results": [item("steady", 2), item("spiky", 1)]},
                {"results": [item("spiky", 100), item("steady", 10)]},
                {"results": [item("steady", 1), item("spiky", -20)]},
            ]
        )

        self.assertEqual(ranked[0]["id"], "steady")
        self.assertEqual(ranked[0]["periods"], 3)


if __name__ == "__main__":
    unittest.main()
