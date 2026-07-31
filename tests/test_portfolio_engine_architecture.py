import importlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PortfolioEngineArchitectureTests(unittest.TestCase):
    def test_domain_modules_import_without_portfolio_facade(self):
        modules = (
            "contracts",
            "config",
            "ports",
            "signal_ports",
            "short_signal",
            "target_pipeline",
            "exposure",
            "borrow",
            "margin",
            "risk",
            "execution",
            "valuation",
            "ledger",
            "service",
        )
        for name in modules:
            with self.subTest(name=name):
                self.assertIsNotNone(
                    importlib.import_module(
                        f"stock_recommender.portfolio_engine.{name}"
                    )
                )

    def test_engine_modules_do_not_import_rendering_or_http_layers(self):
        forbidden = {"reports", "admin", "delivery", "tracking"}
        root = ROOT / "src" / "stock_recommender" / "portfolio_engine"
        for source in root.glob("*.py"):
            text = source.read_text(encoding="utf-8")
            for module in forbidden:
                self.assertNotIn(f"stock_recommender.{module}", text)
                self.assertNotIn(f"from ..{module}", text)


if __name__ == "__main__":
    unittest.main()
