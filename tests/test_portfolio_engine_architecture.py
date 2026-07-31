import ast
import importlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _find_forbidden_engine_imports(
    source: str, forbidden: set[str]
) -> set[str]:
    violations: set[str] = set()

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for imported in node.names:
                parts = imported.name.split(".")
                if (
                    len(parts) > 1
                    and parts[0] == "stock_recommender"
                    and parts[1] in forbidden
                ):
                    violations.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            module_parts = (node.module or "").split(".")
            if node.level:
                if module_parts[0] in forbidden:
                    violations.add(module_parts[0])
                elif not node.module:
                    violations.update(
                        imported.name
                        for imported in node.names
                        if imported.name in forbidden
                    )
            elif module_parts[0] == "stock_recommender":
                if len(module_parts) > 1 and module_parts[1] in forbidden:
                    violations.add(module_parts[1])
                elif len(module_parts) == 1:
                    violations.update(
                        imported.name
                        for imported in node.names
                        if imported.name in forbidden
                    )

    return violations


class PortfolioEngineArchitectureTests(unittest.TestCase):
    def test_import_guard_detects_forbidden_import_syntaxes_and_aliases(self):
        forbidden = {"reports", "admin", "delivery", "tracking"}
        templates = (
            "import stock_recommender.{module}",
            "import stock_recommender.{module} as layer",
            "from stock_recommender.{module} import x",
            "from stock_recommender.{module} import x as alias",
            "from stock_recommender import {module}",
            "from stock_recommender import {module} as alias",
            "from ..{module} import x",
            "from ..{module} import x as alias",
            "from .. import {module}",
            "from .. import {module} as alias",
        )

        for module in forbidden:
            for template in templates:
                source = template.format(module=module)
                with self.subTest(module=module, source=source):
                    self.assertEqual(
                        {module}, _find_forbidden_engine_imports(source, forbidden)
                    )

    def test_import_guard_ignores_comments_strings_and_docstrings(self):
        forbidden = {"reports", "admin", "delivery", "tracking"}
        source = '''
"""Mentions stock_recommender.reports and from .. import admin."""

# import stock_recommender.delivery
message = "from stock_recommender import tracking"
'''

        self.assertEqual(
            set(), _find_forbidden_engine_imports(source, forbidden)
        )

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
            violations = _find_forbidden_engine_imports(text, forbidden)
            self.assertFalse(
                violations,
                f"{source}: forbidden imports: {', '.join(sorted(violations))}",
            )


if __name__ == "__main__":
    unittest.main()
