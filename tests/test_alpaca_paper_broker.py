from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from stock_recommender.alpaca_paper_broker import (
    AlpacaPaperBroker,
    alpaca_client_order_id,
    execution_backend_for_strategy,
)
from stock_recommender.portfolio_engine.contracts import (
    AccountSnapshot,
    MarketSnapshot,
    OrderIntent,
    OrderSide,
    PositionEffect,
    PositionSide,
    TargetPosition,
)
from stock_recommender.portfolio_engine.execution import (
    ExecutionPolicy,
    execute_intents,
    intent_for_delta,
)
from stock_recommender.portfolio_engine.ports import BrokerOrderSnapshot


NOW = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)


def intent() -> OrderIntent:
    return OrderIntent(
        id="test-intent",
        symbol="AAPL",
        position_side=PositionSide.LONG,
        order_side=OrderSide.BUY,
        position_effect=PositionEffect.OPEN,
        quantity=10,
        reason="TEST",
        created_snapshot_id="market-old",
        created_market_at=NOW - timedelta(minutes=5),
    )


def order_for(value: OrderIntent, **updates: object) -> SimpleNamespace:
    values = {
        "id": "alpaca-order-1",
        "client_order_id": alpaca_client_order_id("strategy-1", value.id),
        "symbol": value.symbol,
        "side": "buy",
        "qty": str(value.quantity),
        "filled_qty": "0",
        "filled_avg_price": None,
        "status": "accepted",
    }
    values.update(updates)
    return SimpleNamespace(**values)


class FakeClient:
    def __init__(self, orders=()):
        self.orders = list(orders)
        self.get_calls = 0
        self.submit_calls = []
        self.submit_result = None
        self.raise_after_submit = False
        self.account = SimpleNamespace(status="ACTIVE", cash="100000")
        self.positions = []

    def get_orders(self, *, filter):
        self.get_calls += 1
        return list(self.orders)

    def submit_order(self, *, order_data):
        self.submit_calls.append(order_data)
        result = self.submit_result
        if result is None:
            raise AssertionError("submit_result must be configured")
        self.orders.append(result)
        if self.raise_after_submit:
            raise TimeoutError("response was lost")
        return result

    def get_account(self):
        return self.account

    def get_all_positions(self):
        return list(self.positions)


def broker(client: FakeClient) -> AlpacaPaperBroker:
    return AlpacaPaperBroker(
        "strategy-1",
        client,
        order_request_factory=lambda **values: values,
        all_orders_request_factory=lambda: {"status": "all", "limit": 500},
    )


