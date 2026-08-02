import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from stock_recommender.admin import (
    AdminHandler,
    _serialize_strategy_performance,
    build_strategy_performance,
)
from stock_recommender.parameters import create_strategy
from stock_recommender.portfolio_engine.contracts import (
    AccountSnapshot,
    PerformanceHistoryAvailability,
    PerformanceHistoryStatus,
    PerformanceRuntime,
    PerformanceStrategySource,
    PerformanceSummary,
    PositionSide,
    PositionSnapshot,
    StrategyPerformanceProjection,
)
from stock_recommender.portfolio_engine.ledger import JsonLedgerStore


class AdminPortfolioIntegrationTests(unittest.TestCase):
    class _QuoteAdapter:
        def __init__(self, *, price=120.0, percent=2.5, error=None):
            self.price = price
            self.percent = percent
            self.error = error

        def fetch_watchlist(self, rows, **_kwargs):
            symbols = [item["symbol"] for item in rows]
            if self.error is not None:
                return [], self.error
            return [
                {
                    "symbol": symbol,
                    "price": self.price,
                    "percent": self.percent,
                }
                for symbol in symbols
            ], None

    def test_removed_compatibility_routes_return_not_found(self):
        for path in ("/api/config", "/api/performance", "/performance"):
            with self.subTest(path=path):
                captured = {}
                handler = AdminHandler.__new__(AdminHandler)
                handler.path = path
                handler.send_error = lambda status, *args: captured.update(status=status)
                handler.do_GET()
                self.assertEqual(captured["status"], 404)

    def test_admin_serializer_preserves_service_economics_without_recalculation(self):
        now = datetime(2026, 8, 2, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        source = PerformanceStrategySource(
            id="strategy-serializer",
            name="serializer",
            revision=2,
            stage="paper",
            market="us",
            market_label="美股",
            currency="USD",
            currency_symbol="$",
            initial_cash=100.0,
            max_positions=10,
            config={"initial_cash": 100.0},
            allocation={"model": "equal_weight"},
        )
        projection = StrategyPerformanceProjection(
            generated_at=now,
            quote_error=None,
            strategy=source,
            summary=PerformanceSummary(
                initial_cash=100.0,
                nav=123.0,
                cash=5.0,
                reserved_cash=0.0,
                market_value=118.0,
                cumulative_return_pct=999.0,
                maximum_drawdown_pct=None,
                realized_pnl=7.0,
                unrealized_pnl=16.0,
                position_count=0,
                max_positions=10,
                target_exposure_pct=None,
                closed_trade_count=0,
                win_rate_pct=None,
            ),
            runtime=PerformanceRuntime(),
            nav_history=(),
            positions=(),
            orders=(),
            closed_trades=(),
            events=(),
            history_availability=PerformanceHistoryStatus(
                nav=PerformanceHistoryAvailability(
                    complete=False,
                    source="v2_ledger",
                    reason="test",
                ),
                lifecycle=PerformanceHistoryAvailability(
                    complete=True,
                    source="v2_ledger",
                ),
            ),
        )

        payload = _serialize_strategy_performance(projection)

        self.assertEqual(payload["summary"]["nav"], 123.0)
        self.assertEqual(payload["summary"]["cumulative_return_pct"], 999.0)
        self.assertEqual(payload["summary"]["realized_pnl"], 7.0)

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

    def test_performance_payload_has_complete_fields_and_central_economics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "strategies.json"
            portfolio_path = Path(temp_dir) / "portfolios.json"
            environment = {
                "STOCK_AGENT_CONFIG": str(config_path),
                "STOCK_AGENT_PORTFOLIO_PATH": str(portfolio_path),
            }
            occurred_at = datetime(2026, 8, 2, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            with patch.dict(os.environ, environment, clear=False):
                strategy = create_strategy("typed performance")
                initial_cash = strategy["portfolio"]["initial_cash"]
                JsonLedgerStore(portfolio_path).create_account(
                    AccountSnapshot(
                        id=f"account-{strategy['id']}",
                        strategy_id=strategy["id"],
                        strategy_revision=strategy["revision"],
                        occurred_at=occurred_at,
                        available_cash=initial_cash - 1_000.0,
                        positions=(
                            PositionSnapshot(
                                symbol="AAPL",
                                side=PositionSide.LONG,
                                quantity=10,
                                average_cost=100.0,
                                current_price=105.0,
                                peak_price=110.0,
                                sellable_quantity=10,
                            ),
                        ),
                        snapshot_id="admin-economic-snapshot",
                    )
                )
                with patch(
                    "stock_recommender.admin.get_market_adapter",
                    return_value=self._QuoteAdapter(price=120.0, percent=2.5),
                ):
                    payload = build_strategy_performance(
                        strategy_id=strategy["id"],
                        path=portfolio_path,
                        now=occurred_at,
                    )

        expected_nav = initial_cash - 1_000.0 + 1_200.0
        self.assertAlmostEqual(payload["summary"]["nav"], expected_nav)
        self.assertAlmostEqual(
            payload["summary"]["cumulative_return_pct"],
            (expected_nav / initial_cash - 1.0) * 100.0,
        )
        self.assertEqual(payload["summary"]["unrealized_pnl"], 200.0)
        self.assertEqual(payload["summary"]["realized_pnl"], 0.0)
        self.assertEqual(payload["summary"]["closed_trade_count"], 0)
        self.assertEqual(payload["summary"]["win_rate_pct"], None)
        held = payload["positions"][0]
        self.assertEqual(held["current_price"], 120.0)
        self.assertEqual(held["day_change_pct"], 2.5)
        self.assertEqual(held["return_pct"], 20.0)
        self.assertEqual(held["unrealized_pnl"], 200.0)
        self.assertAlmostEqual(held["weight_pct"], 1_200.0 / expected_nav * 100.0)
        self.assertTrue(
            {
                "slot_id",
                "name",
                "symbol",
                "first_entry_price",
                "first_entry_at",
                "current_price",
                "day_change_pct",
                "return_pct",
                "unrealized_pnl",
                "weight_pct",
                "quantity",
                "sellable_quantity",
                "trailing_active",
                "signal_invalid_days",
                "exit_distance_pct",
            }.issubset(held)
        )
        self.assertTrue(
            {
                "last_successful_pipeline_at",
                "last_successful_pipeline_run_id",
                "last_pipeline_admitted",
                "last_pipeline_stages",
                "last_pipeline_data_quality",
            }.issubset(payload["runtime"])
        )
        self.assertGreaterEqual(len(payload["nav_history"]), 1)
        self.assertEqual(payload["nav_history"][-1]["nav"], expected_nav)
        self.assertEqual(payload["history_availability"]["nav"]["source"], "v2_ledger")
        self.assertIn("complete", payload["history_availability"]["nav"])
        self.assertEqual(payload["history_availability"]["lifecycle"]["source"], "v2_ledger")
        self.assertTrue(payload["events"])
        self.assertTrue(
            {"id", "type", "occurred_at", "message", "strategy_revision"}.issubset(
                payload["events"][0]
            )
        )

    def test_quote_failure_returns_error_and_persisted_valuation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "strategies.json"
            portfolio_path = Path(temp_dir) / "portfolios.json"
            environment = {
                "STOCK_AGENT_CONFIG": str(config_path),
                "STOCK_AGENT_PORTFOLIO_PATH": str(portfolio_path),
            }
            occurred_at = datetime(2026, 8, 2, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            with patch.dict(os.environ, environment, clear=False):
                strategy = create_strategy("quote fallback")
                initial_cash = strategy["portfolio"]["initial_cash"]
                JsonLedgerStore(portfolio_path).create_account(
                    AccountSnapshot(
                        id=f"account-{strategy['id']}",
                        strategy_id=strategy["id"],
                        strategy_revision=strategy["revision"],
                        occurred_at=occurred_at,
                        available_cash=initial_cash - 1_000.0,
                        positions=(
                            PositionSnapshot(
                                symbol="AAPL",
                                side=PositionSide.LONG,
                                quantity=10,
                                average_cost=100.0,
                                current_price=105.0,
                                peak_price=110.0,
                                sellable_quantity=10,
                            ),
                        ),
                        snapshot_id="admin-fallback-snapshot",
                    )
                )
                with patch(
                    "stock_recommender.admin.get_market_adapter",
                    return_value=self._QuoteAdapter(error="provider unavailable"),
                ):
                    payload = build_strategy_performance(
                        strategy_id=strategy["id"],
                        path=portfolio_path,
                        now=occurred_at,
                    )

        self.assertEqual(payload["quote_error"], "provider unavailable")
        self.assertEqual(payload["positions"][0]["current_price"], 105.0)
        self.assertEqual(payload["summary"]["nav"], initial_cash + 50.0)
        self.assertEqual(payload["nav_history"][-1]["source"], "persisted_quote_fallback")


if __name__ == "__main__":
    unittest.main()
