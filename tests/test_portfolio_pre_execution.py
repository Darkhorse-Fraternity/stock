from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

from stock_recommender.pipeline import StageInput
from stock_recommender.portfolio_engine.borrow import (
    AVAILABLE,
    BorrowSecurity,
    BorrowSnapshot,
)
from stock_recommender.portfolio_engine.config import (
    ExposurePolicy,
    MarginPolicy,
    ShortPolicy,
)
from stock_recommender.portfolio_engine.contracts import (
    AccrualLifecycle,
    AccountSnapshot,
    MarketSnapshot,
    OrderIntent,
    OrderSide,
    PositionEffect,
    PositionSide,
    PositionSnapshot,
    stable_execution_intent_id,
)
from stock_recommender.portfolio_engine.execution import (
    ExecutionPolicy,
    execute_intents,
)


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
    created_market_at: datetime | None = None,
    reason: str = "planned entry",
) -> OrderIntent:
    increases = position_effect in {PositionEffect.OPEN, PositionEffect.INCREASE}
    order_side = (
        OrderSide.BUY
        if (position_side is PositionSide.LONG) == increases
        else OrderSide.SELL
    )
    values = {
        "symbol": symbol,
        "position_side": position_side,
        "order_side": order_side,
        "position_effect": position_effect,
        "quantity": quantity,
        "reason": reason,
        "created_snapshot_id": "market-planned",
        "created_market_at": created_market_at or NOW.replace(minute=30),
    }
    return OrderIntent(
        id=stable_execution_intent_id(**values),
        **values,
    )


