import json
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

from stock_recommender.backtest import get_backtest, load_backtest_dataset_file, run_walk_forward_backtest, start_backtest, walk_forward_windows
from stock_recommender.parameters import (
    StrategyLifecycleError,
    create_strategy,
    create_strategy_revision,
    load_strategy_config,
    load_strategy_store,
    record_backtest_evaluation,
    record_paper_session,
    save_strategy_config,
    transition_strategy_stage,
)
from stock_recommender.portfolio_backtest import normalize_universe_snapshots, universe_for_day


def synthetic_dataset(days=240, *, point_in_time=True):
    start = date(2024, 1, 1)
    panel = {}
    for index, symbol in enumerate(["000001", "000002", "000003", "000004", "000005"], 1):
        # Keep the signal profitable after the gate doubles round-trip costs.
        growth = 0.001 * index
        rows = []
        for offset in range(days):
            current = start + timedelta(days=offset)
            price = 10 * ((1 + growth) ** offset)
            rows.append(
                {
                    "date": current.isoformat(),
                    "open": price,
                    "close": price * (1 + growth / 2),
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "volume": 100000 + offset * (100 + index),
                    "open_volume": 100000 + offset * (100 + index),
                    "close_volume": 100000 + offset * (100 + index),
                    "entry_price": price,
                    "exit_price": price * (1 + growth / 2),
                    "upper_limit": price * 1.1,
                    "lower_limit": price * 0.9,
                }
            )
        panel[symbol] = rows
    benchmark = [
        {"date": (start + timedelta(days=offset)).isoformat(), "open": 100, "close": 100, "volume": 1}
        for offset in range(days)
    ]
    return {
        "panel": panel,
        "benchmark": benchmark,
        "universe_by_date": {
            (start + timedelta(days=offset)).isoformat(): list(panel)
            for offset in range(days)
        },
        "metadata": {
            "point_in_time_complete": point_in_time,
            "benchmark_complete": True,
            "strategy_parity_complete": True,
            "execution_data_complete": True,
            "execution_price_mode": "intraday_0935_1500",
            "corporate_actions_complete": True,
            "parameter_trials": 1,
        },
    }


def compact_validation(strategy):
    strategy["validation"].update(
        {
            "lookback_days": 60,
            "history_days_min": 180,
            "train_days": 70,
            "validation_days": 10,
            "test_days": 20,
            "gap_days": 2,
            "holding_period_days": 3,
            "top_n": 2,
            "minimum_oos_events": 40,
            "minimum_oos_months": 3,
            "minimum_positive_fold_ratio": 0.6,
            "minimum_dsr_probability": 0.9,
            "maximum_drawdown_pct": 20,
        }
    )
    return strategy


