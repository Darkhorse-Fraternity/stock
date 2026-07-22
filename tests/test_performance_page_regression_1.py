import unittest
from pathlib import Path


class PerformancePageMobileRegressionTests(unittest.TestCase):
    # Regression: ISSUE-001 — current NAV forced horizontal overflow on 375px screens.
    # Found by /qa on 2026-07-22.
    # Report: .gstack/qa-reports/qa-report-localhost-2026-07-22.md
    def test_mobile_metrics_are_allowed_to_shrink_without_page_overflow(self):
        page = (Path(__file__).parents[1] / "frontend/public/performance.html").read_text(encoding="utf-8")

        self.assertIn(".metric{min-width:0", page)
        self.assertIn("main{width:100%;margin:0;overflow:hidden}", page)
        self.assertIn(".metric strong{font-size:clamp(19px,5.5vw,24px);white-space:nowrap}", page)


if __name__ == "__main__":
    unittest.main()
