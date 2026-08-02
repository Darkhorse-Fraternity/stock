import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class PerformancePageLongShortTests(unittest.TestCase):
    def setUp(self):
        self.page = (ROOT / "frontend/public/performance.html").read_text(
            encoding="utf-8"
        )
        self.api = (ROOT / "frontend/src/lib/api.ts").read_text(encoding="utf-8")
        self.app = (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
        self.css = (ROOT / "frontend/src/index.css").read_text(encoding="utf-8")

    def interface_fields(self, name):
        match = re.search(
            rf"export interface {name} \{{(?P<body>.*?)^\}}",
            self.api,
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match, f"missing TypeScript interface {name}")
        return {
            field.group(1): field.group(2) is not None
            for field in re.finditer(
                r"^  ([A-Za-z_][A-Za-z0-9_]*)(\?)?:", match.group("body"), re.MULTILINE
            )
        }

    def assert_interface_fields(self, name, expected):
        fields = self.interface_fields(name)
        self.assertEqual(set(fields), set(expected), name)
        self.assertFalse(
            any(fields.values()), f"{name} must not make backend fields optional"
        )

    def test_page_renders_direction_and_margin_fields(self):
        for field in (
            "long_market_value",
            "short_liability",
            "gross_exposure_pct",
            "net_exposure_pct",
            "margin_rate_pct",
            "buying_power",
            "financing_cost",
            "borrow_cost",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.page)
        self.assertIn("position.side", self.page)
        self.assertIn("borrow_rate_estimated", self.page)
        self.assertIn("borrow_rate_source", self.page)

    def test_page_labels_risk_events_and_escapes_event_content(self):
        for event_type in ("MARGIN_CALL", "COVER_ONLY", "RISK_CHANGED"):
            with self.subTest(event_type=event_type):
                self.assertIn(event_type, self.page)
        self.assertIn("esc(r.message)", self.page)
        self.assertIn("esc(r.type)", self.page)

    def test_frontend_types_match_backend_policy_contracts(self):
        expected = (
            "export type ExposureMode = \"LONG_ONLY\" | \"LONG_LEVERAGED\" | \"LONG_SHORT\"",
            "export interface ExposurePolicy",
            "export interface MarginPolicy",
            "export interface ShortPolicy",
            "exposure_policy: ExposurePolicy",
            "margin_policy: MarginPolicy",
            "short_policy: ShortPolicy",
            "export interface PortfolioPerformanceSummary",
            "export interface PortfolioPerformancePosition",
            "export interface PortfolioPerformanceEvent",
        )
        for declaration in expected:
            with self.subTest(declaration=declaration):
                self.assertIn(declaration, self.api)

    def test_performance_payload_types_cover_every_backend_field(self):
        self.assert_interface_fields(
            "PerformanceHistoryAvailability", {"complete", "source", "reason"}
        )
        self.assert_interface_fields(
            "PerformanceHistoryStatus", {"nav", "lifecycle"}
        )
        self.assert_interface_fields(
            "PortfolioPerformanceStrategy",
            {
                "id", "name", "revision", "stage", "market", "market_label",
                "currency", "currency_symbol", "initial_cash", "max_positions",
                "signal_model", "signal_time", "signal_data_cutoff",
                "allocation_model", "benchmark_symbol", "benchmark_name",
                "market_regime", "risk_level", "trading_mode",
                "target_exposure_pct", "exposure_policy", "margin_policy",
                "short_policy",
            },
        )
        self.assert_interface_fields(
            "PortfolioPerformanceRuntime",
            {
                "last_successful_pipeline_at", "last_successful_pipeline_run_id",
                "last_pipeline_admitted", "last_pipeline_stages",
                "last_pipeline_market_regime", "last_pipeline_data_quality",
                "availability",
            },
        )
        self.assert_interface_fields(
            "PortfolioPerformanceNavPoint",
            {
                "at", "nav", "cash", "market_value", "cumulative_return_pct",
                "drawdown_pct", "risk_level", "trading_mode", "source",
            },
        )
        self.assert_interface_fields(
            "PortfolioPerformanceSummary",
            {
                "initial_cash", "nav", "cash", "reserved_cash", "market_value",
                "long_market_value", "short_liability", "gross_exposure_pct",
                "net_exposure_pct", "margin_rate_pct", "buying_power",
                "margin_loan", "financing_cost", "borrow_cost",
                "cumulative_return_pct", "maximum_drawdown_pct", "realized_pnl",
                "unrealized_pnl", "position_count", "max_positions",
                "target_exposure_pct", "closed_trade_count", "win_rate_pct",
            },
        )
        self.assert_interface_fields(
            "PortfolioPerformancePosition",
            {
                "slot_id", "name", "symbol", "first_entry_price", "first_entry_at",
                "current_price", "day_change_pct", "return_pct", "unrealized_pnl",
                "weight_pct", "quantity", "sellable_quantity", "trailing_active",
                "signal_invalid_days", "exit_distance_pct", "market_value",
                "average_cost", "position_side", "side", "position_mode",
                "borrow_rate_pct", "borrow_rate_source", "borrow_rate_estimated",
                "margin_used",
            },
        )
        self.assert_interface_fields(
            "PortfolioPerformanceOrder",
            {
                "id", "side", "symbol", "name", "quantity", "filled_quantity",
                "status", "reason", "created_at", "updated_at", "filled_notional",
                "commission_charged", "fees_charged", "strategy_revision",
                "position_side", "position_effect", "key", "control_epoch",
                "purpose", "slot_id", "signal_price", "score", "reserved_cash",
                "valid_date", "valid_session_date", "cancel_reason",
                "replacement_candidate",
            },
        )
        self.assert_interface_fields(
            "PortfolioPerformanceClosedTrade",
            {
                "id", "name", "symbol", "entry_price", "exit_price", "quantity",
                "realized_pnl", "return_pct", "reason", "closed_at",
                "strategy_revision", "position_side",
            },
        )
        self.assert_interface_fields(
            "PortfolioPerformanceEvent",
            {"id", "type", "occurred_at", "message", "strategy_revision", "key", "data"},
        )
        self.assert_interface_fields(
            "StrategyPerformancePayload",
            {
                "generated_at", "quote_error", "strategy", "summary", "runtime",
                "nav_history", "positions", "orders", "closed_trades", "events",
                "history_availability", "market", "market_label", "currency",
                "currency_symbol", "config", "allocation",
            },
        )
        self.assertIn("key: string | null", self.api)
        self.assertNotIn("key?: string | null", self.api)
        for type_line in (
            'market: "cn" | "us"',
            "last_successful_pipeline_at: string | null",
            "last_pipeline_stages: PerformanceJsonObject[] | null",
            'status: "INTENDED" | "PARTIAL" | "FILLED" | "CANCELLED" | "EXPIRED"',
            'position_effect: "OPEN" | "INCREASE" | "REDUCE" | "CLOSE"',
            'purpose: "ENTRY" | "EXIT" | null',
            'position_side: "LONG" | "SHORT"',
            'borrow_rate_source: "strategy_estimate" | "unavailable"',
            "history_availability: PerformanceHistoryStatus",
            "config: PortfolioConfig",
            "allocation: AllocationConfig",
        ):
            with self.subTest(type_line=type_line):
                self.assertIn(type_line, self.api)
        performance_contract = self.api.split(
            "export interface PerformanceHistoryAvailability", 1
        )[1].split("export interface SignalConfig", 1)[0]
        self.assertNotIn("any", performance_contract)
        self.assertNotIn("Record<string, unknown>", performance_contract)

    def test_policy_editor_guards_non_us_and_confirms_real_transition(self):
        self.assertIn("多空与杠杆", self.app)
        self.assertIn("market !== \"us\"", self.app)
        self.assertIn("mode !== \"LONG_SHORT\"", self.app)
        self.assertIn("initialMode !== \"LONG_ONLY\"", self.app)
        self.assertIn("safeConfig.exposure_policy.mode === \"LONG_ONLY\"", self.app)
        self.assertIn("window.confirm", self.app)
        self.assertIn("if (!confirmPolicyTransition()) return", self.app)
        self.assertIn("回测与模拟盘审批", self.app)

    def test_mobile_and_accessibility_guards_are_present(self):
        self.assertIn("@media(max-width:760px)", self.page)
        self.assertIn("overflow-x:hidden", self.page)
        self.assertIn(":focus-visible", self.page)
        self.assertIn("prefers-reduced-motion", self.page)
        self.assertIn("prefers-reduced-motion", self.css)
        self.assertIn("overflow-x: hidden", self.css)
        self.assertIn(":focus-visible", self.css)
        self.assertIn('role="tablist"', self.page)
        self.assertIn('role="tab"', self.page)
        self.assertIn('aria-selected="true"', self.page)
        self.assertIn('aria-controls="positions"', self.page)
        self.assertIn('role="tabpanel"', self.page)
        self.assertIn("setAttribute('aria-selected'", self.page)
        self.assertIn(".risk-cell strong{white-space:normal;overflow-wrap:anywhere}", self.page)
        self.assertIn(".table-wrap{max-width:100%;overflow-x:auto", self.page)

    def test_built_assets_have_no_trailing_whitespace(self):
        for path in (ROOT / "src/stock_recommender/web/assets").glob("*"):
            if path.suffix not in {".js", ".css"}:
                continue
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                with self.subTest(path=path.name, line=line_number):
                    self.assertEqual(line, line.rstrip(" \t"))

    def test_inline_javascript_is_valid_and_build_copies_the_page(self):
        script = self.page.split("<script>", 1)[1].split("</script>", 1)[0]
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as handle:
            handle.write(script)
            handle.flush()
            result = subprocess.run(
                ["node", "--check", handle.name],
                capture_output=True,
                check=False,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        built = (ROOT / "src/stock_recommender/web/performance.html").read_text(
            encoding="utf-8"
        )
        self.assertEqual(built, self.page)


if __name__ == "__main__":
    unittest.main()
