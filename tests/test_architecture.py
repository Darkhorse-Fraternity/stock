import importlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def test_domain_modules_are_directly_importable(self):
        for module in (
            "stock_recommender.cli",
            "stock_recommender.context",
            "stock_recommender.data_sources",
            "stock_recommender.llm",
            "stock_recommender.market_adapters",
            "stock_recommender.markets",
            "stock_recommender.reports",
            "stock_recommender.schedule",
            "stock_recommender.selection",
            "stock_recommender.tracking",
            "stock_recommender.universe",
        ):
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.import_module(module))

    def test_compatibility_facade_is_removed(self):
        self.assertFalse((ROOT / "src" / "stock_agent.py").exists())
        self.assertIsNone(importlib.util.find_spec("stock_agent"))


if __name__ == "__main__":
    unittest.main()
