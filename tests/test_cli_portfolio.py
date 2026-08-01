import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
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
                "STOCK_AGENT_PUBLIC_URL": "https://stock.example.com",
                "STOCK_AGENT_SCHEDULE_GUARD": "0",
            }
            stream = io.StringIO()
            report_text = "\n".join(
                (
                    "📊 **策略持仓每小时报告**",
                    "策略：科技 AI · v4",
                    "https://stock.example.com/strategies/tech-ai/portfolio",
                )
            )
            with patch.dict(os.environ, environment, clear=False), patch.object(
                cli, "load_strategy_config", return_value=self.strategy
            ), patch.object(
                cli,
                "open_portfolio_runtime",
                return_value=("engine", "account"),
            ), patch.object(
                cli,
                "process_portfolio_runtime",
                return_value=("batch", "snapshot"),
            ) as process, patch.object(
                cli, "format_portfolio_snapshot", return_value=report_text
            ), redirect_stdout(stream):
                cli.main()

            process.assert_called_once()

            report = output.read_text(encoding="utf-8")
        self.assertIn("策略持仓每小时报告", report)
        self.assertIn("科技 AI · v4", report)
        self.assertIn("https://stock.example.com/strategies/tech-ai/portfolio", report)
        self.assertEqual(stream.getvalue().strip(), report.strip())

    def test_risk_mode_is_silent_when_no_action_occurs(self):
        environment = {
            "STOCK_AGENT_MODE": "risk",
            "STOCK_AGENT_OUTPUT": "",
            "STOCK_AGENT_SCHEDULE_GUARD": "0",
        }
        stream = io.StringIO()
        with patch.dict(os.environ, environment, clear=False), patch.object(
            cli, "load_strategy_config", return_value=self.strategy
        ), patch.object(
            cli,
            "open_portfolio_runtime",
            return_value=("engine", "account"),
        ), patch.object(
            cli,
            "process_portfolio_runtime",
            return_value=("batch", "snapshot"),
        ) as process, patch.object(
            cli, "format_portfolio_actions", return_value=""
        ), redirect_stdout(stream):
            cli.main()

        self.assertEqual(stream.getvalue(), "")
        process.assert_called_once()

    def test_scheduled_ai_passes_runtime_to_one_plan_and_tracks_exact_plan(self):
        environment = {
            "STOCK_AGENT_MODE": "ai",
            "STOCK_AGENT_EXECUTION_KIND": "scheduled",
            "STOCK_AGENT_OUTPUT": "",
            "STOCK_AGENT_SCHEDULE_GUARD": "0",
            "STOCK_AGENT_DELIVERY_RUN": "0",
        }
        plan = object()
        engine = object()
        account = object()
        with patch.dict(os.environ, environment, clear=False), patch.object(
            cli, "load_strategy_config", return_value=self.strategy
        ), patch.object(
            cli, "open_portfolio_runtime", return_value=(engine, account)
        ), patch.object(
            cli, "should_publish_at_market_open", return_value=True
        ), patch.object(
            cli, "collect_recommendation_plan", return_value=plan
        ) as collect, patch.object(
            cli, "save_daily_selection"
        ) as save, patch.object(
            cli,
            "render_ai_report_result",
            return_value=SimpleNamespace(report="AI report"),
        ), redirect_stdout(io.StringIO()):
            cli.main()

        self.assertIs(collect.call_args.kwargs["portfolio_engine"], engine)
        self.assertIs(collect.call_args.kwargs["portfolio_account"], account)
        self.assertIs(save.call_args.args[1], plan)
        self.assertIs(save.call_args.kwargs["portfolio_engine"], engine)


if __name__ == "__main__":
    unittest.main()
