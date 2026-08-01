from __future__ import annotations

import unittest
from datetime import datetime, timezone

from stock_recommender.pipeline import StageInput
from stock_recommender.portfolio_engine.config import ExposurePolicy, MarginPolicy
from stock_recommender.portfolio_engine.contracts import (
    AccountSnapshot,
    MarketSnapshot,
    OrderIntent,
    OrderSide,
    PositionEffect,
    PositionSide,
    PositionSnapshot,
    stable_execution_intent_id,
)
from stock_recommender.portfolio_engine.execution import ExecutionPolicy


NOW = datetime(2026, 8, 3, 14, 35, tzinfo=timezone.utc)


def account(
    *,
    available_cash: float = 100_000.0,
    positions: tuple[PositionSnapshot, ...] = (),
) -> AccountSnapshot:
    return AccountSnapshot(
        id="account-admission",
        strategy_id="strategy-admission",
        strategy_revision=1,
        occurred_at=NOW,
        available_cash=available_cash,
        positions=positions,
        snapshot_id="account-before",
    )


def intent(
    quantity: int,
    *,
    symbol: str = "L",
    position_side: PositionSide = PositionSide.LONG,
    position_effect: PositionEffect = PositionEffect.OPEN,
) -> OrderIntent:
    order_side = (
        OrderSide.BUY
        if (position_side is PositionSide.LONG) == (position_effect is PositionEffect.OPEN)
        else OrderSide.SELL
    )
    values = {
        "symbol": symbol,
        "position_side": position_side,
        "order_side": order_side,
        "position_effect": position_effect,
        "quantity": quantity,
        "reason": "planned entry",
        "created_snapshot_id": "market-planned",
        "created_market_at": NOW.replace(minute=30),
    }
    return OrderIntent(
        id=stable_execution_intent_id(**values),
        **values,
    )


def market(price: float, *, symbol: str = "L") -> MarketSnapshot:
    return MarketSnapshot(
        id=f"market-current-{price}",
        occurred_at=NOW,
        quotes={
            symbol: {
                "price": price,
                "bar_open": price,
                "bar_high": price,
                "bar_low": price,
                "bar_volume": 1_000_000,
            }
        },
    )


def execution_policy(*, minimum_commission: float = 0.0) -> ExecutionPolicy:
    return ExecutionPolicy(
        market="us",
        lot_size=1,
        same_day_sell=True,
        commission_rate_pct=0.0,
        minimum_commission=minimum_commission,
        stamp_duty_rate_pct=0.0,
        transfer_fee_rate_pct=0.0,
        slippage_bps=0.0,
        max_bar_participation_pct=100.0,
    )


