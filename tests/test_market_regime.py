import unittest

from stock_recommender.market_regime import (
    evaluate_market_regime,
    filter_absolute_momentum,
)
from stock_recommender.parameters import default_strategy_config
from stock_recommender.reports import format_market_regime_summary


def row(symbol, momentum20, momentum60, trend):
    return {
        "symbol": symbol,
        "signal_features": {
            "momentum20": momentum20,
            "momentum60": momentum60,
            "trend": trend,
        },
    }


class MarketRegimeTests(unittest.TestCase):
    def setUp(self):
        self.strategy = default_strategy_config()

    def test_three_breadth_signals_map_to_exposure_budget(self):
        risk_on = evaluate_market_regime([row("1", 0.1, 0.2, 2), row("2", 0.2, 0.1, 1)], self.strategy)
        neutral = evaluate_market_regime([row("1", 0.1, -0.2, 2), row("2", 0.2, -0.1, 1)], self.strategy)
        risk_off = evaluate_market_regime([row("1", -0.1, -0.2, 0), row("2", -0.2, -0.1, 0)], self.strategy)

        self.assertEqual((risk_on["state"], risk_on["target_exposure_pct"]), ("RISK_ON", 100.0))
        self.assertEqual((neutral["state"], neutral["target_exposure_pct"]), ("NEUTRAL", 40.0))
        self.assertEqual((risk_off["state"], risk_off["target_exposure_pct"]), ("RISK_OFF", 0.0))

    def test_absolute_momentum_filter_rejects_falling_stock(self):
        rows = [row("strong", 0.05, 0.01, 1), row("falling", -0.01, 0.10, 2)]
        decision = {"state": "RISK_ON", "target_exposure_pct": 100}

        admitted = filter_absolute_momentum(rows, self.strategy, decision)

        self.assertEqual([item["symbol"] for item in admitted], ["strong"])

    def test_unknown_data_fails_closed_and_summary_is_presentation_only(self):
        decision = evaluate_market_regime([], self.strategy)
        summary = format_market_regime_summary(decision)

        self.assertEqual(decision["state"], "UNKNOWN")
        self.assertEqual(decision["target_exposure_pct"], 0.0)
        self.assertIn("UNKNOWN", summary)
        self.assertIn("目标仓位 0%", summary)


if __name__ == "__main__":
    unittest.main()
