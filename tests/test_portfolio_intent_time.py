from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from stock_recommender.markets import market_date
from stock_recommender.portfolio_engine.borrow import BorrowSnapshot
from stock_recommender.portfolio_engine.config import (
    default_exposure_policy,
    default_margin_policy,
    default_short_policy,
)

from stock_recommender.portfolio_engine.contracts import (
    AccountSnapshot,
    MarketSnapshot,
    OrderIntent,
    OrderSide,
    PlanRequest,
    PositionEffect,
    PositionSide,
    ProcessRequest,
    stable_execution_intent_id,
)


CREATED = datetime(2026, 8, 3, 14, 30, tzinfo=timezone.utc)


def intent_id(created_at: datetime) -> str:
    return stable_execution_intent_id(
        symbol="AAPL",
        position_side=PositionSide.LONG,
        order_side=OrderSide.BUY,
        position_effect=PositionEffect.OPEN,
        quantity=10,
        reason="REBALANCE",
        created_snapshot_id="market-created",
        created_market_at=created_at,
    )


def strategy() -> dict:
    return {
        "version": 6,
        "id": "strategy-us",
        "revision": 1,
        "market": "us",
        "exposure_policy": default_exposure_policy(),
        "margin_policy": default_margin_policy(),
        "short_policy": default_short_policy(),
    }


def account() -> AccountSnapshot:
    return AccountSnapshot(
        id="account-us",
        strategy_id="strategy-us",
        strategy_revision=1,
        occurred_at=CREATED,
        available_cash=1_000.0,
        snapshot_id="account-snapshot",
    )


def forged_naive_market() -> MarketSnapshot:
    value = object.__new__(MarketSnapshot)
    object.__setattr__(value, "id", "forged-market")
    object.__setattr__(value, "occurred_at", CREATED.replace(tzinfo=None))
    object.__setattr__(value, "quotes", {})
    return value


class IntentCreationTimeTests(unittest.TestCase):
    def test_created_market_time_has_no_implicit_default(self):
        with self.assertRaises(TypeError):
            stable_execution_intent_id(
                symbol="AAPL",
                position_side=PositionSide.LONG,
                order_side=OrderSide.BUY,
                position_effect=PositionEffect.OPEN,
                quantity=10,
                reason="REBALANCE",
                created_snapshot_id="market-created",
            )
        with self.assertRaises(TypeError):
            OrderIntent(
                id="invalid",
                symbol="AAPL",
                position_side=PositionSide.LONG,
                order_side=OrderSide.BUY,
                position_effect=PositionEffect.OPEN,
                quantity=10,
                reason="REBALANCE",
                created_snapshot_id="market-created",
            )

    def test_creation_time_is_aware_required_and_bound_to_stable_id(self):
        first = intent_id(CREATED)
        later = intent_id(CREATED.replace(minute=31))
        self.assertNotEqual(first, later)
        value = OrderIntent(
            id=first,
            symbol="AAPL",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            quantity=10,
            reason="REBALANCE",
            created_snapshot_id="market-created",
            created_market_at=CREATED,
        )
        self.assertEqual(value.created_market_at, CREATED)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            OrderIntent(
                id="invalid",
                symbol="AAPL",
                position_side=PositionSide.LONG,
                order_side=OrderSide.BUY,
                position_effect=PositionEffect.OPEN,
                quantity=10,
                reason="REBALANCE",
                created_snapshot_id="market-created",
                created_market_at=CREATED.replace(tzinfo=None),
            )

    def test_market_snapshot_contract_rejects_naive_time(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            MarketSnapshot(
                id="naive-market",
                occurred_at=CREATED.replace(tzinfo=None),
                quotes={},
            )

    def test_requests_revalidate_market_time_even_for_forged_snapshot(self):
        common = {
            "run_key": "run-naive",
            "strategy": strategy(),
            "account": account(),
            "market": forged_naive_market(),
            "borrow": BorrowSnapshot.unavailable(),
        }
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            ProcessRequest(**common)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            PlanRequest(
                **common,
                analyzed_rows=(),
                event_calendar={},
            )

    def test_us_market_date_is_stable_across_dst_transition(self):
        before_jump = datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)
        after_jump = datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)
        self.assertEqual(market_date(before_jump, "us"), date(2026, 3, 8))
        self.assertEqual(market_date(after_jump, "us"), date(2026, 3, 8))


if __name__ == "__main__":
    unittest.main()
