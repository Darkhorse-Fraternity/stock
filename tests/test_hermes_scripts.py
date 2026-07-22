import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HermesScriptRegressionTests(unittest.TestCase):
    def test_daily_llm_timeout_stays_below_hermes_script_limit(self) -> None:
        script = (ROOT / "scripts" / "hermes-ai-run.sh").read_text(encoding="utf-8")
        match = re.search(r"STOCK_AGENT_LLM_TIMEOUT:-(?P<seconds>\d+)", script)

        self.assertIsNotNone(match)
        self.assertLess(int(match.group("seconds")), 120)
        self.assertIn('STOCK_AGENT_ENABLE_TICK="${STOCK_AGENT_ENABLE_TICK:-0}"', script)


if __name__ == "__main__":
    unittest.main()
