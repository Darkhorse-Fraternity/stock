import math
import unittest
from datetime import date, timedelta

from stock_recommender.enrichment import (
    calculate_technical_indicators,
    enrich_candidates,
    normalize_financial_snapshot,
)
from stock_recommender.parameters import default_strategy_config
from stock_recommender.selection import filter_candidates


def history(closes):
    start = date(2025, 1, 1)
    return [{"date": start + timedelta(days=index), "close": close} for index, close in enumerate(closes)]


class EnrichmentTests(unittest.TestCase):
    def test_technical_indicators_cover_trend_momentum_and_risk(self):
        rows = history([10 + index * 0.1 for index in range(260)])

        result = calculate_technical_indicators(rows)

        self.assertTrue(result["ma5_above_ma20"])
        self.assertTrue(result["ma20_above_ma60"])
        self.assertTrue(result["price_above_ma20"])
        self.assertGreaterEqual(result["rsi"], 99)
        self.assertTrue(result["macd_bullish"])
        self.assertTrue(result["breakout_20d"])
        self.assertEqual(result["distance_52w_high"], 0)
        self.assertGreaterEqual(result["listed_days"], 259)
        self.assertTrue(math.isfinite(result["volatility_20d"]))

    def test_financial_snapshot_uses_documented_eastmoney_fields(self):
        result = normalize_financial_snapshot(
            {
                "REPORT_DATE": "2026-03-31 00:00:00",
                "TOTALOPERATEREVETZ": 15.3,
                "PARENTNETPROFITTZ": 45.8,
                "EPSJBTZ": 46.1,
                "ROEJQ": 12.4,
                "ZZCJLL": 5.1,
                "ROIC": 9.2,
                "XSMLL": 31.5,
                "XSJLL": 8.2,
                "MGJYXJJE": 0.35,
                "FCFF_BACK": 120_000_000,
                "ZCFZL": 42.5,
                "LD": 1.8,
            },
            total_market_cap=4_000_000_000,
        )

        self.assertEqual(result["revenue_growth"], 15.3)
        self.assertEqual(result["profit_growth"], 45.8)
        self.assertEqual(result["roe"], 12.4)
        self.assertTrue(result["operating_cashflow_positive"])
        self.assertTrue(result["free_cashflow_positive"])
        self.assertEqual(result["fcf_yield"], 3)
        self.assertEqual(result["financial_report_date"], "2026-03-31")

    def test_enrichment_runs_only_for_enabled_parameter_families(self):
        config = default_strategy_config()
        config["parameters"]["rsi_min"] = {"enabled": True, "value": 50}
        calls = []

        def history_fetcher(symbol):
            calls.append(symbol)
            return history([10 + index * 0.1 for index in range(80)])

        rows = enrich_candidates(
            [{"symbol": "600001", "price": 18, "turnover": 100, "float_market_cap": 3_000_000_000}],
            strategy=config,
            history_fetcher=history_fetcher,
            financial_fetcher=lambda symbol: self.fail("financial fetch should not run"),
        )

        self.assertEqual(calls, ["600001"])
        self.assertIn("rsi", rows[0])

    def test_enabled_enriched_parameter_filters_missing_and_failing_rows(self):
        config = default_strategy_config()
        config["parameters"]["rsi_min"] = {"enabled": True, "value": 50}
        base = {"price": 18, "turnover": 100, "float_market_cap": 3_000_000_000}

        filtered = filter_candidates(
            [
                {**base, "symbol": "600001", "rsi": 55},
                {**base, "symbol": "600002", "rsi": 45},
                {**base, "symbol": "600003"},
            ],
            strategy=config,
        )

        self.assertEqual([item["symbol"] for item in filtered], ["600001"])


if __name__ == "__main__":
    unittest.main()
