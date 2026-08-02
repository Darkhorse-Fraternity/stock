import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).parents[1]


class _ModuleScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        attributes = dict(attrs)
        if attributes.get("type") == "module" and attributes.get("src"):
            self.sources.append(attributes["src"])


def module_scripts(path):
    parser = _ModuleScripts()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser.sources


class PerformancePageLongShortTests(unittest.TestCase):
    def test_performance_is_a_vite_module_entry_without_public_shadow_copy(self):
        source_page = ROOT / "frontend/performance.html"

        self.assertEqual(module_scripts(source_page), ["/src/performance-entry.ts"])
        self.assertFalse((ROOT / "frontend/public/performance.html").exists())

    def test_built_page_references_existing_hashed_module(self):
        built_page = ROOT / "src/stock_recommender/web/performance.html"
        sources = module_scripts(built_page)

        self.assertEqual(len(sources), 1)
        self.assertRegex(sources[0], r"^/assets/performance-[A-Za-z0-9_-]+\.js$")
        self.assertTrue((ROOT / "src/stock_recommender/web" / sources[0].lstrip("/")).is_file())

    def test_generated_javascript_and_css_have_no_trailing_whitespace(self):
        assets = ROOT / "src/stock_recommender/web/assets"
        checked = 0
        for path in assets.iterdir():
            if path.suffix not in {".js", ".css"}:
                continue
            checked += 1
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                with self.subTest(path=path.name, line=line_number):
                    self.assertEqual(line, line.rstrip(" \t"))
        self.assertGreaterEqual(checked, 2)

    def test_horizontal_overflow_is_owned_by_tables_not_hidden_globally(self):
        page = (ROOT / "frontend/performance.html").read_text(encoding="utf-8")
        app_css = (ROOT / "frontend/src/index.css").read_text(encoding="utf-8")

        self.assertNotIn("overflow-x: hidden", page)
        self.assertNotIn("overflow-x: hidden", app_css)
        self.assertIn(".table-wrap { max-width: 100%; overflow-x: auto", page)
        self.assertIn("overflow-wrap: anywhere", page)


if __name__ == "__main__":
    unittest.main()
