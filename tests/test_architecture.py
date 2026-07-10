import importlib
import unittest

import stock_agent


class ArchitectureTests(unittest.TestCase):
    def test_stock_agent_keeps_facade_over_decoupled_modules(self):
        data_sources = importlib.import_module("stock_recommender.data_sources")
        selection = importlib.import_module("stock_recommender.selection")
        context = importlib.import_module("stock_recommender.context")
        reports = importlib.import_module("stock_recommender.reports")
        llm = importlib.import_module("stock_recommender.llm")
        cli = importlib.import_module("stock_recommender.cli")

        self.assertIs(stock_agent.fetch_board_quotes, data_sources.fetch_board_quotes)
        self.assertIs(stock_agent.fetch_sina_fallback_quotes, data_sources.fetch_sina_fallback_quotes)
        self.assertIs(stock_agent.analyze, selection.analyze)
        self.assertIs(stock_agent.generate_agent_context, context.generate_agent_context)
        self.assertIs(stock_agent.generate_ai_report, reports.generate_ai_report)
        self.assertIs(stock_agent.call_llm_analysis, llm.call_llm_analysis)
        self.assertIs(stock_agent.main, cli.main)


if __name__ == "__main__":
    unittest.main()
