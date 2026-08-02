import json
import os
import re
import tempfile
import unittest
from copy import deepcopy
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from stock_recommender.admin import (
    AdminHandler,
    _serialize_strategy_performance,
    build_strategy_performance,
)
from stock_recommender.parameters import create_strategy, load_strategy_config, save_strategy_config
from stock_recommender.portfolio_engine.contracts import (
    AccountSnapshot,
    PerformanceHistoryAvailability,
    PerformanceHistoryStatus,
    PerformanceEventView,
    PerformanceNavPoint,
    PerformanceOrder,
    PerformancePosition,
    PerformanceRuntime,
    PerformanceStrategySource,
    PerformanceSummary,
    PositionSide,
    PositionSnapshot,
    StrategyPerformanceProjection,
)
from stock_recommender.portfolio_engine.ledger import JsonLedgerStore


class AdminPortfolioIntegrationTests(unittest.TestCase):
    def test_strategy_get_and_put_expose_policies_and_policy_put_creates_revision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "strategies.json"
            with patch.dict(os.environ, {"STOCK_AGENT_CONFIG": str(config_path)}, clear=False):
                original = create_strategy("policy api")

                get_result = {}
                get_handler = AdminHandler.__new__(AdminHandler)
                get_handler.path = f"/api/strategies/{original['id']}"
                get_handler._send_json = lambda payload, status=None: get_result.update(
                    payload=payload,
                    status=status,
                )
                get_handler.do_GET()

                for section in ("exposure_policy", "margin_policy", "short_policy"):
                    self.assertIn(section, get_result["payload"]["config"])

                candidate = deepcopy(get_result["payload"]["config"])
                candidate["parameters"]["market"] = {"enabled": True, "value": "us"}
                candidate["exposure_policy"]["mode"] = "LONG_SHORT"
                put_result = {}
                put_handler = AdminHandler.__new__(AdminHandler)
                put_handler.path = f"/api/strategies/{original['id']}"
                put_handler._read_json = lambda: candidate
                put_handler._send_json = lambda payload, status=None: put_result.update(
                    payload=payload,
                    status=status,
                )
                put_handler.do_PUT()

                revised = put_result["payload"]["config"]
                persisted_original = load_strategy_config(strategy_id=original["id"])

        self.assertNotEqual(revised["id"], original["id"])
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["exposure_policy"]["mode"], "LONG_SHORT")
        self.assertEqual(revised["lifecycle"]["stage"], "draft")
        self.assertFalse(revised["validation"]["approval_gate"]["passed"])
        self.assertEqual(persisted_original["exposure_policy"]["mode"], "LONG_ONLY")

    def test_typed_projection_preserves_complete_nested_public_api_shape(self):
        runtime_fields = {item.name for item in fields(PerformanceRuntime)}
        self.assertTrue(
            {
                "last_successful_pipeline_at",
                "last_successful_pipeline_run_id",
                "last_pipeline_admitted",
                "last_pipeline_stages",
                "last_pipeline_market_regime",
                "last_pipeline_data_quality",
                "availability",
            }.issubset(runtime_fields)
        )
        self.assertEqual(
            {item.name for item in fields(PerformanceNavPoint)},
            {
                "at",
                "nav",
                "cash",
                "market_value",
                "cumulative_return_pct",
                "drawdown_pct",
                "risk_level",
                "trading_mode",
                "source",
            },
        )
        self.assertTrue(
            {
                "id",
                "key",
                "strategy_revision",
                "control_epoch",
                "side",
                "purpose",
                "symbol",
                "name",
                "slot_id",
                "quantity",
                "filled_quantity",
                "filled_notional",
                "commission_charged",
                "fees_charged",
                "status",
                "reason",
                "signal_price",
                "score",
                "reserved_cash",
                "valid_date",
                "valid_session_date",
                "created_at",
                "updated_at",
                "replacement_candidate",
                "cancel_reason",
                "position_side",
                "position_effect",
            }.issubset({item.name for item in fields(PerformanceOrder)})
        )
        self.assertEqual(
            {item.name for item in fields(PerformanceEventView)},
            {
                "id",
                "key",
                "type",
                "occurred_at",
                "message",
                "strategy_revision",
                "data",
            },
        )
        self.assertTrue(
            {
                "borrow_rate_pct",
                "borrow_rate_source",
                "borrow_rate_estimated",
            }.issubset({item.name for item in fields(PerformancePosition)})
        )

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
                long_market_value=118.0,
                short_liability=0.0,
                gross_exposure_pct=95.9349593495935,
                net_exposure_pct=95.9349593495935,
                margin_rate_pct=104.23728813559322,
                buying_power=5.0,
                margin_loan=0.0,
                financing_cost=0.0,
                borrow_cost=0.0,
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
            runtime=PerformanceRuntime(
                last_pipeline_market_regime={"state": "RISK_ON"},
            ),
            nav_history=(
                PerformanceNavPoint(
                    at=now,
                    nav=123.0,
                    cash=5.0,
                    market_value=118.0,
                    cumulative_return_pct=999.0,
                    drawdown_pct=None,
                    risk_level=None,
                    trading_mode=None,
                    source="test",
                ),
            ),
            positions=(),
            orders=(
                PerformanceOrder(
                    id="order-1",
                    side="BUY",
                    symbol="AAPL",
                    name="Apple",
                    quantity=1,
                    filled_quantity=0,
                    status="INTENDED",
                    reason="test",
                    created_at=now,
                    updated_at=now,
                    filled_notional=0.0,
                    commission_charged=0.0,
                    fees_charged=0.0,
                    strategy_revision=2,
                    position_side="LONG",
                    position_effect="OPEN",
                ),
            ),
            closed_trades=(),
            events=(
                PerformanceEventView(
                    id="event-1",
                    type="ACCOUNT_OPENED",
                    occurred_at=now,
                    message="opened",
                    strategy_revision=2,
                    key=None,
                    data={},
                ),
            ),
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
        contract_fixture = json.loads(
            (
                Path(__file__).parents[1]
                / "frontend/src/test-fixtures/admin-strategy-performance.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(payload, contract_fixture)
        self.assertEqual(payload["summary"]["nav"], 123.0)
        self.assertEqual(payload["summary"]["cumulative_return_pct"], 999.0)
        self.assertEqual(payload["summary"]["realized_pnl"], 7.0)
        self.assertEqual(
            payload["runtime"]["last_pipeline_market_regime"],
            {"state": "RISK_ON"},
        )
        self.assertEqual(
            set(payload["nav_history"][0]),
            {
                "at",
                "nav",
                "cash",
                "market_value",
                "cumulative_return_pct",
                "drawdown_pct",
                "risk_level",
                "trading_mode",
                "source",
            },
        )
        self.assertIsNone(payload["orders"][0]["key"])
        self.assertIsNone(payload["orders"][0]["signal_price"])
        self.assertIsNone(payload["orders"][0]["replacement_candidate"])
        self.assertEqual(
            set(payload["events"][0]),
            {
                "id",
                "key",
                "type",
                "occurred_at",
                "message",
                "strategy_revision",
                "data",
            },
        )
        self.assertEqual(payload["config"], {"initial_cash": 100.0})
        self.assertEqual(payload["allocation"], {"model": "equal_weight"})

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
                web_root = Path(__file__).parents[1] / "src/stock_recommender/web"
                page = (web_root / "performance.html").read_text(encoding="utf-8")

        self.assertEqual(payload["strategy"]["id"], strategy["id"])
        self.assertEqual(payload["summary"]["nav"], 123_456.0)
        self.assertEqual(payload["summary"]["cash"], 123_456.0)
        self.assertEqual(payload["summary"]["max_positions"], 10)
        self.assertIn("策略表现 · Stock Agent", page)
        module_path = re.search(r'src="(/assets/performance-[^"]+\.js)"', page)
        self.assertIsNotNone(module_path)
        module = (web_root / module_path.group(1).lstrip("/")).read_text(encoding="utf-8")
        self.assertIn("Strategy Portfolio Ledger", module)

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
        self.assertIsNone(payload["summary"]["realized_pnl"])
        self.assertIsNone(payload["summary"]["closed_trade_count"])
        self.assertEqual(payload["summary"]["win_rate_pct"], None)
        self.assertFalse(payload["history_availability"]["lifecycle"]["complete"])
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

    def test_long_short_portfolio_api_exposes_typed_account_and_position_metrics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "strategies.json"
            portfolio_path = Path(temp_dir) / "portfolios.json"
            environment = {
                "STOCK_AGENT_CONFIG": str(config_path),
                "STOCK_AGENT_PORTFOLIO_PATH": str(portfolio_path),
            }
            occurred_at = datetime(2026, 8, 3, 10, 0, tzinfo=ZoneInfo("America/New_York"))
            with patch.dict(os.environ, environment, clear=False):
                original = create_strategy("long short performance")
                candidate = deepcopy(original)
                candidate["parameters"]["market"] = {"enabled": True, "value": "us"}
                candidate["exposure_policy"]["mode"] = "LONG_SHORT"
                strategy = save_strategy_config(candidate, strategy_id=original["id"])
                initial_cash = strategy["portfolio"]["initial_cash"]
                JsonLedgerStore(portfolio_path).create_account(
                    AccountSnapshot(
                        id=f"account-{strategy['id']}",
                        strategy_id=strategy["id"],
                        strategy_revision=strategy["revision"],
                        occurred_at=occurred_at,
                        available_cash=initial_cash,
                        restricted_short_proceeds=1_000.0,
                        margin_loan=500.0,
                        accrued_financing_cost=12.5,
                        accrued_borrow_cost=4.5,
                        positions=(
                            PositionSnapshot(
                                symbol="MSFT",
                                side=PositionSide.LONG,
                                quantity=1,
                                average_cost=90.0,
                                current_price=90.0,
                                sellable_quantity=1,
                            ),
                            PositionSnapshot(
                                symbol="PLTR",
                                side=PositionSide.SHORT,
                                quantity=10,
                                average_cost=100.0,
                                current_price=90.0,
                                position_mode="COVER_ONLY",
                            ),
                        ),
                        snapshot_id="admin-long-short-snapshot",
                    )
                )
                with patch(
                    "stock_recommender.admin.get_market_adapter",
                    return_value=self._QuoteAdapter(price=90.0, percent=-2.0),
                ):
                    payload = build_strategy_performance(
                        strategy_id=strategy["id"],
                        path=portfolio_path,
                        now=occurred_at,
                    )

        summary = payload["summary"]
        self.assertEqual(summary["long_market_value"], 90.0)
        self.assertEqual(summary["short_liability"], 900.0)
        self.assertIn("gross_exposure_pct", summary)
        self.assertIn("net_exposure_pct", summary)
        self.assertIn("margin_rate_pct", summary)
        self.assertIn("buying_power", summary)
        self.assertEqual(summary["margin_loan"], 500.0)
        self.assertEqual(summary["financing_cost"], 12.5)
        self.assertEqual(summary["borrow_cost"], 4.5)
        held_by_symbol = {item["symbol"]: item for item in payload["positions"]}
        held = held_by_symbol["PLTR"]
        self.assertEqual(held["side"], "SHORT")
        self.assertAlmostEqual(held["return_pct"], 10.0)
        self.assertEqual(held["position_mode"], "COVER_ONLY")
        self.assertEqual(
            held["borrow_rate_pct"],
            strategy["short_policy"]["estimated_borrow_apr_pct"],
        )
        self.assertEqual(held["borrow_rate_source"], "strategy_estimate")
        self.assertTrue(held["borrow_rate_estimated"])
        self.assertGreater(held["margin_used"], 0.0)
        self.assertIsNotNone(held["exit_distance_pct"])
        long_held = held_by_symbol["MSFT"]
        self.assertIsNone(long_held["borrow_rate_pct"])
        self.assertEqual(long_held["borrow_rate_source"], "unavailable")
        self.assertFalse(long_held["borrow_rate_estimated"])

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
