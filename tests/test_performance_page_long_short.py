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