def market(
    price: float,
    *,
    symbol: str = "L",
    occurred_at: datetime = NOW,
) -> MarketSnapshot:
    return MarketSnapshot(
        id=f"market-current-{symbol}-{price}-{occurred_at.isoformat()}",
        occurred_at=occurred_at,
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


def execution_policy(
    *,
    minimum_commission: float = 0.0,
    max_bar_participation_pct: float = 100.0,
) -> ExecutionPolicy:
    return ExecutionPolicy(
        market="us",
        lot_size=1,
        same_day_sell=True,
        commission_rate_pct=0.0,
        minimum_commission=minimum_commission,
        stamp_duty_rate_pct=0.0,
        transfer_fee_rate_pct=0.0,
        slippage_bps=0.0,
        max_bar_participation_pct=max_bar_participation_pct,
    )


def available_borrow(
    symbol: str = "S",
    *,
    quantity: int | None = None,
    apr: float | None = 2.0,
    shortable: bool = True,
    easy_to_borrow: bool = True,
) -> BorrowSnapshot:
    return BorrowSnapshot(
        id=f"borrow-{symbol}",
        status=AVAILABLE,
        securities={
            symbol: BorrowSecurity(
                symbol=symbol,
                shortable=shortable,
                easy_to_borrow=easy_to_borrow,
                borrow_apr_pct=apr,
                available_quantity=quantity,
            )
        },
    )


def long_short_exposure() -> ExposurePolicy:
    return ExposurePolicy(
        mode="LONG_SHORT",
        max_positions=10,
        max_gross_exposure_pct=200.0,
        max_net_exposure_pct=200.0,
        max_long_exposure_pct=200.0,
        max_short_exposure_pct=200.0,
        max_long_position_pct=200.0,
        max_short_position_pct=200.0,
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
        borrow_snapshot: BorrowSnapshot | None = None,
        short_policy: ShortPolicy | None = None,
        progress: tuple = (),
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
            borrow_snapshot=borrow_snapshot or BorrowSnapshot.unavailable(),
            short_policy=short_policy or ShortPolicy(),
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
                    {"kind": "execution_progress", "items": progress},
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
            borrow_snapshot=available_borrow(quantity=1_000),
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

    def test_admission_order_is_fifo_and_independent_of_caller_permutation(self):
        earlier = intent(
            1,
            symbol="B",
            created_market_at=NOW - timedelta(minutes=2),
            reason="earlier",
        )
        later = intent(
            1,
            symbol="A",
            created_market_at=NOW - timedelta(minutes=1),
            reason="later",
        )
        quote = MarketSnapshot(
            id="market-fifo-cash",
            occurred_at=NOW,
            quotes={
                symbol: {
                    "price": 100.0,
                    "bar_open": 100.0,
                    "bar_high": 100.0,
                    "bar_low": 100.0,
                    "bar_volume": 1_000_000,
                }
                for symbol in ("A", "B")
            },
        )
        for orders in ((later, earlier), (earlier, later)):
            output = self._evaluate(
                orders,
                quote,
                current_account=account(available_cash=100.0),
            )
            admitted = next(
                fact
                for fact in output.facts
                if fact["kind"] == "pre_execution_admitted_intents"
            )
            self.assertEqual(admitted["items"], (earlier,))

        same_time_a = intent(1, symbol="A", reason="same-time-a")
        same_time_b = intent(1, symbol="B", reason="same-time-b")
        output = self._evaluate(
            (same_time_b, same_time_a),
            quote,
            current_account=account(available_cash=100.0),
        )
        admitted = next(
            fact
            for fact in output.facts
            if fact["kind"] == "pre_execution_admitted_intents"
        )
        self.assertEqual(admitted["items"], (same_time_a,))

        same_symbol = (
            intent(1, symbol="A", reason="id-tie-first"),
            intent(1, symbol="A", reason="id-tie-second"),
        )
        expected = min(same_symbol, key=lambda item: item.id)
        for orders in (same_symbol, tuple(reversed(same_symbol))):
            output = self._evaluate(
                orders,
                quote,
                current_account=account(available_cash=100.0),
            )
            admitted = next(
                fact
                for fact in output.facts
                if fact["kind"] == "pre_execution_admitted_intents"
            )
            self.assertEqual(admitted["items"], (expected,))

    def test_short_borrow_reservation_uses_fifo_not_caller_order(self):
        earlier = intent(
            40,
            symbol="S",
            position_side=PositionSide.SHORT,
            position_effect=PositionEffect.INCREASE,
            created_market_at=NOW - timedelta(minutes=2),
            reason="earlier-short",
        )
        later = intent(
            40,
            symbol="S",
            position_side=PositionSide.SHORT,
            created_market_at=NOW - timedelta(minutes=1),
            reason="later-short",
        )
        partial_market = MarketSnapshot(
            id="market-partial-borrow-fifo",
            occurred_at=NOW - timedelta(seconds=30),
            quotes={
                "S": {
                    "price": 100.0,
                    "bar_open": 100.0,
                    "bar_high": 100.0,
                    "bar_low": 100.0,
                    "bar_volume": 20,
                }
            },
        )
        partial = execute_intents(
            account(),
            (later,),
            partial_market,
            execution_policy(),
        )
        self.assertEqual(partial.progress[0].filled_quantity, 20)
        for orders in ((later, earlier), (earlier, later)):
            output = self._evaluate(
                orders,
                market(100.0, symbol="S"),
                current_account=partial.account,
                exposure=long_short_exposure(),
                borrow_snapshot=available_borrow(quantity=50),
                progress=partial.progress,
            )
            admitted = next(
                fact
                for fact in output.facts
                if fact["kind"] == "pre_execution_admitted_intents"
            )
            self.assertEqual(admitted["items"], (earlier,))

    def test_hard_caps_have_no_float_tolerance_at_any_scale(self):
        from stock_recommender.portfolio_engine.pre_execution import hard_cap_breaches

        exposure = ExposurePolicy(
            mode="LONG_ONLY",
            max_positions=10,
            max_gross_exposure_pct=100.0,
            max_net_exposure_pct=100.0,
            max_long_exposure_pct=100.0,
            max_short_exposure_pct=0.0,
            max_long_position_pct=100.0,
            max_short_position_pct=0.0,
        )
        for scale in (1e-200, 100.0, 1e200):
            with self.subTest(scale=scale, boundary="exact"):
                exact = account(
                    available_cash=0.0,
                    positions=(
                        PositionSnapshot(
                            symbol="L",
                            side=PositionSide.LONG,
                            quantity=1,
                            average_cost=scale,
                            current_price=scale,
                        ),
                    ),
                )
                self.assertNotIn(
                    "GROSS_EXPOSURE_CAP",
                    hard_cap_breaches(exact, market(scale), exposure, MarginPolicy()),
                )

            with self.subTest(scale=scale, boundary="100.0000000005"):
                loan = scale * 5e-12
                beyond = AccountSnapshot(
                    id="account-admission",
                    strategy_id="strategy-admission",
                    strategy_revision=1,
                    occurred_at=NOW,
                    available_cash=0.0,
                    margin_loan=loan,
                    financing_lifecycle=AccrualLifecycle(
                        id=f"financing-{scale}",
                        started_on=NOW.date(),
                    ),
                    positions=(
                        PositionSnapshot(
                            symbol="L",
                            side=PositionSide.LONG,
                            quantity=1,
                            average_cost=scale,
                            current_price=scale,
                        ),
                    ),
                    snapshot_id="account-before",
                )
                self.assertIn(
                    "GROSS_EXPOSURE_CAP",
                    hard_cap_breaches(beyond, market(scale), exposure, MarginPolicy()),
                )

        nextafter_loan = math.ulp(100.0)
        nextafter_account = AccountSnapshot(
            id="account-admission",
            strategy_id="strategy-admission",
            strategy_revision=1,
            occurred_at=NOW,
            available_cash=0.0,
            margin_loan=nextafter_loan,
            financing_lifecycle=AccrualLifecycle(
                id="financing-nextafter",
                started_on=NOW.date(),
            ),
            positions=(
                PositionSnapshot(
                    symbol="L",
                    side=PositionSide.LONG,
                    quantity=1,
                    average_cost=100.0,
                    current_price=100.0,
                ),
            ),
            snapshot_id="account-before",
        )
        self.assertIn(
            "GROSS_EXPOSURE_CAP",
            hard_cap_breaches(
                nextafter_account,
                market(100.0),
                exposure,
                MarginPolicy(),
            ),
        )

    def test_existing_policy_breach_is_baseline_aware_and_reduce_only_safe(self):
        held = PositionSnapshot(
            symbol="S",
            side=PositionSide.SHORT,
            quantity=1,
            average_cost=100.0,
            current_price=100.0,
        )
        existing = AccountSnapshot(
            id="account-admission",
            strategy_id="strategy-admission",
            strategy_revision=1,
            occurred_at=NOW,
            available_cash=1_000.0,
            restricted_short_proceeds=100.0,
            positions=(held,),
            snapshot_id="account-before",
        )
        permissive_long_only = ExposurePolicy(
            mode="LONG_ONLY",
            max_positions=10,
            max_gross_exposure_pct=200.0,
            max_net_exposure_pct=200.0,
            max_long_exposure_pct=200.0,
            max_short_exposure_pct=0.0,
            max_long_position_pct=200.0,
            max_short_position_pct=0.0,
        )
        non_worsening_long = intent(1, symbol="L")
        reducing_cover = intent(
            1,
            symbol="S",
            position_side=PositionSide.SHORT,
            position_effect=PositionEffect.CLOSE,
        )
        worsening_short = intent(
            1,
            symbol="S",
            position_side=PositionSide.SHORT,
            position_effect=PositionEffect.INCREASE,
            reason="worsening-short",
        )
        quote = MarketSnapshot(
            id="market-baseline-policy-breach",
            occurred_at=NOW,
            quotes={
                symbol: {
                    "price": 100.0,
                    "bar_open": 100.0,
                    "bar_high": 100.0,
                    "bar_low": 100.0,
                    "bar_volume": 1_000_000,
                }
                for symbol in ("L", "S")
            },
        )

        non_worsening = self._evaluate(
            non_worsening_long,
            quote,
            current_account=existing,
            exposure=permissive_long_only,
        )
        admitted = next(
            fact
            for fact in non_worsening.facts
            if fact["kind"] == "pre_execution_admitted_intents"
        )
        self.assertEqual(admitted["items"], (non_worsening_long,))

        reduction = self._evaluate(
            reducing_cover,
            quote,
            current_account=existing,
            exposure=permissive_long_only,
        )
        admitted = next(
            fact
            for fact in reduction.facts
            if fact["kind"] == "pre_execution_admitted_intents"
        )
        self.assertEqual(admitted["items"], (reducing_cover,))

        worsening = self._evaluate(
            worsening_short,
            quote,
            current_account=existing,
            exposure=permissive_long_only,
            borrow_snapshot=available_borrow(quantity=10),
        )
        admitted = next(
            fact
            for fact in worsening.facts
            if fact["kind"] == "pre_execution_admitted_intents"
        )
        diagnostic = next(
            fact
            for fact in worsening.facts
            if fact["kind"] == "pre_execution_diagnostics"
        )
        self.assertEqual(admitted["items"], ())
        self.assertIn(
            diagnostic["items"][0]["reason"],
            {"LONG_ONLY_SHORT_FORBIDDEN", "SHORT_EXPOSURE_CAP"},
        )

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

    def test_short_increase_rechecks_current_flags_rate_and_quantity(self):
        short_order = intent(100, symbol="S", position_side=PositionSide.SHORT)
        policy = ShortPolicy(estimated_borrow_apr_pct=8.0, cost_stress_multiplier=2.0)
        cases = (
            (BorrowSnapshot.unavailable(), "BORROW_DATA_MISSING"),
            (available_borrow(quantity=100, shortable=False), "SHORT_NOT_SHORTABLE"),
            (available_borrow(quantity=99), "BORROW_QUANTITY_INSUFFICIENT"),
            (
                available_borrow(quantity=100, apr=math.nextafter(16.0, math.inf)),
                "BORROW_RATE_TOO_HIGH",
            ),
        )
        for current_borrow, reason in cases:
            with self.subTest(reason=reason):
                output = self._evaluate(
                    short_order,
                    market(100.0, symbol="S"),
                    exposure=long_short_exposure(),
                    borrow_snapshot=current_borrow,
                    short_policy=policy,
                )
                admitted = next(
                    fact for fact in output.facts
                    if fact["kind"] == "pre_execution_admitted_intents"
                )
                diagnostic = next(
                    fact for fact in output.facts
                    if fact["kind"] == "pre_execution_diagnostics"
                )
                self.assertEqual(admitted["items"], ())
                self.assertEqual(diagnostic["items"][0]["reason"], reason)

        boundary = self._evaluate(
            short_order,
            market(100.0, symbol="S"),
            exposure=long_short_exposure(),
            borrow_snapshot=available_borrow(quantity=100, apr=16.0),
            short_policy=policy,
        )
        admitted = next(
            fact for fact in boundary.facts
            if fact["kind"] == "pre_execution_admitted_intents"
        )
        self.assertEqual(admitted["items"], (short_order,))

    def test_partial_progress_checks_only_remaining_short_quantity(self):
        short_order = intent(100, symbol="S", position_side=PositionSide.SHORT)
        prior_market = market(
            100.0,
            symbol="S",
            occurred_at=NOW - timedelta(minutes=1),
        )
        prior_market = MarketSnapshot(
            id=prior_market.id,
            occurred_at=prior_market.occurred_at,
            quotes={
                "S": {
                    "price": 100.0,
                    "bar_open": 100.0,
                    "bar_high": 100.0,
                    "bar_low": 100.0,
                    "bar_volume": 60,
                }
            },
        )
        prior = execute_intents(
            account(),
            (short_order,),
            prior_market,
            execution_policy(),
        )
        self.assertEqual(prior.progress[0].filled_quantity, 60)
        for quantity, admitted_expected in ((40, True), (39, False)):
            with self.subTest(quantity=quantity):
                output = self._evaluate(
                    short_order,
                    market(100.0, symbol="S"),
                    current_account=prior.account,
                    exposure=long_short_exposure(),
                    borrow_snapshot=available_borrow(quantity=quantity),
                    progress=prior.progress,
                )
                admitted = next(
                    fact for fact in output.facts
                    if fact["kind"] == "pre_execution_admitted_intents"
                )
                self.assertEqual(bool(admitted["items"]), admitted_expected)

    def test_multiple_short_increases_reserve_borrow_sequentially(self):
        first = intent(30, symbol="S", position_side=PositionSide.SHORT)
        second = intent(
            30,
            symbol="S",
            position_side=PositionSide.SHORT,
            position_effect=PositionEffect.INCREASE,
        )
        output = self._evaluate(
            (first, second),
            market(100.0, symbol="S"),
            exposure=long_short_exposure(),
            borrow_snapshot=available_borrow(quantity=50),
        )
        admitted = next(
            fact for fact in output.facts
            if fact["kind"] == "pre_execution_admitted_intents"
        )
        diagnostic = next(
            fact for fact in output.facts
            if fact["kind"] == "pre_execution_diagnostics"
        )
        self.assertEqual(admitted["items"], (first,))
        self.assertEqual(
            diagnostic["items"][0]["reason"],
            "BORROW_QUANTITY_INSUFFICIENT",
        )

    def test_short_cover_never_requires_current_borrow(self):
        held = PositionSnapshot(
            symbol="S",
            side=PositionSide.SHORT,
            quantity=100,
            average_cost=100.0,
            current_price=100.0,
        )
        closing = intent(
            100,
            symbol="S",
            position_side=PositionSide.SHORT,
            position_effect=PositionEffect.CLOSE,
        )
        current_account = AccountSnapshot(
            id="account-admission",
            strategy_id="strategy-admission",
            strategy_revision=1,
            occurred_at=NOW,
            available_cash=100_000.0,
            restricted_short_proceeds=10_000.0,
            positions=(held,),
            snapshot_id="account-before",
        )
        output = self._evaluate(
            closing,
            market(100.0, symbol="S"),
            current_account=current_account,
            exposure=long_short_exposure(),
            borrow_snapshot=BorrowSnapshot.unavailable(),
        )
        admitted = next(
            fact for fact in output.facts
            if fact["kind"] == "pre_execution_admitted_intents"
        )
        self.assertEqual(admitted["items"], (closing,))


if __name__ == "__main__":
    unittest.main()
