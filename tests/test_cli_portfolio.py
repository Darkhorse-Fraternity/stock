import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from stock_recommender import cli
from stock_recommender.parameters import default_strategy_config


class PortfolioCliTests(unittest.TestCase):
    def setUp(self):
        self.strategy = default_strategy_config()
        self.strategy.update({"id": "tech-ai", "name": "科技 AI", "revision": 4})
        self.strategy["lifecycle"]["stage"] = "paper"
        self.account = {
            "strategy_id": "tech-ai",
            "strategy_name": "科技 AI",
            "strategy_revision": 4,
            "strategy_stage": "paper",
            "initial_cash": 1_000_000,
            "latest_nav": 1_010_000,
            "cash": 910_000,
            "risk_level": "NORMAL",
            "trading_mode": "RUNNING",
            "portfolio_config": {"max_positions": 10},
            "positions": {},
            "orders": [],
            "nav_history": [{"drawdown_pct": 0.0}],
        }

    def test_hourly_mode_prints_strategy_portfolio_and_deep_link(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "tracking.md"
            environment = {
                "STOCK_AGENT_MODE": "track",
                "STOCK_AGENT_OUTPUT": str(output),
                "STOCK_AGENT_PERFORMANCE_URL": "https://stock.example.com/performance",
                "STOCK_AGENT_SCHEDULE_GUARD": "0",
            }
            stream = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), patch.object(cli, "load_strategy_config", return_value=self.strategy), patch.object(cli, "monitor_portfolio", return_value=(self.account, [], None)), redirect_stdout(stream):
                cli.main()

            report = output.read_text(encoding="utf-8")
        self.assertIn("策略持仓每小时报告", report)
        self.assertIn("科技 AI · v4", report)
        self.assertIn("strategy_id=tech-ai", report)
        self.assertEqual(stream.getvalue().strip(), report.strip())

    def test_risk_mode_is_silent_when_no_action_occurs(self):
        environment = {
            "STOCK_AGENT_MODE": "risk",
            "STOCK_AGENT_OUTPUT": "",
            "STOCK_AGENT_SCHEDULE_GUARD": "0",
        }
        stream = io.StringIO()
        with patch.dict(os.environ, environment, clear=False), patch.object(cli, "load_strategy_config", return_value=self.strategy), patch.object(cli, "monitor_portfolio", return_value=(self.account, [], None)), redirect_stdout(stream):
            cli.main()

        self.assertEqual(stream.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
