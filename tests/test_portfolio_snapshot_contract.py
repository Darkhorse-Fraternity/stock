from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone

from stock_recommender.portfolio_engine.contracts import (
    AccountSnapshot,
    PortfolioSnapshot,
    PositionSide,
    PositionSnapshot,
)
from stock_recommender.portfolio_engine.valuation import value_account


NOW = datetime(2026, 8, 3, 14, 30, tzinfo=timezone.utc)


def valued_account(*, cash: float = 1_000.0) -> tuple[AccountSnapshot, object]:
    position = PositionSnapshot(
        symbol="AAPL",
        side=PositionSide.LONG,
        quantity=10,
        average_cost=90.0,
        current_price=100.0,
    )
    account = AccountSnapshot(
        id="account-us",
        strategy_id="strategy-us",
        strategy_revision=1,
        occurred_at=NOW,
        available_cash=cash,
        positions=(position,),
        snapshot_id="account-snapshot-1",
    )
    return account, value_account(account, {"AAPL": 100.0})


class PortfolioSnapshotContractTests(unittest.TestCase):
    def test_positions_must_fully_equal_account_positions(self):
        account, valuation = valued_account()
        forged_position = replace(valuation.positions[0], quantity=11)

        with self.assertRaisesRegex(ValueError, "fully match"):
            PortfolioSnapshot(
                account=account,
                metrics=valuation.metrics,
                positions=(forged_position,),
                open_intents=(),
                recent_events=(),
            )

    def test_metrics_must_be_derived_from_same_account_and_positions(self):
        account, valuation = valued_account()
        other_account, other_valuation = valued_account(cash=2_000.0)
        self.assertNotEqual(account.available_cash, other_account.available_cash)

        with self.assertRaisesRegex(ValueError, "same account"):
            PortfolioSnapshot(
                account=account,
                metrics=other_valuation.metrics,
                positions=valuation.positions,
                open_intents=(),
                recent_events=(),
            )


if __name__ == "__main__":
    unittest.main()