class PreExecutionAdmissionTests(unittest.TestCase):
    def _evaluate(
        self,
        orders: OrderIntent | tuple[OrderIntent, ...],
        quote: MarketSnapshot,
        *,
        fee: float = 0.0,
        current_account: AccountSnapshot | None = None,
        exposure: ExposurePolicy | None = None,
        margin_policy: MarginPolicy | None = None,
    ):
        from stock_recommender.portfolio_engine.pre_execution import (
            PreExecutionAdmissionStage,
        )

        return PreExecutionAdmissionStage(
            current_account or account(),
            quote,
            exposure or ExposurePolicy(
                mode="LONG_ONLY",
                max_positions=10,
                max_gross_exposure_pct=100.0,
                max_net_exposure_pct=100.0,
                max_long_exposure_pct=100.0,
                max_short_exposure_pct=0.0,
                max_long_position_pct=100.0,
                max_short_position_pct=0.0,
            ),
            margin_policy or MarginPolicy(),
            execution_policy(minimum_commission=fee),
        ).evaluate(
            StageInput(
                run_id="process:admission",
                strategy_id="strategy-admission",
                strategy_version=1,
                as_of=NOW.isoformat(),
                market_snapshot_id=quote.id,
                portfolio_snapshot_id="account-before",
                upstream_facts=(
                    {
                        "kind": "order_intents",
                        "items": orders if isinstance(orders, tuple) else (orders,),
                    },
                    {"kind": "execution_progress", "items": ()},
                ),
            )
        )

    def test_price_gap_rejects_stale_risk_increase_before_execution(self):
        output = self._evaluate(intent(100), market(2_000.0))
        admitted = next(
            fact for fact in output.facts if fact["kind"] == "pre_execution_admitted_intents"
        )
        diagnostic = next(
            fact for fact in output.facts if fact["kind"] == "pre_execution_diagnostics"
        )
        self.assertEqual(admitted["items"], ())
        self.assertEqual(diagnostic["items"][0]["reason"], "GROSS_EXPOSURE_CAP")

    def test_exact_cap_is_admitted_but_fee_induced_breach_is_rejected(self):
        exact = self._evaluate(intent(100), market(1_000.0))
        exact_admitted = next(
            fact for fact in exact.facts if fact["kind"] == "pre_execution_admitted_intents"
        )
        self.assertEqual(exact_admitted["items"], (intent(100),))

        with_fee = self._evaluate(intent(100), market(1_000.0), fee=1.0)
        fee_admitted = next(
            fact for fact in with_fee.facts if fact["kind"] == "pre_execution_admitted_intents"
        )
        fee_diagnostic = next(
            fact for fact in with_fee.facts if fact["kind"] == "pre_execution_diagnostics"
        )
        self.assertEqual(fee_admitted["items"], ())
        self.assertEqual(fee_diagnostic["items"][0]["reason"], "GROSS_EXPOSURE_CAP")

    def test_short_admission_uses_absolute_net_exposure(self):
        exposure = ExposurePolicy(
            mode="LONG_SHORT",
            max_positions=10,
            max_gross_exposure_pct=200.0,
            max_net_exposure_pct=50.0,
            max_long_exposure_pct=200.0,
            max_short_exposure_pct=200.0,
            max_long_position_pct=200.0,
            max_short_position_pct=200.0,
        )
        output = self._evaluate(
            intent(600, symbol="S", position_side=PositionSide.SHORT),
            market(100.0, symbol="S"),
            exposure=exposure,
        )
        admitted = next(
            fact for fact in output.facts if fact["kind"] == "pre_execution_admitted_intents"
        )
        diagnostic = next(
            fact for fact in output.facts if fact["kind"] == "pre_execution_diagnostics"
        )
        self.assertEqual(admitted["items"], ())
        self.assertEqual(diagnostic["items"][0]["reason"], "NET_EXPOSURE_CAP")

    def test_multiple_intents_are_admitted_against_cumulative_projected_account(self):
        orders = (intent(600, symbol="L1"), intent(600, symbol="L2"))
        quote = MarketSnapshot(
            id="market-current-multi",
            occurred_at=NOW,
            quotes={
                symbol: {
                    "price": 100.0,
                    "bar_open": 100.0,
                    "bar_high": 100.0,
                    "bar_low": 100.0,
                    "bar_volume": 1_000_000,
                }
                for symbol in ("L1", "L2")
            },
        )
        output = self._evaluate(orders, quote)
        admitted = next(
            fact for fact in output.facts if fact["kind"] == "pre_execution_admitted_intents"
        )
        rejected = next(
            fact for fact in output.facts if fact["kind"] == "pre_execution_rejected_intents"
        )
        self.assertEqual(admitted["items"], (orders[0],))
        self.assertEqual(rejected["items"], (orders[1],))

    def test_risk_reduction_is_applied_before_risk_increase(self):
        held = PositionSnapshot(
            symbol="L",
            side=PositionSide.LONG,
            quantity=100,
            average_cost=100.0,
            current_price=100.0,
        )
        opening = intent(100, symbol="L2")
        closing = intent(
            100,
            symbol="L",
            position_effect=PositionEffect.CLOSE,
        )
        quote = MarketSnapshot(
            id="market-current-reduce-first",
            occurred_at=NOW,
            quotes={
                symbol: {
                    "price": 100.0,
                    "bar_open": 100.0,
                    "bar_high": 100.0,
                    "bar_low": 100.0,
                    "bar_volume": 1_000_000,
                }
                for symbol in ("L", "L2")
            },
        )
        output = self._evaluate(
            (opening, closing),
            quote,
            current_account=account(available_cash=0.0, positions=(held,)),
        )
        admitted = next(
            fact for fact in output.facts if fact["kind"] == "pre_execution_admitted_intents"
        )
        self.assertEqual(admitted["items"], (closing, opening))

    def test_margin_maintenance_and_liquidation_buffer_is_a_hard_cap(self):
        exposure = ExposurePolicy(
            mode="LONG_LEVERAGED",
            max_positions=10,
            max_gross_exposure_pct=250.0,
            max_net_exposure_pct=250.0,
            max_long_exposure_pct=250.0,
            max_short_exposure_pct=0.0,
            max_long_position_pct=250.0,
            max_short_position_pct=0.0,
        )
        output = self._evaluate(
            intent(2_000),
            market(100.0),
            exposure=exposure,
            margin_policy=MarginPolicy(
                maintenance_margin_pct=45.0,
                liquidation_buffer_pct=10.0,
            ),
        )
        admitted = next(
            fact for fact in output.facts if fact["kind"] == "pre_execution_admitted_intents"
        )
        diagnostic = next(
            fact for fact in output.facts if fact["kind"] == "pre_execution_diagnostics"
        )
        self.assertEqual(admitted["items"], ())
        self.assertEqual(diagnostic["items"][0]["reason"], "MARGIN_BUFFER_BREACH")


if __name__ == "__main__":
    unittest.main()
