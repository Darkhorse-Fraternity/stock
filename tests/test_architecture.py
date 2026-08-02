import ast
import importlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PORTFOLIO_MODULES = {
    "stock_recommender.portfolio",
    "stock_recommender.portfolio_pipeline",
}


def _import_targets(parsed: ast.AST) -> set[tuple[int, str]]:
    targets: set[tuple[int, str]] = set()
    for node in ast.walk(parsed):
        names: set[str] = set()
        if isinstance(node, ast.Import):
            names = {item.name for item in node.names}
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = {module}
            if node.level and module in {"portfolio", "portfolio_pipeline"}:
                names.add(f"stock_recommender.{module}")
            if module == "stock_recommender":
                names.update(f"stock_recommender.{item.name}" for item in node.names)
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            is_dynamic_import = (
                isinstance(function, ast.Name)
                and function.id in {"__import__", "import_module"}
            ) or (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "importlib"
                and function.attr == "import_module"
            )
            first = node.args[0]
            if is_dynamic_import and isinstance(first, ast.Constant) and isinstance(first.value, str):
                target = first.value
                if target in {".portfolio", ".portfolio_pipeline"} and len(node.args) > 1:
                    package = node.args[1]
                    if isinstance(package, ast.Constant) and package.value == "stock_recommender":
                        target = "stock_recommender" + target
                names = {target}
        for matched in FORBIDDEN_PORTFOLIO_MODULES & names:
            targets.add((node.lineno, matched))
    return targets


class ArchitectureTests(unittest.TestCase):
    def test_portfolio_renderers_live_in_reports_only(self):
        reports = importlib.import_module("stock_recommender.reports")
        runtime = importlib.import_module("stock_recommender.portfolio_runtime")
        for name in ("format_portfolio_snapshot", "format_portfolio_actions"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(reports, name, None)))
                self.assertFalse(hasattr(runtime, name))

    def test_no_source_or_test_imports_legacy_portfolio_modules(self):
        violations = []
        for tree in (ROOT / "src", ROOT / "tests"):
            for source in tree.rglob("*.py"):
                parsed = ast.parse(source.read_text(encoding="utf-8"))
                for line, matched in sorted(_import_targets(parsed)):
                    violations.append(f"{source.relative_to(ROOT)}:{line}:{matched}")
        self.assertEqual(violations, [])

    def test_legacy_import_guard_detects_dynamic_imports(self):
        source = """
import importlib
from importlib import import_module
importlib.import_module("stock_recommender.portfolio")
import_module("stock_recommender.portfolio_pipeline")
__import__("stock_recommender.portfolio")
importlib.import_module(".portfolio_pipeline", "stock_recommender")
"""
        self.assertEqual(
            {target for _, target in _import_targets(ast.parse(source))},
            FORBIDDEN_PORTFOLIO_MODULES,
        )

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
