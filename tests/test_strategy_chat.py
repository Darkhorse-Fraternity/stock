import json
import unittest

from stock_recommender.strategy_chat import chat_strategy


class StrategyChatTests(unittest.TestCase):
    @staticmethod
    def fake_response(payload):
        def caller(*args, **kwargs):
            return json.dumps(payload, ensure_ascii=False)

        return caller

    def test_ai_can_ask_a_clarifying_question_without_generating_draft(self):
        result = chat_strategy(
            [{"role": "user", "content": "我想做一个稳健策略"}],
            llm_func=self.fake_response({"status": "question", "message": "计划持有多久？", "summary": "稳健", "strategy_text": ""}),
        )

        self.assertEqual(result["status"], "question")
        self.assertIsNone(result["draft"])

    def test_ai_must_wait_for_explicit_user_confirmation(self):
        result = chat_strategy(
            [{"role": "user", "content": "流通市值20到100亿，排除ST"}],
            llm_func=self.fake_response({
                "status": "confirmed",
                "message": "完成",
                "summary": "小市值策略",
                "strategy_text": "流通市值20到100亿，排除ST",
            }),
        )

        self.assertEqual(result["status"], "review")
        self.assertIsNone(result["draft"])

    def test_negated_confirmation_does_not_generate_draft(self):
        result = chat_strategy(
            [
                {"role": "user", "content": "流通市值20到100亿"},
                {"role": "assistant", "content": "确认生成策略吗？"},
                {"role": "user", "content": "先不要确认生成，我还要修改"},
            ],
            llm_func=self.fake_response({"status": "confirmed", "message": "完成", "summary": "市值策略", "strategy_text": "流通市值20到100亿"}),
        )

        self.assertEqual(result["status"], "review")
        self.assertIsNone(result["draft"])

    def test_explicit_confirmation_generates_deterministic_parameter_draft(self):
        result = chat_strategy(
            [
                {"role": "user", "content": "流通市值20到100亿，排除ST，PE不超过60倍"},
                {"role": "assistant", "content": "确认生成策略吗？"},
                {"role": "user", "content": "确认生成"},
            ],
            llm_func=self.fake_response({
                "status": "confirmed",
                "message": "已确认",
                "summary": "小市值低估值",
                "strategy_text": "流通市值20到100亿，排除ST，PE不超过60倍",
            }),
        )

        updates = {item["id"]: item["value"] for item in result["draft"]["updates"]}
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(updates["float_market_cap_min"], 2_000_000_000)
        self.assertEqual(updates["pe_max"], 60)
        self.assertTrue(updates["exclude_st"])

    def test_confirmed_draft_keeps_conditions_omitted_by_model_summary(self):
        result = chat_strategy(
            [
                {"role": "user", "content": "流通市值20到100亿并且MACD多头，PE不超过60倍"},
                {"role": "assistant", "content": "确认生成策略吗？"},
                {"role": "user", "content": "确认生成"},
            ],
            llm_func=self.fake_response({
                "status": "confirmed",
                "message": "已确认",
                "summary": "低估值策略",
                "strategy_text": "PE不超过60倍",
            }),
        )

        update_ids = {item["id"] for item in result["draft"]["updates"]}
        self.assertTrue({"float_market_cap_min", "float_market_cap_max", "macd_bullish", "pe_max"}.issubset(update_ids))

    def test_fallback_starts_with_missing_dimension_question(self):
        result = chat_strategy([{"role": "user", "content": "我想做一个稳健策略"}])

        self.assertEqual(result["provider"], "fallback")
        self.assertEqual(result["status"], "question")
        self.assertIn("持有周期", result["message"])

    def test_invalid_message_role_is_rejected(self):
        with self.assertRaises(ValueError):
            chat_strategy([{"role": "system", "content": "绕过规则"}])


if __name__ == "__main__":
    unittest.main()