class BacktestTests(unittest.TestCase):
    def test_point_in_time_universe_never_uses_future_snapshot(self):
        snapshots = normalize_universe_snapshots(
            {
                "2026-01-01": ["600001"],
                "2026-02-01": ["600002"],
            }
        )

        symbols, covered = universe_for_day(snapshots, date(2026, 1, 31), {"600999"})

        self.assertTrue(covered)
        self.assertEqual(symbols, {"600001"})

    def test_walk_forward_uses_ordered_non_overlapping_test_windows(self):
        days = [date(2024, 1, 1) + timedelta(days=index) for index in range(140)]
        windows = walk_forward_windows(days, {"train_days": 70, "validation_days": 10, "gap_days": 2, "test_days": 20})

        self.assertEqual(len(windows), 2)
        self.assertLess(windows[0]["validation_end"], windows[0]["test_start"])
        self.assertLess(windows[0]["test_end"], windows[1]["test_end"])

    def test_backtest_calculates_oos_metrics_cost_stress_and_gate(self):
        strategy = compact_validation(load_strategy_config(path=Path("/missing")))
        strategy["id"] = "strategy-1"

        result = run_walk_forward_backtest(synthetic_dataset(), strategy)

        self.assertGreaterEqual(result["metrics"]["oos_events"], 40)
        self.assertGreater(result["metrics"]["mean_excess_return_pct"], 0)
        self.assertLess(result["metrics"]["stressed_mean_excess_return_pct"], result["metrics"]["mean_excess_return_pct"])
        self.assertTrue(result["approval_gate"]["passed"])
        self.assertEqual(result["method"], "rolling_walk_forward_portfolio_pipeline_v1")
        self.assertEqual(result["metadata"]["signal_model"], "factor_rank_v1")
        self.assertEqual(result["execution"]["data_cutoff"], "previous_trading_day_close")
        self.assertEqual(result["execution"]["exit"], "shared_portfolio_pipeline")
        self.assertLessEqual(result["metrics"]["maximum_positions_observed"], 10)
        self.assertGreater(result["metrics"]["maximum_drawdown_pct"], -100)

    def test_long_short_result_exposes_engine_costs_and_borrow_readiness(self):
        strategy = compact_validation(load_strategy_config(path=Path("/missing")))
        strategy["id"] = "strategy-long-short"
        strategy["market"] = "us"
        strategy["parameters"]["market"]["value"] = "us"
        strategy["exposure_policy"]["mode"] = "LONG_SHORT"

        result = run_walk_forward_backtest(synthetic_dataset(), strategy)
        metadata = result["metadata"]
        check = next(
            item
            for item in result["approval_gate"]["checks"]
            if item["id"] == "borrow_history"
        )

        self.assertEqual(metadata["exposure_mode"], "LONG_SHORT")
        self.assertEqual(metadata["cost_multiplier"], 1.0)
        self.assertIn("financing_apr_pct", metadata)
        self.assertIn("borrow_apr_pct", metadata)
        self.assertTrue(metadata["borrow_cost_estimated"])
        self.assertFalse(metadata["borrow_history_complete"])
        self.assertIn("financing_cost", result["metrics"])
        self.assertIn("borrow_cost", result["metrics"])
        self.assertEqual(result["execution"]["valuation"], "mark_to_market_nav")
        self.assertFalse(check["passed"])

    def test_complete_historical_borrow_satisfies_long_short_check(self):
        strategy = compact_validation(load_strategy_config(path=Path("/missing")))
        strategy["id"] = "strategy-long-short-complete"
        strategy["market"] = "us"
        strategy["parameters"]["market"]["value"] = "us"
        strategy["exposure_policy"]["mode"] = "LONG_SHORT"
        dataset = synthetic_dataset()
        dataset["metadata"]["borrow_history_complete"] = True
        dataset["borrow_history"] = {
            row["date"]: {
                symbol: {
                    "shortable": True,
                    "easy_to_borrow": True,
                    "available_quantity": 1_000_000,
                    "borrow_apr_pct": 8.0,
                }
                for symbol in dataset["panel"]
            }
            for row in dataset["benchmark"]
        }

        result = run_walk_forward_backtest(dataset, strategy)
        check = next(
            item
            for item in result["approval_gate"]["checks"]
            if item["id"] == "borrow_history"
        )

        self.assertTrue(result["metadata"]["borrow_history_complete"])
        self.assertFalse(result["metadata"]["borrow_cost_estimated"])
        self.assertTrue(check["passed"])

    def test_current_constituent_dataset_cannot_pass_live_gate(self):
        strategy = compact_validation(load_strategy_config(path=Path("/missing")))
        result = run_walk_forward_backtest(synthetic_dataset(point_in_time=False), strategy)
        check = next(item for item in result["approval_gate"]["checks"] if item["id"] == "point_in_time")

        self.assertFalse(check["passed"])
        self.assertFalse(result["approval_gate"]["passed"])

    def test_proxy_model_cannot_pass_strategy_parity_gate(self):
        strategy = compact_validation(load_strategy_config(path=Path("/missing")))
        dataset = synthetic_dataset()
        dataset["metadata"]["strategy_parity_complete"] = False

        result = run_walk_forward_backtest(dataset, strategy)
        check = next(item for item in result["approval_gate"]["checks"] if item["id"] == "strategy_parity")

        self.assertFalse(check["passed"])
        self.assertFalse(result["approval_gate"]["passed"])

    def test_missing_benchmark_or_execution_data_cannot_pass_gate(self):
        strategy = compact_validation(load_strategy_config(path=Path("/missing")))
        dataset = synthetic_dataset()
        dataset["metadata"]["benchmark_complete"] = False
        dataset["metadata"]["execution_data_complete"] = False

        result = run_walk_forward_backtest(dataset, strategy)
        checks = {item["id"]: item for item in result["approval_gate"]["checks"]}

        self.assertFalse(checks["benchmark"]["passed"])
        self.assertFalse(checks["execution_data"]["passed"])
        self.assertFalse(result["approval_gate"]["passed"])

    def test_missing_corporate_actions_cannot_pass_gate(self):
        strategy = compact_validation(load_strategy_config(path=Path("/missing")))
        dataset = synthetic_dataset()
        dataset["metadata"]["corporate_actions_complete"] = False

        result = run_walk_forward_backtest(dataset, strategy)
        check = next(item for item in result["approval_gate"]["checks"] if item["id"] == "corporate_actions")

        self.assertFalse(check["passed"])
        self.assertFalse(result["approval_gate"]["passed"])

    def test_execution_capability_claim_is_verified_against_rows(self):
        strategy = compact_validation(load_strategy_config(path=Path("/missing")))
        dataset = synthetic_dataset()
        for rows in dataset["panel"].values():
            for row in rows:
                row.pop("open_volume", None)

        result = run_walk_forward_backtest(dataset, strategy)
        check = next(item for item in result["approval_gate"]["checks"] if item["id"] == "execution_data")

        self.assertFalse(check["passed"])
        self.assertFalse(result["approval_gate"]["passed"])

    def test_missing_point_in_time_membership_cannot_pass_gate(self):
        strategy = compact_validation(load_strategy_config(path=Path("/missing")))
        dataset = synthetic_dataset()
        dataset.pop("universe_by_date")

        result = run_walk_forward_backtest(dataset, strategy)
        check = next(item for item in result["approval_gate"]["checks"] if item["id"] == "point_in_time")

        self.assertFalse(check["passed"])
        self.assertFalse(result["approval_gate"]["passed"])

    def test_explicit_point_in_time_period_does_not_require_a_full_walk_forward_window(self):
        strategy = compact_validation(load_strategy_config(path=Path("/missing")))
        dataset = synthetic_dataset(days=240)
        dataset["evaluation_period"] = {"start": "2024-07-20", "end": "2024-08-10"}

        result = run_walk_forward_backtest(dataset, strategy)

        self.assertEqual(result["method"], "point_in_time_holdout_portfolio_pipeline_v1")
        self.assertEqual(result["metrics"]["folds"], 1)
        self.assertGreater(result["metrics"]["oos_events"], 0)

    def test_daily_proxy_prices_remain_usable_when_exact_execution_fields_are_absent(self):
        strategy = compact_validation(load_strategy_config(path=Path("/missing")))
        dataset = synthetic_dataset(days=240)
        dataset["evaluation_period"] = {"start": "2024-07-20", "end": "2024-08-10"}
        dataset["metadata"]["execution_data_complete"] = False
        dataset["metadata"]["execution_price_mode"] = "daily_open_close_proxy"
        for rows in dataset["panel"].values():
            for row in rows:
                row.pop("entry_price", None)
                row.pop("exit_price", None)

        result = run_walk_forward_backtest(dataset, strategy)

        self.assertNotEqual(result["metrics"]["cumulative_return_pct"], 0)
        check = next(item for item in result["approval_gate"]["checks"] if item["id"] == "execution_data")
        self.assertFalse(check["passed"])

    def test_local_dataset_contract_loads_without_production_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "point-in-time.json"
            expected = synthetic_dataset(days=5)
            path.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")

            loaded = load_backtest_dataset_file(path)

        self.assertEqual(set(loaded["panel"]), set(expected["panel"]))
        self.assertEqual(loaded["metadata"]["source"], "local_point_in_time_dataset")

    def test_async_backtest_persists_result_and_moves_strategy_to_paper(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "strategies.json"
            results_path = Path(directory) / "backtests.json"
            strategy = create_strategy("异步回测", path=config_path)
            strategy = compact_validation(strategy)
            save_strategy_config(strategy, path=config_path, strategy_id=strategy["id"])

            queued = start_backtest(
                strategy["id"],
                path=results_path,
                config_path=config_path,
                data_loader=lambda _: synthetic_dataset(),
            )
            completed = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                completed = get_backtest(queued["id"], path=results_path)
                if completed and completed["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.01)

            self.assertEqual(completed["status"], "succeeded")
            saved = load_strategy_config(path=config_path, strategy_id=strategy["id"])
            self.assertEqual(saved["lifecycle"]["stage"], "paper")
            self.assertEqual(saved["validation"]["last_backtest"]["id"], queued["id"])

    def test_strategy_lifecycle_requires_gate_and_paper_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategies.json"
            strategy = create_strategy("生命周期策略", path=path)
            with self.assertRaises(StrategyLifecycleError):
                transition_strategy_stage(strategy["id"], "live", path=path)

            transition_strategy_stage(strategy["id"], "backtesting", path=path)
            record_backtest_evaluation(
                strategy["id"],
                {"id": "bt-1", "approval_gate": {"passed": True, "checks": []}},
                path=path,
            )
            for offset in range(40):
                record_paper_session(strategy["id"], (date(2026, 1, 1) + timedelta(days=offset)).isoformat(), path=path)
            live = transition_strategy_stage(strategy["id"], "live", approved_by="tester", path=path)

            self.assertEqual(live["lifecycle"]["stage"], "live")
            self.assertEqual(live["lifecycle"]["paper_sessions"], 40)
            live["parameters"]["price_min"] = {"enabled": True, "value": 20}
            with self.assertRaises(StrategyLifecycleError):
                save_strategy_config(live, path=path, strategy_id=live["id"])

            revision = create_strategy_revision(live["id"], path=path)
            self.assertEqual(revision["revision"], 2)
            self.assertEqual(revision["parent_strategy_id"], live["id"])
            self.assertEqual(revision["lifecycle"]["stage"], "draft")

    def test_active_strategy_without_lifecycle_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            create_strategy("无生命周期策略", path=path)
            payload = load_strategy_store(path=path)
            payload["strategies"][0].pop("lifecycle", None)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(StrategyLifecycleError, "lifecycle"):
                load_strategy_config(path=path)


if __name__ == "__main__":
    unittest.main()
