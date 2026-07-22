import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_recommender.parameters import default_strategy_config, normalize_portfolio_config
from stock_recommender.portfolio import (
    build_strategy_performance,
    format_action_notifications,
    load_portfolio_account,
    monitor_portfolio,
    plan_daily_candidates,
)
from stock_recommender.tracking import save_daily_selection


SHANGHAI = ZoneInfo("Asia/Shanghai")


class StrategyPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "portfolio.json"
        self.strategy = default_strategy_config()
        self.strategy.update({"id": "tech-ai", "name": "科技 AI", "revision": 7, "stage": "paper"})
        self.t0 = datetime(2026, 7, 22, 8, 0, tzinfo=SHANGHAI)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _candidate(index, price=10.0, score=None):
        return {
            "symbol": f"60{index:04d}",
            "name": f"测试{index}",
            "price": price,
            "score": score if score is not None else 1 - index / 100,
        }

    @staticmethod
    def _quotes(price=10.0, volume=1_000_000):
        def fetcher(watchlist):
            return (
                [
                    {
                        "symbol": item["symbol"],
                        "name": item.get("name", item["symbol"]),
                        "price": price,
                        "volume": volume,
                        "turnover": price * volume,
                    }
                    for item in watchlist
                ],
                None,
            )

        return fetcher

    def test_portfolio_defaults_enforce_approved_risk_contract(self):
        config = normalize_portfolio_config({"max_positions": 99, "target_weight_pct": 50})

        self.assertEqual(config["max_positions"], 10)
        self.assertEqual(config["target_weight_pct"], 10.0)
        self.assertEqual(config["stop_loss_pct"], 8.0)
        self.assertEqual(config["trailing_activation_pct"], 10.0)
        self.assertEqual(config["trailing_drawdown_pct"], 5.0)
        self.assertEqual(
            (config["warning_drawdown_pct"], config["derisk_drawdown_pct"], config["halt_drawdown_pct"]),
            (12.0, 14.0, 15.0),
        )

    def test_daily_plan_caps_slots_and_is_idempotent(self):
        candidates = [self._candidate(index) for index in range(12)]

        account, events = plan_daily_candidates(self.strategy, candidates, now=self.t0, path=self.path)
        repeated, repeated_events = plan_daily_candidates(self.strategy, candidates, now=self.t0, path=self.path)

        self.assertEqual(len(account["orders"]), 10)
        self.assertEqual(len([slot for slot in account["slots"] if slot["state"] != "EMPTY"]), 10)
        self.assertGreater(account["reserved_cash"], 0)
        self.assertEqual(len(events), 11)
        self.assertEqual(events[0]["type"], "PIPELINE_COMPLETED")
        self.assertEqual([stage["stage"] for stage in account["last_pipeline_trace"]], [
            "candidate_normalization", "portfolio_capacity", "risk_admission",
        ])
        self.assertEqual(len(repeated["orders"]), 10)
        self.assertEqual(repeated_events, [])

    def test_concurrent_daily_runs_commit_once_without_corrupting_ledger(self):
        candidates = [self._candidate(index) for index in range(10)]

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(
                executor.map(
                    lambda _: plan_daily_candidates(self.strategy, candidates, now=self.t0, path=self.path),
                    range(12),
                )
            )

        account = load_portfolio_account("tech-ai", path=self.path)
        self.assertEqual(len(account["orders"]), 10)
        self.assertEqual(account["committed_run_keys"].count("daily:2026-07-22"), 1)
        self.assertEqual(sum(len(events) for _, events in results), 11)

    def test_each_strategy_has_an_independent_cash_and_slot_ledger(self):
        other = {**self.strategy, "id": "value", "name": "低估值", "revision": 2}
        plan_daily_candidates(self.strategy, [self._candidate(1)], now=self.t0, path=self.path)
        plan_daily_candidates(other, [self._candidate(2)], now=self.t0, path=self.path)

        tech = load_portfolio_account("tech-ai", path=self.path)
        value = load_portfolio_account("value", path=self.path)

        self.assertEqual([order["symbol"] for order in tech["orders"]], ["600001"])
        self.assertEqual([order["symbol"] for order in value["orders"]], ["600002"])
        self.assertEqual(tech["cash"], value["cash"])
        self.assertNotEqual(tech["id"], value["id"])

    def test_orders_fill_only_on_later_snapshot_and_obey_t_plus_one(self):
        plan_daily_candidates(self.strategy, [self._candidate(1)], now=self.t0, path=self.path)

        same_time, same_events, _ = monitor_portfolio(
            self.strategy, now=self.t0, path=self.path, quote_fetcher=self._quotes(10.1)
        )
        filled, fill_events, _ = monitor_portfolio(
            self.strategy,
            now=self.t0 + timedelta(hours=2),
            path=self.path,
            quote_fetcher=self._quotes(10.1),
        )

        self.assertEqual(same_time["orders"][0]["status"], "INTENDED")
        self.assertEqual(same_events, [])
        self.assertEqual(filled["orders"][0]["status"], "FILLED")
        self.assertEqual(fill_events[0]["type"], "ORDER_FILLED")
        position = filled["positions"]["600001"]
        self.assertEqual(position["sellable_quantity"], 0)
        self.assertEqual(position["sellable_on"], "2026-07-23")

    def test_stop_loss_creates_exit_then_fills_on_next_snapshot(self):
        plan_daily_candidates(self.strategy, [self._candidate(1)], now=self.t0, path=self.path)
        monitor_portfolio(
            self.strategy,
            now=self.t0 + timedelta(hours=2),
            path=self.path,
            quote_fetcher=self._quotes(10.0),
        )

        exit_planned, events, _ = monitor_portfolio(
            self.strategy,
            now=self.t0 + timedelta(days=1, hours=2),
            path=self.path,
            quote_fetcher=self._quotes(9.0),
        )
        sell_orders = [order for order in exit_planned["orders"] if order["side"] == "SELL"]
        self.assertEqual(len(sell_orders), 1)
        self.assertEqual(sell_orders[0]["status"], "INTENDED")
        self.assertEqual(sell_orders[0]["reason"], "STOP_LOSS")
        self.assertIn("EXIT_TRIGGERED", [event["type"] for event in events])

        exited, exit_events, _ = monitor_portfolio(
            self.strategy,
            now=self.t0 + timedelta(days=1, hours=2, minutes=5),
            path=self.path,
            quote_fetcher=self._quotes(8.95),
        )
        self.assertEqual(exited["positions"], {})
        self.assertEqual(len(exited["closed_trades"]), 1)
        self.assertIn("ORDER_FILLED", [event["type"] for event in exit_events])

    def test_partial_fill_respects_bar_participation_limit(self):
        plan_daily_candidates(self.strategy, [self._candidate(1)], now=self.t0, path=self.path)

        def thin_market(watchlist):
            return ([{"symbol": "600001", "name": "测试1", "price": 10.0, "bar_volume": 2_000}], None)

        account, events, _ = monitor_portfolio(
            self.strategy,
            now=self.t0 + timedelta(hours=2),
            path=self.path,
            quote_fetcher=thin_market,
        )

        self.assertEqual(account["orders"][0]["status"], "PARTIAL")
        self.assertEqual(account["orders"][0]["filled_quantity"], 100)
        self.assertEqual(account["positions"]["600001"]["quantity"], 100)
        self.assertIn("ORDER_PARTIAL", [event["type"] for event in events])

    def test_trailing_profit_exit_uses_peak_drawdown(self):
        plan_daily_candidates(self.strategy, [self._candidate(1)], now=self.t0, path=self.path)
        monitor_portfolio(
            self.strategy,
            now=self.t0 + timedelta(hours=2),
            path=self.path,
            quote_fetcher=self._quotes(10.0),
        )
        advanced, _, _ = monitor_portfolio(
            self.strategy,
            now=self.t0 + timedelta(days=1, hours=2),
            path=self.path,
            quote_fetcher=self._quotes(11.2),
        )
        self.assertTrue(advanced["positions"]["600001"]["trailing_active"])

        pulled_back, events, _ = monitor_portfolio(
            self.strategy,
            now=self.t0 + timedelta(days=1, hours=2, minutes=5),
            path=self.path,
            quote_fetcher=self._quotes(10.5),
        )
        sell = [order for order in pulled_back["orders"] if order["side"] == "SELL"]
        self.assertEqual(sell[-1]["reason"], "TRAILING_STOP")
        self.assertIn("EXIT_TRIGGERED", [event["type"] for event in events])

    def test_strategy_revision_cancels_old_unfilled_intents(self):
        plan_daily_candidates(self.strategy, [self._candidate(1)], now=self.t0, path=self.path)
        revised = {**self.strategy, "revision": 8}

        account, events = plan_daily_candidates(
            revised,
            [self._candidate(2)],
            now=self.t0 + timedelta(days=1),
            path=self.path,
        )

        old = next(order for order in account["orders"] if order["symbol"] == "600001")
        new = next(order for order in account["orders"] if order["symbol"] == "600002")
        self.assertEqual(old["status"], "CANCELLED")
        self.assertEqual(new["strategy_revision"], 8)
        self.assertGreater(account["control_epoch"], 1)
        self.assertIn("STRATEGY_VERSION_ACTIVATED", [event["type"] for event in events])

    def test_portfolio_drawdown_breach_halts_entries_and_emits_risk_event(self):
        candidates = [self._candidate(index) for index in range(10)]
        plan_daily_candidates(self.strategy, candidates, now=self.t0, path=self.path)
        monitor_portfolio(
            self.strategy,
            now=self.t0 + timedelta(hours=2),
            path=self.path,
            quote_fetcher=self._quotes(10.0),
        )

        account, events, _ = monitor_portfolio(
            self.strategy,
            now=self.t0 + timedelta(days=1, hours=2),
            path=self.path,
            quote_fetcher=self._quotes(8.0),
        )

        self.assertEqual(account["risk_level"], "BREACHED")
        self.assertEqual(account["trading_mode"], "MANUAL_HALT")
        self.assertIn("RISK_CHANGED", [event["type"] for event in events])
        self.assertEqual(len([order for order in account["orders"] if order["side"] == "SELL"]), 10)

    def test_strategy_performance_and_notifications_are_strategy_scoped(self):
        _, events = plan_daily_candidates(
            self.strategy, [self._candidate(1)], now=self.t0, path=self.path
        )
        account = load_portfolio_account("tech-ai", path=self.path)
        message = format_action_notifications(
            account,
            events,
            performance_url="https://stock.example.com/performance",
        )
        performance = build_strategy_performance(strategy_id="tech-ai", path=self.path, now=self.t0)

        self.assertIn("科技 AI", message)
        self.assertIn("v7", message)
        self.assertIn("strategy_id=tech-ai", message)
        self.assertEqual(performance["strategy"]["id"], "tech-ai")
        self.assertEqual(performance["summary"]["position_count"], 0)
        self.assertEqual(len(performance["orders"]), 1)

    def test_daily_recommendation_is_committed_to_strategy_portfolio(self):
        report = """📈 **推荐股每小时成交与涨跌跟踪**
- 测试股票 (600001)：最新价 ¥10.00，涨跌幅 +1.00%；成交量 1000 手，成交额 100 万
"""
        symbols = save_daily_selection(
            Path(self.temp_dir.name) / "daily.json",
            report,
            strategy=self.strategy,
            now=self.t0,
            portfolio_path=self.path,
        )
        account = load_portfolio_account("tech-ai", path=self.path)

        self.assertEqual(symbols, ["600001"])
        self.assertEqual(account["orders"][0]["symbol"], "600001")
        self.assertEqual(account["last_pipeline_trace"][-1]["stage"], "risk_admission")


if __name__ == "__main__":
    unittest.main()
