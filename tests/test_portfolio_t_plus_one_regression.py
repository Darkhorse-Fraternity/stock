import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_recommender.parameters import default_strategy_config
from stock_recommender.portfolio import monitor_portfolio, plan_daily_candidates


SHANGHAI = ZoneInfo("Asia/Shanghai")


class PortfolioTPlusOneRegressionTests(unittest.TestCase):
    @staticmethod
    def _quotes(price):
        def fetcher(watchlist):
            return (
                [
                    {
                        "symbol": item["symbol"],
                        "name": item.get("name", item["symbol"]),
                        "price": price,
                        "volume": 1_000_000,
                        "turnover": price * 1_000_000,
                    }
                    for item in watchlist
                ],
                None,
            )

        return fetcher

    def test_existing_exit_fills_on_first_t_plus_one_snapshot(self):
        # Regression: ISSUE-001 - T+1 inventory was unlocked after order execution.
        # Found by /qa on 2026-07-22.
        # Report: .gstack/qa-reports/qa-report-stock-agent-2026-07-22.md
        strategy = default_strategy_config()
        strategy.update({"id": "tech-ai", "name": "科技 AI", "revision": 7, "stage": "paper"})
        t0 = datetime(2026, 7, 22, 8, 0, tzinfo=SHANGHAI)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.json"
            plan_daily_candidates(
                strategy,
                [{"symbol": "600001", "name": "测试1", "price": 10.0, "score": 0.99}],
                now=t0,
                path=path,
            )
            monitor_portfolio(
                strategy,
                now=t0 + timedelta(hours=2),
                path=path,
                quote_fetcher=self._quotes(10.0),
            )
            exit_planned, _, _ = monitor_portfolio(
                strategy,
                now=t0 + timedelta(hours=3),
                path=path,
                quote_fetcher=self._quotes(9.0),
            )

            sell_order = next(order for order in exit_planned["orders"] if order["side"] == "SELL")
            self.assertEqual(sell_order["status"], "INTENDED")
            self.assertEqual(sell_order["reason"], "STOP_LOSS")
            self.assertEqual(exit_planned["positions"]["600001"]["sellable_quantity"], 0)

            exited, events, _ = monitor_portfolio(
                strategy,
                now=t0 + timedelta(days=1, hours=2),
                path=path,
                quote_fetcher=self._quotes(8.95),
            )

        self.assertEqual(exited["positions"], {})
        self.assertEqual(len(exited["closed_trades"]), 1)
        self.assertEqual(exited["closed_trades"][0]["exit_reason"], "STOP_LOSS")
        self.assertIn("ORDER_FILLED", [event["type"] for event in events])


if __name__ == "__main__":
    unittest.main()
