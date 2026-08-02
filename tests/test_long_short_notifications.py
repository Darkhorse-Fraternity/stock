import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from stock_recommender.portfolio_engine.contracts import (
    AccountSnapshot,
    DecisionBatch,
    ExecutionFill,
    OrderIntent,
    OrderSide,
    PortfolioEvent,
    PortfolioSnapshot,
    PositionEffect,
    PositionSide,
    PositionSnapshot,
)
from stock_recommender.portfolio_engine.valuation import value_account
from stock_recommender.reports import (
    append_performance_link,
    format_portfolio_actions,
    format_portfolio_snapshot,
)


NOW = datetime(2026, 8, 3, 10, 0, tzinfo=ZoneInfo("America/New_York"))


def long_short_strategy():
    return {
        "id": "long-short-test",
        "name": "多空测试",
        "revision": 2,
        "exposure_policy": {"max_positions": 10},
    }


def long_short_snapshot():
    positions = (
        PositionSnapshot(
            symbol="MSFT",
            side=PositionSide.LONG,
            quantity=9,
            average_cost=95.0,
            current_price=100.0,
            sellable_quantity=9,
        ),
        PositionSnapshot(
            symbol="PLTR",
            side=PositionSide.SHORT,
            quantity=3,
            average_cost=105.0,
            current_price=100.0,
        ),
    )
    account = AccountSnapshot(
        id="account-long-short-test",
        strategy_id="long-short-test",
        strategy_revision=2,
        occurred_at=NOW,
        available_cash=100.0,
        restricted_short_proceeds=300.0,
        margin_loan=100.0,
        accrued_financing_cost=12.5,
        accrued_borrow_cost=4.5,
        positions=positions,
        snapshot_id="portfolio-long-short-test",
    )
    valuation = value_account(account, {"MSFT": 100.0, "PLTR": 100.0})
    return PortfolioSnapshot(
        account=account,
        metrics=valuation.metrics,
        positions=valuation.positions,
        open_intents=(),
        recent_events=(),
    )


def cover_batch():
    intent = OrderIntent(
        id="cover-pltr",
        symbol="PLTR",
        position_side=PositionSide.SHORT,
        order_side=OrderSide.BUY,
        position_effect=PositionEffect.CLOSE,
        quantity=1,
        reason="MARGIN_CALL",
        created_snapshot_id="market-long-short-test",
        created_market_at=NOW,
    )
    return DecisionBatch(
        run_key="risk-long-short-test",
        strategy_id="long-short-test",
        strategy_revision=2,
        portfolio_snapshot_id="portfolio-long-short-test",
        market_snapshot_id="market-long-short-test",
        intents=(intent,),
        events=(
            PortfolioEvent(
                id="margin-call-long-short-test",
                type="MARGIN_CALL",
                occurred_at=NOW,
                data={"reason": "MAINTENANCE_MARGIN_BREACH"},
            ),
        ),
        fills=(
            ExecutionFill(
                intent_id="cover-pltr",
                symbol="PLTR",
                quantity=1,
                price=100.0,
                fees=0.1,
                status="FILLED",
            ),
        ),
    )


class LongShortNotificationTests(unittest.TestCase):
    def test_action_message_contains_direction_exposure_margin_costs_and_link(self):
        message = format_portfolio_actions(
            long_short_strategy(),
            cover_batch(),
            snapshot=long_short_snapshot(),
            performance_url="https://stock.example/strategies/long-short-test/portfolio",
        )

        self.assertIn("策略：多空测试 · v2", message)
        self.assertIn("空头回补", message)
        self.assertIn("模拟成交：PLTR 空头回补", message)
        self.assertIn("保证金追缴", message)
        self.assertIn("维持保证金不足", message)
        self.assertIn("总敞口 133.33%", message)
        self.assertIn("净敞口 66.67%", message)
        self.assertIn("保证金率 75.00%", message)
        self.assertIn("融资成本 12.50", message)
        self.assertIn("借券成本 4.50", message)
        self.assertIn("https://stock.example/strategies/long-short-test/portfolio", message)

    def test_hourly_message_contains_direction_account_metrics_costs_and_link(self):
        message = format_portfolio_snapshot(
            long_short_strategy(),
            long_short_snapshot(),
            performance_url="https://stock.example/strategies/long-short-test/portfolio",
        )

        self.assertIn("策略：多空测试 · v2", message)
        self.assertIn("MSFT 多头", message)
        self.assertIn("PLTR 空头", message)
        self.assertIn("总敞口 133.33%", message)
        self.assertIn("净敞口 66.67%", message)
        self.assertIn("保证金率 75.00%", message)
        self.assertIn("融资成本 12.50", message)
        self.assertIn("借券成本 4.50", message)
        self.assertIn("https://stock.example/strategies/long-short-test/portfolio", message)

    def test_five_minute_risk_message_is_silent_without_actions_or_events(self):
        empty = DecisionBatch(
            run_key="risk-empty-long-short-test",
            strategy_id="long-short-test",
            strategy_revision=2,
            portfolio_snapshot_id="portfolio-long-short-test",
            market_snapshot_id="market-empty-long-short-test",
        )

        self.assertEqual(
            format_portfolio_actions(
                long_short_strategy(),
                empty,
                snapshot=long_short_snapshot(),
                performance_url="https://stock.example/strategies/long-short-test/portfolio",
            ),
            "",
        )

    def test_performance_link_rejects_markdown_injection_and_credentials(self):
        malicious = "https://token@stock.example/portfolio)\nINTERNAL_SECRET"

        self.assertEqual(append_performance_link("report", malicious), "report")
        message = format_portfolio_snapshot(
            long_short_strategy(),
            long_short_snapshot(),
            performance_url=malicious,
        )
        self.assertNotIn("token", message)
        self.assertNotIn("INTERNAL_SECRET", message)


if __name__ == "__main__":
    unittest.main()
