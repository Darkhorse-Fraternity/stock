import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

from stock_recommender.context import extract_market_payload, generate_agent_context
from stock_recommender.parameters import create_strategy, default_strategy_config
from stock_recommender.strategy_runs import (
    StrategyRunInProgressError,
    get_strategy_run,
    list_strategy_runs,
    start_strategy_run,
)


class StrategyExecutionTests(unittest.TestCase):
    @staticmethod
    def _history(symbol):
        start = date(2026, 1, 1)
        return [
            {"date": start + timedelta(days=index), "open": 10 + index / 10, "close": 10 + index / 10, "volume": 1000 + index}
            for index in range(100)
        ]

    def test_context_uses_explicit_strategy_instead_of_active_strategy(self):
        strategy = default_strategy_config()
        strategy["parameters"]["change_pct_min"] = {"enabled": True, "value": 5}
        rows = [{
            "symbol": "300130",
            "name": "测试股票",
            "price": 20,
            "percent": 2,
            "change": 0.4,
            "turnover": 300_000_000,
            "turnover_rate": 3,
            "float_market_cap": 5_000_000_000,
            "source": "测试行情",
        }]

        context = generate_agent_context(
            board_fetcher=lambda *args, **kwargs: (rows, None),
            candidate_limit=3,
            strategy=strategy,
            history_fetcher=self._history,
        )

        self.assertEqual(extract_market_payload(context)["candidate_count"], 0)

    def test_context_scoring_uses_explicit_strategy_risk_threshold(self):
        strategy = default_strategy_config()
        strategy["parameters"]["chase_risk_pct"] = {"enabled": True, "value": 4}
        rows = [{
            "symbol": "300130",
            "name": "测试股票",
            "price": 20,
            "percent": 5,
            "change": 1,
            "turnover": 300_000_000,
            "turnover_rate": 3,
            "float_market_cap": 5_000_000_000,
            "source": "测试行情",
        }]

        context = generate_agent_context(
            board_fetcher=lambda *args, **kwargs: (rows, None),
            candidate_limit=1,
            strategy=strategy,
            history_fetcher=self._history,
        )

        candidate = extract_market_payload(context)["candidates"][0]
        self.assertEqual(candidate["risk_hint"], "高")
        self.assertIn("超过追高阈值", candidate["machine_reasons"][0])

    def test_run_is_persisted_and_returns_report(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "strategies.json"
            runs_path = Path(directory) / "runs.json"
            strategy = create_strategy("测试策略", path=config_path)
            run = start_strategy_run(
                strategy["id"],
                config_path=config_path,
                path=runs_path,
                executor=lambda strategy_id: f"report for {strategy_id}",
            )

            completed = self._wait_for_run(run["id"], runs_path)
            history = list_strategy_runs(strategy["id"], path=runs_path)

            self.assertEqual(completed["status"], "succeeded")
            self.assertIn(strategy["id"], completed["report"])
            self.assertEqual(history[0]["id"], run["id"])
            self.assertNotIn("report", history[0])

    def test_only_one_strategy_run_can_execute_at_a_time(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "strategies.json"
            runs_path = Path(directory) / "runs.json"
            first = create_strategy("策略一", path=config_path)
            second = create_strategy("策略二", path=config_path)
            entered = threading.Event()
            release = threading.Event()

            def blocking_executor(strategy_id):
                entered.set()
                release.wait(timeout=2)
                return strategy_id

            run = start_strategy_run(first["id"], config_path=config_path, path=runs_path, executor=blocking_executor)
            self.assertTrue(entered.wait(timeout=1))
            with self.assertRaises(StrategyRunInProgressError):
                start_strategy_run(second["id"], config_path=config_path, path=runs_path, executor=lambda _: "done")
            release.set()
            self.assertEqual(self._wait_for_run(run["id"], runs_path)["status"], "succeeded")

    def test_missing_strategy_cannot_start(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(KeyError):
                start_strategy_run(
                    "missing",
                    config_path=Path(directory) / "strategies.json",
                    path=Path(directory) / "runs.json",
                    executor=lambda _: "done",
                )

    @staticmethod
    def _wait_for_run(run_id: str, path: Path) -> dict:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            run = get_strategy_run(run_id, path=path)
            if run and run["status"] in {"succeeded", "failed"}:
                return run
            time.sleep(0.01)
        raise AssertionError("strategy run did not finish")


if __name__ == "__main__":
    unittest.main()
