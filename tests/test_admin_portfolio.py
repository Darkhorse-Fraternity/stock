import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from stock_recommender.admin import AdminHandler
from stock_recommender.parameters import create_strategy
from stock_recommender.portfolio_engine.contracts import AccountSnapshot
from stock_recommender.portfolio_engine.ledger import JsonLedgerStore


class AdminPortfolioIntegrationTests(unittest.TestCase):
    def test_removed_compatibility_routes_return_not_found(self):
        for path in ("/api/config", "/api/performance", "/performance"):
            with self.subTest(path=path):
                captured = {}
                handler = AdminHandler.__new__(AdminHandler)
                handler.path = path
                handler.send_error = lambda status, *args: captured.update(status=status)
                handler.do_GET()
                self.assertEqual(captured["status"], 404)

        for method, path in (("do_PUT", "/api/config"), ("do_POST", "/api/config/reset")):
            with self.subTest(method=method, path=path):
                captured = {}
                handler = AdminHandler.__new__(AdminHandler)
                handler.path = path
                handler._read_json = lambda: {}
                handler.send_error = lambda status, *args: captured.update(status=status)
                getattr(handler, method)()
                self.assertEqual(captured["status"], 404)

    def test_strategy_portfolio_api_and_page_are_served(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "strategies.json"
            portfolio_path = Path(temp_dir) / "portfolios.json"
            environment = {
                "STOCK_AGENT_CONFIG": str(config_path),
                "STOCK_AGENT_PORTFOLIO_PATH": str(portfolio_path),
            }
            with patch.dict(os.environ, environment, clear=False):
                strategy = create_strategy("科技 AI")
                JsonLedgerStore(portfolio_path).create_account(
                    AccountSnapshot(
                        id=f"account-{strategy['id']}",
                        strategy_id=strategy["id"],
                        strategy_revision=strategy["revision"],
                        occurred_at=datetime(
                            2026,
                            7,
                            22,
                            8,
                            0,
                            tzinfo=ZoneInfo("Asia/Shanghai"),
                        ),
                        available_cash=123_456.0,
                        snapshot_id="admin-v2-snapshot",
                    )
                )
                captured = {}
                handler = AdminHandler.__new__(AdminHandler)
                handler.path = f"/api/strategies/{strategy['id']}/portfolio"
                handler._send_json = lambda payload, status=None: captured.update(payload=payload, status=status)
                handler.do_GET()
                payload = captured["payload"]
                page = (Path(__file__).parents[1] / "src/stock_recommender/web/performance.html").read_text(encoding="utf-8")

        self.assertEqual(payload["strategy"]["id"], strategy["id"])
        self.assertEqual(payload["summary"]["nav"], 123_456.0)
        self.assertEqual(payload["summary"]["cash"], 123_456.0)
        self.assertEqual(payload["summary"]["max_positions"], 10)
        self.assertIn("策略表现 · Stock Agent", page)
        self.assertIn("Strategy Portfolio Ledger", page)


if __name__ == "__main__":
    unittest.main()
