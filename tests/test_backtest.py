import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

from stock_recommender.backtest import get_backtest, run_walk_forward_backtest, start_backtest, walk_forward_windows
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
        "metadata": {
            "point_in_time_complete": point_in_time,
            "strategy_parity_complete": True,
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
        self.assertEqual(result["method"], "rolling_walk_forward_fixed_factor_rank")

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
            for _ in range(100):
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

    def test_legacy_active_strategy_migrates_to_paper(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            strategy = create_strategy("旧活动策略", path=path)
            payload = load_strategy_store(path=path)
            payload["strategies"][0].pop("lifecycle", None)
            path.write_text(__import__("json").dumps(payload, ensure_ascii=False), encoding="utf-8")

            migrated = load_strategy_config(path=path)

            self.assertEqual(migrated["lifecycle"]["stage"], "paper")


if __name__ == "__main__":
    unittest.main()
