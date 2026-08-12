from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from stock_recommender.portfolio_engine.contracts import (
    ExecutionProgressFill,
    OrderExecutionProgress,
    OrderSide,
    PositionSide,
    stable_execution_progress_fill_id,
)
from stock_recommender.portfolio_engine.progress_state import (
    ProgressStateConflict,
    merge_latest_execution_progress,
)


NOW = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)


def progress_fill(*, minute: int, price: float, status: str) -> ExecutionProgressFill:
    facts = {
        "intent_id": "intent-1",
        "symbol": "NVDA",
        "position_side": PositionSide.LONG,
        "order_side": OrderSide.BUY,
        "snapshot_id": f"market-{minute}",
        "occurred_at": NOW + timedelta(minutes=minute),
        "quantity": 5,
        "price": price,
        "fees": 0.5,
        "commission": 0.5,
        "status": status,
    }
    return ExecutionProgressFill(
        id=stable_execution_progress_fill_id(**facts),
        **facts,
    )


def progress(*fills: ExecutionProgressFill) -> OrderExecutionProgress:
    return OrderExecutionProgress(
        intent_id="intent-1",
        symbol="NVDA",
        position_side=PositionSide.LONG,
        order_side=OrderSide.BUY,
        intent_quantity=10,
        execution_policy_fingerprint="policy-1",
        fills=fills,
    )


class ProgressStateTests(unittest.TestCase):
    def test_later_cumulative_snapshot_supersedes_migration_prefix(self):
        first = progress(progress_fill(minute=1, price=100.0, status="PARTIAL"))
        completed = progress(
            *first.fills,
            progress_fill(minute=2, price=101.0, status="FILLED"),
        )

        self.assertEqual(
            merge_latest_execution_progress((first,), (completed,)),
            (completed,),
        )
        self.assertEqual(
            merge_latest_execution_progress((completed,), (first,)),
            (completed,),
        )

    def test_divergent_history_fails_closed(self):
        first = progress(progress_fill(minute=1, price=100.0, status="PARTIAL"))
        divergent = progress(
            progress_fill(minute=1, price=99.0, status="PARTIAL")
        )

        with self.assertRaisesRegex(ProgressStateConflict, "not append-only"):
            merge_latest_execution_progress((first,), (divergent,))

    def test_identity_change_fails_closed(self):
        first = progress(progress_fill(minute=1, price=100.0, status="PARTIAL"))
        changed_policy = OrderExecutionProgress(
            intent_id=first.intent_id,
            symbol=first.symbol,
            position_side=first.position_side,
            order_side=first.order_side,
            intent_quantity=first.intent_quantity,
            execution_policy_fingerprint="different-policy",
            fills=first.fills,
        )

        with self.assertRaisesRegex(ProgressStateConflict, "identity conflict"):
            merge_latest_execution_progress((first,), (changed_policy,))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
