import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_recommender.parameters import default_strategy_config, normalize_portfolio_config
from stock_recommender.portfolio import (
    build_strategy_performance,
    create_portfolio_account,
    format_action_notifications,
    load_portfolio_account,
    monitor_portfolio,
    plan_daily_candidates as _plan_daily_candidates,
)
from stock_recommender.tracking import save_daily_selection
from stock_recommender.runtime import StrategyRuntimeError
from recommendation_fixtures import FULL_EXPOSURE_MARKET_REGIME, candidates_with_positive_momentum, make_recommendation_plan


SHANGHAI = ZoneInfo("Asia/Shanghai")


def plan_daily_candidates(strategy, candidates, *, market_regime=None, **kwargs):
    return _plan_daily_candidates(
        strategy,
        candidates_with_positive_momentum(candidates),
        market_regime=market_regime or FULL_EXPOSURE_MARKET_REGIME,
        **kwargs,
    )


class StrategyPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "portfolio.json"
        self.strategy = default_strategy_config()
        self.strategy.update({"id": "tech-ai", "name": "科技 AI", "revision": 7})
        self.strategy["lifecycle"]["stage"] = "paper"
        self.t0 = datetime(2026, 7, 22, 8, 0, tzinfo=SHANGHAI)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_draft_strategy_cannot_write_portfolio(self):
        draft = default_strategy_config()
        draft["id"] = "draft-strategy"
        with self.assertRaises(StrategyRuntimeError):
            plan_daily_candidates(draft, [self._candidate(1)], now=self.t0, path=self.path)
        self.assertFalse(self.path.exists())

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
            "candidate_normalization", "market_regime", "portfolio_capacity", "risk_admission",
        ])
        self.assertEqual(len(repeated["orders"]), 10)
        self.assertEqual(repeated_events, [])

    def test_in_memory_account_reuses_pipeline_without_writing_ledger(self):
        candidates = [self._candidate(index) for index in range(3)]
        isolated = create_portfolio_account(self.strategy, now=self.t0)

        isolated, isolated_events = plan_daily_candidates(
            self.strategy,
            candidates,
            now=self.t0,
            account=isolated,
        )

        self.assertFalse(self.path.exists())
        persisted, persisted_events = plan_daily_candidates(self.strategy, candidates, now=self.t0, path=self.path)
        self.assertEqual([order["symbol"] for order in isolated["orders"]], [order["symbol"] for order in persisted["orders"]])
        self.assertEqual([event["type"] for event in isolated_events], [event["type"] for event in persisted_events])
        self.assertEqual(isolated["reserved_cash"], persisted["reserved_cash"])

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
        self.assertEqual(
            account["committed_run_keys"].count(
                "daily:2026-07-22:strategy-r7:entry-pipeline-v1.0.0"
            ),
            1,
        )
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

    def test_explicit_market_regime_caps_entries_and_exits_in_risk_off(self):
        candidates = [
            {
                **self._candidate(index),
                "signal_features": {"momentum20": 0.05, "momentum60": 0.08, "trend": 2},
            }
            for index in range(10)
        ]
        neutral = {
            "model": "trend_breadth_v1",
            "state": "NEUTRAL",
            "target_exposure_pct": 40,
        }
        account, _ = plan_daily_candidates(
            self.strategy,
            candidates,
            now=self.t0,
            path=self.path,
            market_regime=neutral,
        )
        self.assertEqual(len([order for order in account["orders"] if order["side"] == "BUY"]), 4)
        self.assertEqual(account["target_exposure_pct"], 40.0)

        monitor_portfolio(
            self.strategy,
            now=self.t0 + timedelta(hours=2),
            path=self.path,
            quote_fetcher=self._quotes(10.0),
        )
        risk_off = {
            "model": "trend_breadth_v1",
            "state": "RISK_OFF",
            "target_exposure_pct": 0,
        }
        account, events = plan_daily_candidates(
            self.strategy,
            [],
            now=self.t0 + timedelta(days=1),
            path=self.path,
            market_regime=risk_off,
        )

        sell_orders = [order for order in account["orders"] if order["side"] == "SELL"]
        self.assertEqual(len(sell_orders), 4)
        self.assertTrue(all(order["reason"] == "MARKET_REGIME_RISK_OFF" for order in sell_orders))
        self.assertIn("EXIT_TRIGGERED", [event["type"] for event in events])

    def test_unknown_regime_from_data_gap_blocks_entries_without_liquidating_positions(self):
        plan_daily_candidates(self.strategy, [self._candidate(1)], now=self.t0, path=self.path)
        monitor_portfolio(
            self.strategy,
            now=self.t0 + timedelta(hours=2),
            path=self.path,
            quote_fetcher=self._quotes(10.0),
        )
        unknown = {
            "model": "trend_breadth_v1",
            "state": "UNKNOWN",
            "target_exposure_pct": 0,
            "reason": "历史特征覆盖不足",
        }

        account, events = plan_daily_candidates(
            self.strategy,
            [],
            now=self.t0 + timedelta(days=1),
            path=self.path,
            market_regime=unknown,
            data_quality={"status": "BLOCKED", "reason": "历史特征覆盖不足"},
        )

        self.assertIn("600001", account["positions"])
        self.assertEqual([order for order in account["orders"] if order["side"] == "SELL"], [])
        pipeline = next(event for event in events if event["type"] == "PIPELINE_COMPLETED")
        self.assertEqual(pipeline["data"]["data_quality"]["status"], "BLOCKED")

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
            performance_url="https://stock.example.com/strategies/tech-ai/portfolio",
        )
        performance = build_strategy_performance(strategy_id="tech-ai", path=self.path, now=self.t0)

        self.assertIn("科技 AI", message)
        self.assertIn("v7", message)
        self.assertIn("/strategies/tech-ai/portfolio", message)
        self.assertEqual(performance["strategy"]["id"], "tech-ai")
        self.assertEqual(performance["summary"]["position_count"], 0)
        self.assertEqual(len(performance["orders"]), 1)
        self.assertEqual(performance["runtime"]["last_successful_pipeline_at"], self.t0.isoformat())
        self.assertEqual(performance["runtime"]["last_pipeline_admitted"], 1)
        self.assertEqual(
            [stage["stage"] for stage in performance["runtime"]["last_pipeline_stages"]],
            ["candidate_normalization", "market_regime", "portfolio_capacity", "risk_admission"],
        )

    def test_daily_recommendation_without_decision_does_not_regenerate_portfolio(self):
        plan = make_recommendation_plan(
            [{"symbol": "600001", "name": "测试股票", "price": 10.0, "percent": 1.0, "score": 0.8}],
            now=self.t0,
        )
        with self.assertRaisesRegex(ValueError, "portfolio_decision"):
            save_daily_selection(
                Path(self.temp_dir.name) / "daily.json",
                plan,
                strategy=self.strategy,
                now=self.t0,
                portfolio_path=self.path,
            )
        account = load_portfolio_account("tech-ai", path=self.path)

        self.assertIsNone(account)


if __name__ == "__main__":
    unittest.main()
