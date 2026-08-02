import ast
import importlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def test_portfolio_renderers_live_in_reports_only(self):
        reports = importlib.import_module("stock_recommender.reports")
        runtime = importlib.import_module("stock_recommender.portfolio_runtime")
        for name in ("format_portfolio_snapshot", "format_portfolio_actions"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(reports, name, None)))
                self.assertFalse(hasattr(runtime, name))

    def test_no_source_or_test_imports_legacy_portfolio_modules(self):
        forbidden = {
            "stock_recommender.portfolio",
            "stock_recommender.portfolio_pipeline",
        }
        violations = []
        for tree in (ROOT / "src", ROOT / "tests"):
            for source in tree.rglob("*.py"):
                parsed = ast.parse(source.read_text(encoding="utf-8"))
                for node in ast.walk(parsed):
                    if isinstance(node, ast.Import):
                        names = {item.name for item in node.names}
                    elif isinstance(node, ast.ImportFrom):
                        module = node.module or ""
                        names = {module}
                        if node.level and module in {
                            "portfolio",
                            "portfolio_pipeline",
                        }:
                            names.add(f"stock_recommender.{module}")
                        if module == "stock_recommender":
                            names.update(
                                f"stock_recommender.{item.name}"
                                for item in node.names
                            )
                    else:
                        continue
                    matched = forbidden & names
                    if matched:
                        violations.append(
                            f"{source.relative_to(ROOT)}:{node.lineno}:"
                            f"{','.join(sorted(matched))}"
                        )
        self.assertEqual(violations, [])

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
            "stock_recommender.us_data_providers",
        ):
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.import_module(module))

    def test_compatibility_facade_is_removed(self):
        self.assertFalse((ROOT / "src" / "stock_agent.py").exists())
        self.assertIsNone(importlib.util.find_spec("stock_agent"))


if __name__ == "__main__":
    unittest.main()
