import unittest
from pathlib import Path


class PerformancePageMobileRegressionTests(unittest.TestCase):
    # Regression: ISSUE-001 — current NAV forced horizontal overflow on 375px screens.
    # Found by /qa on 2026-07-22.
    # Report: .gstack/qa-reports/qa-report-localhost-2026-07-22.md
    def test_mobile_metrics_shrink_and_long_content_wraps_at_390px(self):
        page = (Path(__file__).parents[1] / "frontend/performance.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("@media (max-width: 760px)", page)
        self.assertIn(".metric { min-width: 0", page)
        self.assertIn(".risk-cell strong { white-space: normal; overflow-wrap: anywhere; }", page)
        self.assertIn("h1 {", page)
        self.assertIn("overflow-wrap: anywhere", page)
        self.assertNotIn("overflow-x: hidden", page)


if __name__ == "__main__":
    unittest.main()