class AlpacaPaperBrokerTests(unittest.TestCase):
    def test_client_order_id_is_stable_and_bounded(self):
        first = alpaca_client_order_id("strategy-1", "x" * 500)
        self.assertEqual(first, alpaca_client_order_id("strategy-1", "x" * 500))
        self.assertLessEqual(len(first), 48)
        self.assertNotEqual(first, alpaca_client_order_id("strategy-1", "y" * 500))
        self.assertNotEqual(first, alpaca_client_order_id("strategy-2", "x" * 500))

    def test_existing_order_is_reconciled_without_duplicate_submission(self):
        value = intent()
        client = FakeClient([order_for(value)])
        adapter = broker(client)

        first = adapter.place_or_get(value)
        second = adapter.place_or_get(value)

        self.assertEqual(first.order_id, "alpaca-order-1")
        self.assertEqual(second, first)
        self.assertEqual(client.get_calls, 1)
        self.assertEqual(client.submit_calls, [])

    def test_new_order_uses_market_request_and_is_cached(self):
        value = intent()
        client = FakeClient()
        client.submit_result = order_for(value)
        adapter = broker(client)

        adapter.place_or_get(value)
        adapter.place_or_get(value)

        self.assertEqual(client.get_calls, 1)
        self.assertEqual(
            client.submit_calls,
            [
                {
                    "symbol": "AAPL",
                    "qty": 10,
                    "side": "buy",
                    "client_order_id": alpaca_client_order_id("strategy-1", value.id),
                }
            ],
        )

    def test_lost_submit_response_is_resolved_by_client_order_id(self):
        value = intent()
        client = FakeClient()
        client.submit_result = order_for(value)
        client.raise_after_submit = True

        snapshot = broker(client).place_or_get(value)

        self.assertEqual(snapshot.order_id, "alpaca-order-1")
        self.assertEqual(client.get_calls, 2)
        self.assertEqual(len(client.submit_calls), 1)

    def test_vendor_order_identity_mismatch_fails_closed(self):
        value = intent()
        client = FakeClient([order_for(value, symbol="MSFT")])
        with self.assertRaisesRegex(RuntimeError, "symbol"):
            # The adapter validates its client ID; the execution core validates symbol.
            snapshot = broker(client).place_or_get(value)
            self.assertNotEqual(snapshot.symbol, value.symbol)

    def test_fresh_paper_account_requires_matching_local_cash_and_positions(self):
        client = FakeClient()
        adapter = broker(client)
        local = AccountSnapshot(
            id="account-1",
            strategy_id="strategy-1",
            strategy_revision=1,
            occurred_at=NOW,
            available_cash=1_000_000.0,
        )
        with self.assertRaisesRegex(RuntimeError, "cash does not match"):
            adapter.assert_ready(local)

    def test_fresh_matching_paper_account_is_accepted(self):
        client = FakeClient()
        adapter = broker(client)
        local = AccountSnapshot(
            id="account-1",
            strategy_id="strategy-1",
            strategy_revision=1,
            occurred_at=NOW,
            available_cash=100_000.0,
        )
        adapter.assert_ready(local)
        adapter.assert_ready(local)

    def test_execution_backend_selection_is_explicit_and_strategy_scoped(self):
        value = {
            "id": "strategy-1",
            "market": "us",
            "parameters": {"market": {"enabled": True, "value": "us"}},
            "lifecycle": {"stage": "paper"},
        }
        with patch.dict(os.environ, {"STOCK_AGENT_EXECUTION_BACKEND": "simulation"}):
            self.assertIsNone(execution_backend_for_strategy(value))
        marker = object()
        with (
            patch.dict(os.environ, {"STOCK_AGENT_EXECUTION_BACKEND": "alpaca_paper"}),
            patch.object(AlpacaPaperBroker, "from_env", return_value=marker) as factory,
        ):
            self.assertIs(execution_backend_for_strategy(value), marker)
        factory.assert_called_once_with("strategy-1")

    def test_live_trading_url_is_rejected_before_client_creation(self):
        with patch.dict(
            os.environ,
            {
                "STOCK_AGENT_ALPACA_API_KEY_ID": "key",
                "STOCK_AGENT_ALPACA_API_SECRET_KEY": "secret",
                "STOCK_AGENT_ALPACA_TRADING_URL": "https://api.alpaca.markets/v2",
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "Paper Trading endpoint"):
                AlpacaPaperBroker.from_env("strategy-1")


class SequenceBroker:
    name = "alpaca_paper_execution"

    def __init__(self, snapshots):
        self.snapshots = list(snapshots)

    def place_or_get(self, value):
        return self.snapshots.pop(0)


class BrokerExecutionAccountingTests(unittest.TestCase):
    def setUp(self):
        target = TargetPosition(
            symbol="AAPL",
            side=PositionSide.LONG,
            target_weight_pct=10.0,
            signal_score=0.9,
            model_id="model-v1",
            thesis_id="thesis-aapl",
        )
        self.intent = intent_for_delta(
            None,
            target,
            target_quantity=10,
            created_snapshot_id="market-old",
            created_market_at=NOW - timedelta(minutes=5),
        )
        self.account = AccountSnapshot(
            id="account-1",
            strategy_id="strategy-1",
            strategy_revision=1,
            occurred_at=NOW - timedelta(minutes=10),
            available_cash=10_000.0,
        )
        self.policy = ExecutionPolicy(
            market="us",
            lot_size=1,
            same_day_sell=True,
            commission_rate_pct=0.0,
            minimum_commission=0.0,
            stamp_duty_rate_pct=0.0,
            transfer_fee_rate_pct=0.0,
            slippage_bps=999.0,
            max_bar_participation_pct=0.01,
        )

    def observation(self, *, filled, average, status):
        return BrokerOrderSnapshot(
            order_id="alpaca-order-1",
            client_order_id=alpaca_client_order_id("strategy-1", self.intent.id),
            symbol="AAPL",
            side="buy",
            quantity=10,
            filled_quantity=filled,
            filled_average_price=average,
            status=status,
        )

    def test_broker_price_and_quantity_replace_local_fill_assumptions(self):
        first = execute_intents(
            self.account,
            (self.intent,),
            MarketSnapshot(id="market-1", occurred_at=NOW, quotes={"AAPL": {"price": 99}}),
            self.policy,
            broker=SequenceBroker(
                [self.observation(filled=4, average=101.5, status="partially_filled")]
            ),
        )
        self.assertEqual(first.fills[0].quantity, 4)
        self.assertEqual(first.fills[0].price, 101.5)
        self.assertEqual(first.fills[0].status, "PARTIAL")
        self.assertAlmostEqual(first.account.available_cash, 9_594.0)

        second = execute_intents(
            first.account,
            (self.intent,),
            MarketSnapshot(
                id="market-2",
                occurred_at=NOW + timedelta(minutes=1),
                quotes={"AAPL": {"price": 500}},
            ),
            self.policy,
            prior_progress=first.progress,
            broker=SequenceBroker(
                [self.observation(filled=10, average=102.0, status="filled")]
            ),
        )
        self.assertEqual(second.fills[0].quantity, 6)
        self.assertAlmostEqual(second.fills[0].price, (1_020.0 - 406.0) / 6)
        self.assertEqual(second.fills[0].status, "FILLED")
        self.assertEqual(second.progress[0].filled_quantity, 10)
        self.assertAlmostEqual(second.account.available_cash, 8_980.0)

    def test_pending_broker_order_does_not_create_a_fake_fill(self):
        result = execute_intents(
            self.account,
            (self.intent,),
            MarketSnapshot(id="market-1", occurred_at=NOW, quotes={"AAPL": {"price": 99}}),
            self.policy,
            broker=SequenceBroker(
                [self.observation(filled=0, average=None, status="accepted")]
            ),
        )
        self.assertEqual(result.fills, ())
        self.assertEqual(result.diagnostics[0].reason, "BROKER_ORDER_ACCEPTED")
        self.assertEqual(result.account, self.account)

    def test_terminal_unfilled_order_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "terminal"):
            execute_intents(
                self.account,
                (self.intent,),
                MarketSnapshot(
                    id="market-1",
                    occurred_at=NOW,
                    quotes={"AAPL": {"price": 99}},
                ),
                self.policy,
                broker=SequenceBroker(
                    [self.observation(filled=0, average=None, status="rejected")]
                ),
            )


if __name__ == "__main__":
    unittest.main()
