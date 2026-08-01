import copy
import math
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from stock_recommender.portfolio_engine import execution
from stock_recommender.portfolio_engine import contracts as domain_contracts
from stock_recommender.portfolio_engine.contracts import (
    AccountSnapshot,
    ExecutionFill,
    MarketSnapshot,
    OrderIntent,
    OrderSide,
    PositionEffect,
    PositionSide,
    PositionSnapshot,
    TargetPosition,
)
from stock_recommender.portfolio_engine.risk import evaluate_position_risk
from stock_recommender.markets import market_date
from stock_recommender.parameters import default_portfolio_config
from stock_recommender.pipeline import PipelineContractError, PipelineRunner, StageInput


NOW = datetime(2026, 7, 31, 14, 30, tzinfo=timezone.utc)


def target(symbol="AAPL", side=PositionSide.LONG, weight=10.0):
    return TargetPosition(
        symbol=symbol,
        side=side,
        target_weight_pct=weight,
        signal_score=0.9,
        model_id="model-v1",
        thesis_id=f"thesis-{symbol}",
    )


def position(
    symbol="AAPL",
    side=PositionSide.LONG,
    quantity=10,
    average_cost=100.0,
    **updates,
):
    return PositionSnapshot(
        symbol=symbol,
        side=side,
        quantity=quantity,
        average_cost=average_cost,
        **updates,
    )


def account(*, cash=1_000.0, positions=(), **updates):
    values = dict(
        id="account-1",
        strategy_id="strategy-1",
        strategy_revision=2,
        occurred_at=NOW,
        available_cash=cash,
        positions=positions,
    )
    values.update(updates)
    return AccountSnapshot(**values)


def risk_close(existing, *, snapshot_id, reason):
    return OrderIntent(
        id=domain_contracts.stable_risk_intent_id(
            snapshot_id,
            existing,
            reason,
        ),
        symbol=existing.symbol,
        position_side=existing.side,
        order_side=(
            OrderSide.SELL
            if existing.side is PositionSide.LONG
            else OrderSide.BUY
        ),
        position_effect=PositionEffect.CLOSE,
        quantity=existing.quantity,
        reason=reason,
        created_snapshot_id=snapshot_id,
    )


def accrual_lifecycle(lifecycle_id, started_on=date(2026, 7, 30)):
    return domain_contracts.AccrualLifecycle(
        id=lifecycle_id,
        started_on=started_on,
    )


class OrderIntentPlanningTests(unittest.TestCase):
    def test_delta_semantics_are_exact_for_both_directions(self):
        cases = (
            (None, target(side=PositionSide.LONG), 10, ("LONG", "BUY", "OPEN")),
            (position(quantity=5), target(), 10, ("LONG", "BUY", "INCREASE")),
            (position(quantity=15), target(), 10, ("LONG", "SELL", "REDUCE")),
            (position(), None, 0, ("LONG", "SELL", "CLOSE")),
            (None, target(side=PositionSide.SHORT), 10, ("SHORT", "SELL", "OPEN")),
            (
                position(side=PositionSide.SHORT, quantity=5),
                target(side=PositionSide.SHORT),
                10,
                ("SHORT", "SELL", "INCREASE"),
            ),
            (
                position(side=PositionSide.SHORT, quantity=15),
                target(side=PositionSide.SHORT),
                10,
                ("SHORT", "BUY", "REDUCE"),
            ),
            (
                position(side=PositionSide.SHORT),
                None,
                0,
                ("SHORT", "BUY", "CLOSE"),
            ),
        )
        for existing, requested, quantity, expected in cases:
            with self.subTest(expected=expected):
                intent = execution.intent_for_delta(
                    existing,
                    requested,
                    target_quantity=quantity,
                    created_snapshot_id="market-1",
                )
                self.assertEqual(intent.semantic_tuple(), expected)

    def test_reversal_closes_first_and_never_forges_a_single_flip(self):
        existing = position(side=PositionSide.LONG, quantity=7)

        first = execution.intent_for_delta(
            existing,
            target(side=PositionSide.SHORT),
            target_quantity=3,
            created_snapshot_id="market-1",
        )

        self.assertEqual(first.semantic_tuple(), ("LONG", "SELL", "CLOSE"))
        self.assertEqual(first.quantity, 7)

    def test_planned_intent_id_is_stable_and_bound_to_all_semantics(self):
        original = execution.intent_for_delta(
            None,
            target(),
            target_quantity=10,
            created_snapshot_id="market-1",
        )
        repeated = execution.intent_for_delta(
            None,
            target(),
            target_quantity=10,
            created_snapshot_id="market-1",
        )
        self.assertEqual(original, repeated)
        self.assertTrue(execution.verify_intent_id(original))
        for field_name, value in (
            ("quantity", 11),
            ("reason", "OTHER"),
            ("created_snapshot_id", "market-2"),
            ("order_side", OrderSide.SELL),
        ):
            with self.subTest(field=field_name):
                forged = replace(original, **{field_name: value})
                self.assertFalse(execution.verify_intent_id(forged))

    def test_zero_lot_target_closes_existing_position_instead_of_full_reduce(self):
        held = position(quantity=100)

        intent = execution.intent_for_delta(
            held,
            target(weight=0.001),
            target_quantity=0,
            created_snapshot_id="market-1",
        )

        self.assertEqual(intent.position_effect, PositionEffect.CLOSE)
        self.assertEqual(intent.quantity, 100)


class FillSimulationTests(unittest.TestCase):
    @staticmethod
    def _short_open(quantity=1_000, snapshot_id="market-1"):
        return execution.intent_for_delta(
            None,
            target("NVDA", PositionSide.SHORT, 5.0),
            target_quantity=quantity,
            created_snapshot_id=snapshot_id,
        )

    def test_partial_short_fill_respects_market_lot_and_five_percent_volume(self):
        policy = execution.execution_policy("us", default_portfolio_config())

        fill = execution.simulate_fill(
            self._short_open(),
            {"price": 20.0, "bar_volume": 2_000},
            policy,
            current_snapshot_id="market-2",
        )

        self.assertIsInstance(fill, ExecutionFill)
        self.assertEqual(fill.quantity, 100)
        self.assertEqual(fill.status, "PARTIAL")
        self.assertEqual(fill.price, 19.98)
        self.assertEqual(fill.fees, 0.0)

    def test_fill_uses_commission_transfer_stamp_and_bar_bounded_slippage(self):
        config = default_portfolio_config()
        policy = execution.execution_policy("cn", config)
        held = position("600001", quantity=100)
        sell = risk_close(
            held,
            snapshot_id="market-1",
            reason="LONG_STOP_LOSS",
        )

        fill = execution.simulate_fill(
            sell,
            {
                "bar_open": 10.0,
                "bar_low": 9.995,
                "bar_high": 10.1,
                "bar_volume": 10_000,
            },
            policy,
            current_snapshot_id="market-2",
            existing_position=held,
        )

        self.assertEqual(fill.price, 9.995)
        notional = 999.5
        expected = 5.0 + notional * 0.001 / 100 + notional * 0.05 / 100
        self.assertAlmostEqual(fill.fees, expected)
        self.assertEqual(fill.status, "FILLED")

    def test_missing_invalid_or_zero_liquidity_quote_fails_closed(self):
        policy = execution.execution_policy("us", default_portfolio_config())
        cases = (
            None,
            {},
            {"price": math.nan, "bar_volume": 1_000},
            {"price": 0.0, "bar_volume": 1_000},
            {"price": 20.0},
            {"price": 20.0, "bar_volume": 0},
            {"price": 20.0, "bar_volume": math.inf},
        )
        for quote in cases:
            with self.subTest(quote=quote):
                self.assertIsNone(
                    execution.simulate_fill(
                        self._short_open(),
                        quote,
                        policy,
                        current_snapshot_id="market-2",
                    )
                )

    def test_same_snapshot_and_invalid_direction_semantics_fail_closed(self):
        policy = execution.execution_policy("us", default_portfolio_config())
        valid = self._short_open()
        invalid = replace(valid, order_side=OrderSide.BUY)
        quote = {"price": 20.0, "bar_volume": 1_000}

        self.assertIsNone(
            execution.simulate_fill(
                valid,
                quote,
                policy,
                current_snapshot_id="market-1",
            )
        )
        self.assertIsNone(
            execution.simulate_fill(
                invalid,
                quote,
                policy,
                current_snapshot_id="market-2",
            )
        )

    def test_execution_policy_is_frozen_and_rejects_nonfinite_costs(self):
        policy = execution.execution_policy("us", default_portfolio_config())
        with self.assertRaises(FrozenInstanceError):
            policy.__setattr__("lot_size", 2)
        with self.assertRaises(ValueError):
            replace(policy, slippage_bps=math.inf)
        with self.assertRaises(ValueError):
            replace(policy, slippage_bps=10**1_000)
        self.assertIsNone(
            execution.simulate_fill(
                self._short_open(),
                {"price": 10**1_000, "bar_volume": 1_000},
                policy,
                current_snapshot_id="market-2",
            )
        )

    def test_forged_stable_id_and_unsafe_decimal_price_fail_closed(self):
        policy = execution.execution_policy("us", default_portfolio_config())
        valid = self._short_open()
        forged = replace(valid, quantity=valid.quantity + 1)

        self.assertIsNone(
            execution.simulate_fill(
                forged,
                {"price": 20.0, "bar_volume": 10_000},
                policy,
                current_snapshot_id="market-2",
            )
        )
        self.assertIsNone(
            execution.simulate_fill(
                valid,
                {"price": 1e308, "bar_volume": 10_000},
                policy,
                current_snapshot_id="market-2",
            )
        )

    def test_contradictory_ohlc_and_reference_price_fail_closed(self):
        policy = execution.execution_policy("us", default_portfolio_config())
        intent = self._short_open()
        cases = (
            {"bar_open": 12.0, "bar_low": 9.0, "bar_high": 10.0, "bar_volume": 10_000},
            {
                "bar_open": 9.5,
                "price": 11.0,
                "bar_low": 9.0,
                "bar_high": 10.0,
                "bar_volume": 10_000,
            },
            {"bar_open": 9.5, "bar_low": 10.0, "bar_high": 9.0, "bar_volume": 10_000},
        )
        for quote in cases:
            with self.subTest(quote=quote):
                self.assertIsNone(
                    execution.simulate_fill(
                        intent,
                        quote,
                        policy,
                        current_snapshot_id="market-2",
                    )
                )


class RebalancePlanningStageTests(unittest.TestCase):
    @staticmethod
    def _market(quotes):
        return MarketSnapshot(id="market-1", occurred_at=NOW, quotes=quotes)

    def test_batch_planning_is_stable_risk_first_and_fail_closed_per_symbol(self):
        original = account(
            positions=(
                position("A", quantity=5),
                position("B", quantity=10),
            )
        )
        targets = (
            target("C", weight=10.0),
            target("B", weight=5.0),
            target("D", weight=10.0),
        )
        policy = execution.execution_policy("us", default_portfolio_config())

        result = execution.plan_rebalance_intents(
            original,
            targets,
            self._market(
                {
                    "A": {"price": 100.0, "bar_volume": 1_000},
                    "B": {"price": 100.0, "bar_volume": 1_000},
                    "C": {"price": 100.0, "bar_volume": 1_000},
                }
            ),
            policy,
            account_equity=10_000.0,
        )

        self.assertEqual(
            [(item.symbol, item.position_effect.value) for item in result.intents],
            [("A", "CLOSE"), ("B", "REDUCE"), ("C", "OPEN")],
        )
        self.assertEqual(
            [(item.symbol, item.reason) for item in result.diagnostics],
            [("D", "MARKET_PRICE_MISSING")],
        )
        self.assertEqual(
            result,
            execution.plan_rebalance_intents(
                original,
                tuple(reversed(targets)),
                self._market(
                    {
                        "C": {"price": 100.0, "bar_volume": 1_000},
                        "B": {"price": 100.0, "bar_volume": 1_000},
                        "A": {"price": 100.0, "bar_volume": 1_000},
                    }
                ),
                policy,
                account_equity=10_000.0,
            ),
        )

    def test_stage_prefers_borrow_admitted_targets_over_retained_exposure_fact(self):
        policy = execution.execution_policy("us", default_portfolio_config())
        stage = execution.RebalanceIntentStage(
            account(),
            self._market({"AAPL": {"price": 100.0, "bar_volume": 1_000}}),
            policy,
            account_equity=10_000.0,
        )

        output = stage.evaluate(
            StageInput(
                run_id="run-1",
                strategy_id="strategy-1",
                strategy_version=2,
                as_of=NOW.isoformat(),
                market_snapshot_id="market-1",
                portfolio_snapshot_id="account-1",
                upstream_facts=(
                    {"kind": "exposure_targets", "items": (target("REJECTED"),)},
                    {"kind": "borrow_targets", "items": (target("AAPL"),)},
                ),
            )
        )

        self.assertEqual([item.symbol for item in output.facts[0]["items"]], ["AAPL"])

    def test_opposite_target_closes_existing_position_even_without_target_quote(self):
        result = execution.plan_rebalance_intents(
            account(positions=(position("A", PositionSide.LONG, 10),)),
            (target("A", PositionSide.SHORT, 5.0),),
            self._market({}),
            execution.execution_policy("us", default_portfolio_config()),
            account_equity=10_000.0,
        )

        self.assertEqual(result.diagnostics, ())
        self.assertEqual(result.intents[0].semantic_tuple(), ("LONG", "SELL", "CLOSE"))

    def test_existing_position_closes_when_target_rounds_below_one_lot(self):
        result = execution.plan_rebalance_intents(
            account(positions=(position("600001", quantity=100),)),
            (target("600001", weight=0.001),),
            self._market({"600001": {"price": 100.0, "bar_volume": 10_000}}),
            execution.execution_policy("cn", default_portfolio_config()),
            account_equity=10_000.0,
        )

        self.assertEqual(result.diagnostics, ())
        self.assertEqual(result.intents[0].position_effect, PositionEffect.CLOSE)


class AccountExecutionTests(unittest.TestCase):
    @staticmethod
    def _market(*args, volume=1_000):
        if len(args) == 1:
            return MarketSnapshot(id="market-1", occurred_at=NOW, quotes=args[0])
        snapshot_id, occurred_at, symbol, price = args
        return MarketSnapshot(
            id=snapshot_id,
            occurred_at=occurred_at,
            quotes={symbol: {"price": price, "bar_volume": volume}},
        )

    @staticmethod
    def _zero_cost_policy(market="us", participation=5.0):
        return replace(
            execution.execution_policy(market, default_portfolio_config()),
            commission_rate_pct=0.0,
            minimum_commission=0.0,
            stamp_duty_rate_pct=0.0,
            transfer_fee_rate_pct=0.0,
            slippage_bps=0.0,
            max_bar_participation_pct=participation,
        )

    def test_short_open_proceeds_are_restricted_and_close_releases_basis(self):
        original = account(cash=0.0)
        before = copy.deepcopy(original)
        open_short = execution.intent_for_delta(
            None,
            target("S", PositionSide.SHORT, 5.0),
            target_quantity=10,
            created_snapshot_id="market-1",
        )
        opened = execution.execute_intents(
            original,
            (open_short,),
            self._market("market-2", NOW + timedelta(minutes=5), "S", 100.0),
            self._zero_cost_policy(),
        )

        self.assertEqual(original, before)
        self.assertEqual(opened.account.available_cash, 0.0)
        self.assertEqual(opened.account.restricted_short_proceeds, 1_000.0)
        self.assertEqual(opened.account.positions[0].side, PositionSide.SHORT)
        close = risk_close(
            opened.account.positions[0],
            snapshot_id="market-2",
            reason="SHORT_STOP_LOSS",
        )
        closed = execution.execute_intents(
            opened.account,
            (close,),
            self._market("market-3", NOW + timedelta(minutes=10), "S", 80.0),
            self._zero_cost_policy(),
        )

        self.assertEqual(closed.account.positions, ())
        self.assertEqual(closed.account.restricted_short_proceeds, 0.0)
        self.assertEqual(closed.account.available_cash, 200.0)
        self.assertEqual(closed.account.margin_loan, 0.0)

    def test_partial_short_close_reuses_proportional_basis_settlement(self):
        original = account(
            cash=0.0,
            positions=(position("S", PositionSide.SHORT, 10, 100.0),),
            restricted_short_proceeds=1_000.0,
        )
        close = risk_close(
            original.positions[0],
            snapshot_id="market-1",
            reason="MARGIN_CALL",
        )

        result = execution.execute_intents(
            original,
            (close,),
            self._market("market-2", NOW + timedelta(minutes=5), "S", 80.0, volume=4),
            self._zero_cost_policy(participation=100.0),
        )

        self.assertEqual(result.fills[0].status, "PARTIAL")
        self.assertEqual(result.fills[0].quantity, 4)
        self.assertEqual(result.account.restricted_short_proceeds, 600.0)
        self.assertEqual(result.account.available_cash, 80.0)
        self.assertEqual(result.account.positions[0].quantity, 6)
        self.assertEqual(result.account.positions[0].average_cost, 100.0)

    def test_leveraged_long_consumes_cash_then_loan_and_sale_repay_loan_first(self):
        original = account(cash=50.0)
        open_long = execution.intent_for_delta(
            None,
            target("L", PositionSide.LONG, 10.0),
            target_quantity=10,
            created_snapshot_id="market-1",
        )
        opened = execution.execute_intents(
            original,
            (open_long,),
            self._market("market-2", NOW + timedelta(minutes=5), "L", 100.0),
            self._zero_cost_policy(),
        )
        self.assertEqual(opened.account.available_cash, 0.0)
        self.assertEqual(opened.account.margin_loan, 950.0)

        close = risk_close(
            opened.account.positions[0],
            snapshot_id="market-2",
            reason="LONG_STOP_LOSS",
        )
        closed = execution.execute_intents(
            opened.account,
            (close,),
            self._market("market-3", NOW + timedelta(minutes=10), "L", 100.0),
            self._zero_cost_policy(),
        )
        self.assertEqual(closed.account.margin_loan, 0.0)
        self.assertEqual(closed.account.available_cash, 50.0)

    def test_fee_only_margin_loan_gets_financing_lifecycle_and_accrues(self):
        policy = replace(
            self._zero_cost_policy("us", participation=100.0),
            minimum_commission=5.0,
        )
        intent = execution.intent_for_delta(
            None,
            target("L"),
            target_quantity=10,
            created_snapshot_id="market-1",
        )
        filled = execution.execute_intents(
            account(cash=1_000.0),
            (intent,),
            self._market("market-2", NOW, "L", 100.0),
            policy,
        )

        self.assertEqual(filled.account.margin_loan, 5.0)
        self.assertIsNotNone(filled.account.financing_lifecycle)
        carry = execution.accrue_carry_costs(
            filled.account,
            as_of=market_date(NOW, "us") + timedelta(days=1),
            financing_apr_pct=3_650.0,
        )
        self.assertEqual(carry.financing_cost, 0.5)
        self.assertEqual(carry.new_accruals[0].elapsed_days, 1)

    def test_cn_t_plus_one_blocks_risk_exit_until_next_business_date(self):
        shanghai = ZoneInfo("Asia/Shanghai")
        friday = datetime(2026, 7, 31, 10, 0, tzinfo=shanghai)
        policy = self._zero_cost_policy("cn", participation=100.0)
        open_long = execution.intent_for_delta(
            None,
            target("600001", PositionSide.LONG, 10.0),
            target_quantity=100,
            created_snapshot_id="market-1",
        )
        opened = execution.execute_intents(
            account(cash=2_000.0, occurred_at=friday),
            (open_long,),
            self._market("market-2", friday + timedelta(minutes=5), "600001", 10.0),
            policy,
        )
        held = opened.account.positions[0]
        self.assertEqual(held.sellable_quantity, 0)
        self.assertEqual(held.sellable_on, date(2026, 8, 3))
        risk_exit = risk_close(
            held,
            snapshot_id="market-2",
            reason="LONG_STOP_LOSS",
        )

        blocked = execution.execute_intents(
            opened.account,
            (risk_exit,),
            self._market("market-3", friday + timedelta(minutes=10), "600001", 9.0),
            policy,
        )
        self.assertEqual(blocked.fills, ())
        self.assertEqual(blocked.diagnostics[0].reason, "T_PLUS_ONE_LOCKED")
        monday = datetime(2026, 8, 3, 10, 0, tzinfo=shanghai)
        exited = execution.execute_intents(
            blocked.account,
            (risk_exit,),
            self._market("market-4", monday, "600001", 9.0),
            policy,
        )
        self.assertEqual(exited.account.positions, ())
        self.assertEqual(exited.fills[0].intent_id, risk_exit.id)
        self.assertEqual(risk_exit.semantic_tuple(), ("LONG", "SELL", "CLOSE"))

    def test_us_position_is_sellable_on_same_session_after_later_snapshot(self):
        policy = self._zero_cost_policy("us", participation=100.0)
        open_long = execution.intent_for_delta(
            None,
            target("AAPL"),
            target_quantity=10,
            created_snapshot_id="market-1",
        )
        opened = execution.execute_intents(
            account(cash=2_000.0),
            (open_long,),
            self._market("market-2", NOW + timedelta(minutes=5), "AAPL", 100.0),
            policy,
        )
        self.assertEqual(opened.account.positions[0].sellable_quantity, 10)

    def test_execution_stage_consumes_admitted_intents_without_persistence(self):
        policy = self._zero_cost_policy()
        original = account()
        intent = execution.intent_for_delta(
            None, target(), target_quantity=1, created_snapshot_id="market-1"
        )
        stage = execution.ExecutionSimulationStage(
            original,
            self._market("market-2", NOW + timedelta(minutes=5), "AAPL", 100.0),
            policy,
        )
        output = stage.evaluate(
            StageInput(
                run_id="run-1",
                strategy_id="strategy-1",
                strategy_version=2,
                as_of=NOW.isoformat(),
                market_snapshot_id="market-2",
                portfolio_snapshot_id="account-1",
                upstream_facts=(
                    {"kind": "margin_admitted_intents", "items": (intent,)},
                ),
            )
        )
        self.assertEqual(output.facts[0]["kind"], "execution_fills")
        self.assertEqual(output.facts[0]["items"][0].intent_id, intent.id)
        self.assertEqual(original.positions, ())

    def test_partial_open_resumes_as_increase_and_finishes_without_overfill(self):
        policy = self._zero_cost_policy("us", participation=5.0)
        intent = execution.intent_for_delta(
            None,
            target("S", PositionSide.SHORT, 5.0),
            target_quantity=1_000,
            created_snapshot_id="market-1",
        )
        first = execution.execute_intents(
            account(cash=1_000.0),
            (intent,),
            self._market("market-2", NOW + timedelta(minutes=5), "S", 100.0, volume=2_000),
            policy,
        )
        second = execution.execute_intents(
            first.account,
            (intent,),
            self._market("market-3", NOW + timedelta(minutes=10), "S", 100.0, volume=18_000),
            policy,
            prior_progress=first.progress,
        )

        self.assertEqual(first.fills[0].quantity, 100)
        self.assertEqual(first.progress[0].filled_quantity, 100)
        self.assertEqual(second.fills[0].quantity, 900)
        self.assertEqual(second.fills[0].status, "FILLED")
        self.assertEqual(second.progress[0].filled_quantity, 1_000)
        self.assertEqual(second.account.positions[0].quantity, 1_000)
        self.assertEqual(second.account.restricted_short_proceeds, 100_000.0)

    def test_partial_commission_minimum_is_charged_once_across_snapshots(self):
        policy = replace(
            self._zero_cost_policy("cn", participation=5.0),
            minimum_commission=5.0,
        )
        intent = execution.intent_for_delta(
            None,
            target("600001", PositionSide.LONG, 10.0),
            target_quantity=200,
            created_snapshot_id="market-1",
        )
        first = execution.execute_intents(
            account(cash=10_000.0),
            (intent,),
            self._market("market-2", NOW + timedelta(minutes=5), "600001", 10.0, volume=2_000),
            policy,
        )
        second = execution.execute_intents(
            first.account,
            (intent,),
            self._market("market-3", NOW + timedelta(minutes=10), "600001", 10.0, volume=2_000),
            policy,
            prior_progress=first.progress,
        )

        self.assertEqual(first.fills[0].fees, 5.0)
        self.assertEqual(second.fills[0].fees, 0.0)
        self.assertEqual(second.progress[0].commission_charged, 5.0)

    def test_risk_full_close_resumes_for_long_and_short_across_many_partials(self):
        policy = self._zero_cost_policy("us", participation=100.0)
        for side in (PositionSide.LONG, PositionSide.SHORT):
            with self.subTest(side=side):
                original = account(
                    cash=0.0,
                    positions=(position("A", side, 10, 100.0),),
                    restricted_short_proceeds=(
                        1_000.0 if side is PositionSide.SHORT else 0.0
                    ),
                )
                intent = risk_close(
                    original.positions[0],
                    snapshot_id="market-1",
                    reason=(
                        "LONG_STOP_LOSS"
                        if side is PositionSide.LONG
                        else "SHORT_STOP_LOSS"
                    ),
                )

                current = original
                progress = ()
                observed_fills = []
                for index, volume in enumerate((4, 2, 99), start=2):
                    snapshot = self._market(
                        f"market-{index}",
                        NOW + timedelta(minutes=index * 5),
                        "A",
                        80.0,
                        volume=volume,
                    )
                    result = execution.execute_intents(
                        current,
                        (intent,),
                        snapshot,
                        policy,
                        prior_progress=progress,
                    )
                    self.assertEqual(result.diagnostics, ())
                    observed_fills.append(result.fills[0].quantity)
                    if index == 2:
                        replay = execution.execute_intents(
                            result.account,
                            (intent,),
                            snapshot,
                            policy,
                            prior_progress=result.progress,
                        )
                        self.assertEqual(replay.fills, ())
                        self.assertEqual(
                            replay.diagnostics[0].reason,
                            "SAME_SNAPSHOT",
                        )
                        self.assertEqual(replay.account, result.account)
                    current = result.account
                    progress = result.progress

                self.assertEqual(observed_fills, [4, 2, 4])
                self.assertEqual(current.positions, ())
                self.assertEqual(progress[0].filled_quantity, 10)
                self.assertEqual(progress[0].status, "FILLED")

    def test_tampered_partial_progress_cannot_relax_risk_intent_identity(self):
        held = position("A", PositionSide.SHORT, 10, 100.0)
        original = account(
            cash=0.0,
            positions=(held,),
            restricted_short_proceeds=1_000.0,
        )
        intent = risk_close(
            held,
            snapshot_id="market-1",
            reason="SHORT_STOP_LOSS",
        )
        policy = self._zero_cost_policy("us", participation=100.0)
        first = execution.execute_intents(
            original,
            (intent,),
            self._market(
                "market-2",
                NOW + timedelta(minutes=5),
                "A",
                80.0,
                volume=4,
            ),
            policy,
        )
        progress = first.progress[0]
        for label, updates in (
            ("intent_id", {"intent_id": "risk-forged"}),
            ("symbol", {"symbol": "B"}),
            ("direction", {"position_side": PositionSide.LONG}),
            ("average_cost", {"position_average_cost": 101.0}),
        ):
            with self.subTest(label=label), self.assertRaises(ValueError):
                execution.execute_intents(
                    first.account,
                    (intent,),
                    self._market(
                        "market-3",
                        NOW + timedelta(minutes=10),
                        "A",
                        80.0,
                        volume=6,
                    ),
                    policy,
                    prior_progress=(replace(progress, **updates),),
                )

        original_fill = progress.fills[0]
        underfilled_values = {
            "intent_id": original_fill.intent_id,
            "symbol": original_fill.symbol,
            "position_side": original_fill.position_side,
            "order_side": original_fill.order_side,
            "snapshot_id": original_fill.snapshot_id,
            "occurred_at": original_fill.occurred_at,
            "quantity": 3,
            "price": original_fill.price,
            "fees": original_fill.fees,
            "commission": original_fill.commission,
            "status": "PARTIAL",
        }
        underfilled = domain_contracts.ExecutionProgressFill(
            id=domain_contracts.stable_execution_progress_fill_id(
                **underfilled_values
            ),
            **underfilled_values,
        )
        with self.assertRaises(ValueError):
            execution.execute_intents(
                first.account,
                (intent,),
                self._market(
                    "market-3",
                    NOW + timedelta(minutes=10),
                    "A",
                    80.0,
                    volume=6,
                ),
                policy,
                prior_progress=(replace(progress, fills=(underfilled,)),),
            )

        overfilled_values = {**underfilled_values, "quantity": 11, "status": "FILLED"}
        overfilled = domain_contracts.ExecutionProgressFill(
            id=domain_contracts.stable_execution_progress_fill_id(
                **overfilled_values
            ),
            **overfilled_values,
        )
        with self.assertRaises(ValueError):
            replace(progress, fills=(overfilled,))

    def test_progress_aggregates_are_derived_from_bound_fill_history(self):
        intent = execution.intent_for_delta(
            None,
            target("A"),
            target_quantity=10,
            created_snapshot_id="market-1",
        )
        first = execution.execute_intents(
            account(cash=2_000.0),
            (intent,),
            self._market(
                "market-2",
                NOW + timedelta(minutes=5),
                "A",
                100.0,
                volume=4,
            ),
            self._zero_cost_policy("us", participation=100.0),
        )
        progress = first.progress[0]

        self.assertEqual(progress.filled_quantity, 4)
        self.assertEqual(progress.filled_notional, 400.0)
        self.assertEqual(progress.commission_charged, 0.0)
        progress_values = {
            "intent_id": intent.id,
            "symbol": "A",
            "position_side": PositionSide.LONG,
            "order_side": OrderSide.BUY,
            "intent_quantity": 10,
            "execution_policy_fingerprint": progress.execution_policy_fingerprint,
            "fills": progress.fills,
        }
        for aggregate_name in ("commission_charged", "filled_notional"):
            with self.subTest(aggregate_name=aggregate_name), self.assertRaises(
                TypeError
            ):
                execution.OrderExecutionProgress(
                    **progress_values,
                    **{aggregate_name: 999.0},
                )
        for field_name, forged_value in (
            ("price", 999.0),
            ("fees", 999.0),
            ("commission", 999.0),
            ("status", "FILLED"),
        ):
            with self.subTest(field_name=field_name), self.assertRaises(ValueError):
                replace(
                    progress.fills[0],
                    **{field_name: forged_value},
                )

    def test_resigned_claimed_fees_cannot_waive_partial_commission(self):
        policy = replace(
            self._zero_cost_policy("us", participation=100.0),
            minimum_commission=5.0,
        )
        intent = execution.intent_for_delta(
            None,
            target("A"),
            target_quantity=10,
            created_snapshot_id="market-1",
        )
        first = execution.execute_intents(
            account(cash=2_000.0),
            (intent,),
            self._market(
                "market-2",
                NOW + timedelta(minutes=5),
                "A",
                100.0,
                volume=4,
            ),
            policy,
        )
        original_fill = first.progress[0].fills[0]
        forged_values = {
            "intent_id": original_fill.intent_id,
            "symbol": original_fill.symbol,
            "position_side": original_fill.position_side,
            "order_side": original_fill.order_side,
            "snapshot_id": original_fill.snapshot_id,
            "occurred_at": original_fill.occurred_at,
            "quantity": original_fill.quantity,
            "price": original_fill.price,
            "fees": 999.0,
            "commission": 999.0,
            "status": original_fill.status,
        }
        forged_fill = domain_contracts.ExecutionProgressFill(
            id=domain_contracts.stable_execution_progress_fill_id(
                **forged_values
            ),
            **forged_values,
        )
        forged_progress = replace(first.progress[0], fills=(forged_fill,))

        with self.assertRaisesRegex(ValueError, "commission|fees"):
            execution.execute_intents(
                first.account,
                (intent,),
                self._market(
                    "market-3",
                    NOW + timedelta(minutes=10),
                    "A",
                    100.0,
                    volume=6,
                ),
                policy,
                prior_progress=(forged_progress,),
            )

    def test_partial_progress_rejects_execution_policy_change(self):
        policy = replace(
            self._zero_cost_policy("us", participation=100.0),
            minimum_commission=5.0,
        )
        intent = execution.intent_for_delta(
            None,
            target("A"),
            target_quantity=10,
            created_snapshot_id="market-1",
        )
        first = execution.execute_intents(
            account(cash=2_000.0),
            (intent,),
            self._market("market-2", NOW, "A", 100.0, volume=4),
            policy,
        )
        changed = replace(policy, commission_rate_pct=1.0)

        with self.assertRaisesRegex(ValueError, "policy"):
            execution.execute_intents(
                first.account,
                (intent,),
                self._market(
                    "market-3",
                    NOW + timedelta(minutes=5),
                    "A",
                    100.0,
                    volume=6,
                ),
                changed,
                prior_progress=first.progress,
            )

    def test_execution_rotates_carry_lifecycles_only_after_full_exit(self):
        policy = self._zero_cost_policy("us", participation=100.0)

        open_short = execution.intent_for_delta(
            None,
            target("S", PositionSide.SHORT),
            target_quantity=10,
            created_snapshot_id="market-1",
        )
        short_opened = execution.execute_intents(
            account(cash=0.0),
            (open_short,),
            self._market("market-2", NOW, "S", 100.0),
            policy,
        )
        first_borrow_lifecycle = short_opened.account.positions[0].borrow_lifecycle
        close_short = risk_close(
            short_opened.account.positions[0],
            snapshot_id="market-2",
            reason="SHORT_STOP_LOSS",
        )
        short_partial = execution.execute_intents(
            short_opened.account,
            (close_short,),
            self._market("market-3", NOW + timedelta(minutes=5), "S", 90.0, volume=4),
            policy,
        )
        self.assertEqual(
            short_partial.account.positions[0].borrow_lifecycle,
            first_borrow_lifecycle,
        )
        short_closed = execution.execute_intents(
            short_partial.account,
            (close_short,),
            self._market("market-4", NOW + timedelta(minutes=10), "S", 90.0, volume=6),
            policy,
            prior_progress=short_partial.progress,
        )
        short_reopened = execution.execute_intents(
            short_closed.account,
            (
                execution.intent_for_delta(
                    None,
                    target("S", PositionSide.SHORT),
                    target_quantity=10,
                    created_snapshot_id="market-4",
                ),
            ),
            self._market("market-5", NOW + timedelta(minutes=15), "S", 90.0),
            policy,
        )
        self.assertNotEqual(
            short_reopened.account.positions[0].borrow_lifecycle.id,
            first_borrow_lifecycle.id,
        )

        open_long = execution.intent_for_delta(
            None,
            target("L"),
            target_quantity=10,
            created_snapshot_id="market-1",
        )
        long_opened = execution.execute_intents(
            account(cash=0.0),
            (open_long,),
            self._market("market-2", NOW, "L", 100.0),
            policy,
        )
        first_financing_lifecycle = long_opened.account.financing_lifecycle
        close_long = risk_close(
            long_opened.account.positions[0],
            snapshot_id="market-2",
            reason="LONG_STOP_LOSS",
        )
        long_partial = execution.execute_intents(
            long_opened.account,
            (close_long,),
            self._market("market-3", NOW + timedelta(minutes=5), "L", 100.0, volume=4),
            policy,
        )
        self.assertEqual(
            long_partial.account.financing_lifecycle,
            first_financing_lifecycle,
        )
        long_closed = execution.execute_intents(
            long_partial.account,
            (close_long,),
            self._market("market-4", NOW + timedelta(minutes=10), "L", 100.0, volume=6),
            policy,
            prior_progress=long_partial.progress,
        )
        self.assertIsNone(long_closed.account.financing_lifecycle)
        long_reopened = execution.execute_intents(
            long_closed.account,
            (
                execution.intent_for_delta(
                    None,
                    target("L"),
                    target_quantity=10,
                    created_snapshot_id="market-4",
                ),
            ),
            self._market("market-5", NOW + timedelta(minutes=15), "L", 100.0),
            policy,
        )
        self.assertNotEqual(
            long_reopened.account.financing_lifecycle.id,
            first_financing_lifecycle.id,
        )

    def test_forged_intent_is_locally_rejected_by_batch_execution(self):
        valid = execution.intent_for_delta(
            None, target("AAPL"), target_quantity=1, created_snapshot_id="market-1"
        )
        forged = replace(valid, reason="FORGED")

        result = execution.execute_intents(
            account(),
            (forged,),
            self._market("market-2", NOW + timedelta(minutes=5), "AAPL", 100.0),
            self._zero_cost_policy(),
        )

        self.assertEqual(result.fills, ())
        self.assertEqual(result.diagnostics[0].reason, "INVALID_INTENT_ID")

    def test_genuine_risk_intent_passes_stable_id_verification(self):
        held = position("A", quantity=10, average_cost=100.0, current_price=92.0)
        risk_intent = evaluate_position_risk(
            held,
            snapshot_id="market-1",
        ).intents[0]
        quote = {"price": 92.0, "bar_volume": 1_000}

        direct = execution.simulate_fill(
            risk_intent,
            quote,
            self._zero_cost_policy(),
            current_snapshot_id="market-2",
            existing_position=held,
        )
        batch = execution.execute_intents(
            account(positions=(held,)),
            (risk_intent,),
            self._market("market-2", NOW + timedelta(minutes=5), "A", 92.0),
            self._zero_cost_policy(),
        )

        self.assertIsNotNone(direct)
        self.assertEqual(batch.fills[0].intent_id, risk_intent.id)
        forged = replace(risk_intent, quantity=risk_intent.quantity - 1)
        self.assertIsNone(
            execution.simulate_fill(
                forged,
                quote,
                self._zero_cost_policy(),
                current_snapshot_id="market-2",
                existing_position=held,
            )
        )

    def test_same_batch_close_then_open_reversal_executes_only_close_locally(self):
        held = position("A", quantity=10, average_cost=100.0, current_price=92.0)
        close_a = evaluate_position_risk(held, snapshot_id="market-1").intents[0]
        open_a_short = execution.intent_for_delta(
            None,
            target("A", PositionSide.SHORT, 5.0),
            target_quantity=5,
            created_snapshot_id="market-1",
        )
        open_b = execution.intent_for_delta(
            None,
            target("B"),
            target_quantity=1,
            created_snapshot_id="market-1",
        )
        market = MarketSnapshot(
            id="market-2",
            occurred_at=NOW + timedelta(minutes=5),
            quotes={
                "A": {"price": 92.0, "bar_volume": 1_000},
                "B": {"price": 50.0, "bar_volume": 1_000},
            },
        )

        result = execution.execute_intents(
            account(positions=(held,)),
            (open_a_short, open_b, close_a),
            market,
            self._zero_cost_policy(),
        )

        self.assertEqual({item.intent_id for item in result.fills}, {close_a.id, open_b.id})
        self.assertEqual(
            next(
                item.reason
                for item in result.diagnostics
                if item.intent_id == open_a_short.id
            ),
            "SAME_BATCH_REVERSAL_BLOCKED",
        )
        self.assertEqual(
            [(item.symbol, item.side) for item in result.account.positions],
            [("B", PositionSide.LONG)],
        )

    def test_execution_progress_is_contract_owned_and_decision_batch_safe(self):
        fill_values = {
            "intent_id": "intent-1",
            "symbol": "AAPL",
            "position_side": PositionSide.LONG,
            "order_side": OrderSide.BUY,
            "snapshot_id": "market-2",
            "occurred_at": NOW,
            "quantity": 1,
            "price": 100.0,
            "fees": 0.0,
            "commission": 0.0,
            "status": "FILLED",
        }
        progress_fill = domain_contracts.ExecutionProgressFill(
            id=domain_contracts.stable_execution_progress_fill_id(**fill_values),
            **fill_values,
        )
        progress = execution.OrderExecutionProgress(
            intent_id="intent-1",
            symbol="AAPL",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            intent_quantity=1,
            execution_policy_fingerprint=execution.execution_policy_fingerprint(
                self._zero_cost_policy()
            ),
            fills=(progress_fill,),
        )
        self.assertIs(type(progress), domain_contracts.OrderExecutionProgress)
        self.assertIs(copy.deepcopy(progress), progress)

        intent = execution.intent_for_delta(
            None, target("AAPL"), target_quantity=1, created_snapshot_id="market-1"
        )
        stage = execution.ExecutionSimulationStage(
            account(),
            self._market("market-2", NOW + timedelta(minutes=5), "AAPL", 100.0),
            self._zero_cost_policy(),
        )
        output = stage.evaluate(
            StageInput(
                run_id="run-1",
                strategy_id="strategy-1",
                strategy_version=2,
                as_of=NOW.isoformat(),
                market_snapshot_id="market-2",
                portfolio_snapshot_id="account-1",
                upstream_facts=(
                    {"kind": "margin_admitted_intents", "items": (intent,)},
                ),
            )
        )
        batch = domain_contracts.DecisionBatch(
            run_key="run-1",
            strategy_id="strategy-1",
            strategy_revision=2,
            portfolio_snapshot_id="account-1",
            market_snapshot_id="market-2",
            stage_outputs=(output,),
        )

        stored = batch.stage_outputs[0].facts[2]["items"][0]
        self.assertIs(type(stored), domain_contracts.OrderExecutionProgress)
        self.assertIs(copy.deepcopy(batch), batch)


class CarryAccrualTests(unittest.TestCase):
    def _leveraged_short_account(self):
        return account(
            cash=100.0,
            occurred_at=datetime(2026, 7, 30, 14, 30, tzinfo=timezone.utc),
            margin_loan=3_650.0,
            financing_lifecycle=accrual_lifecycle("financing-main"),
            restricted_short_proceeds=1_000.0,
            positions=(
                position(
                    "S",
                    PositionSide.SHORT,
                    10,
                    100.0,
                    current_price=100.0,
                    borrow_lifecycle=accrual_lifecycle("borrow-s"),
                ),
            ),
        )

    def test_daily_financing_and_borrow_accrual_is_idempotent(self):
        original = self._leveraged_short_account()
        before = copy.deepcopy(original)

        first = execution.accrue_carry_costs(
            original,
            as_of=date(2026, 7, 31),
            financing_apr_pct=10.0,
            borrow_apr_by_symbol={"S": 36.5},
        )
        repeated = execution.accrue_carry_costs(
            first.account,
            as_of=date(2026, 7, 31),
            financing_apr_pct=10.0,
            borrow_apr_by_symbol={"S": 36.5},
        )

        self.assertEqual(original, before)
        self.assertEqual(first.financing_cost, 1.0)
        self.assertEqual(first.borrow_cost, 1.0)
        self.assertEqual(first.account.available_cash, 98.0)
        self.assertEqual(first.account.accrued_financing_cost, 1.0)
        self.assertEqual(first.account.accrued_borrow_cost, 1.0)
        self.assertEqual(
            [event.type for event in first.events],
            ["FINANCING_COST_ACCRUED", "BORROW_COST_ACCRUED"],
        )
        self.assertEqual(repeated.financing_cost + repeated.borrow_cost, 0.0)
        self.assertEqual(repeated.events, ())
        self.assertEqual(repeated.account, first.account)

    def test_reopened_borrow_and_financing_ignore_old_lifecycle_records(self):
        old_borrow = domain_contracts.CarryAccrualRecord(
            account_id="account-1",
            cost_type=domain_contracts.CarryCostType.BORROW,
            accrual_date=date(2026, 7, 1),
            elapsed_days=1,
            amount=1.0,
            symbol="S",
            lifecycle_id="borrow-old",
        )
        old_financing = domain_contracts.CarryAccrualRecord(
            account_id="account-1",
            cost_type=domain_contracts.CarryCostType.FINANCING,
            accrual_date=date(2026, 7, 1),
            elapsed_days=1,
            amount=1.0,
            lifecycle_id="financing-old",
        )
        reopened = account(
            cash=100.0,
            occurred_at=datetime(2026, 7, 10, 14, 30, tzinfo=timezone.utc),
            margin_loan=3_650.0,
            financing_lifecycle=domain_contracts.AccrualLifecycle(
                id="financing-new",
                started_on=date(2026, 7, 10),
            ),
            restricted_short_proceeds=1_000.0,
            positions=(
                position(
                    "S",
                    PositionSide.SHORT,
                    10,
                    100.0,
                    current_price=100.0,
                    borrow_lifecycle=domain_contracts.AccrualLifecycle(
                        id="borrow-new",
                        started_on=date(2026, 7, 10),
                    ),
                ),
            ),
            carry_accruals=(old_borrow, old_financing),
        )

        result = execution.accrue_carry_costs(
            reopened,
            as_of=date(2026, 7, 11),
            financing_apr_pct=10.0,
            borrow_apr_by_symbol={"S": 36.5},
        )

        self.assertEqual(result.financing_cost, 1.0)
        self.assertEqual(result.borrow_cost, 1.0)
        self.assertEqual(
            {(item.cost_type.value, item.elapsed_days) for item in result.new_accruals},
            {("FINANCING", 1), ("BORROW", 1)},
        )

    def test_weekend_uses_actual_three_calendar_days_over_365(self):
        friday = execution.accrue_carry_costs(
            self._leveraged_short_account(),
            as_of=date(2026, 7, 31),
            financing_apr_pct=10.0,
            borrow_apr_by_symbol={"S": 36.5},
        )
        monday = execution.accrue_carry_costs(
            friday.account,
            as_of=date(2026, 8, 3),
            financing_apr_pct=10.0,
            borrow_apr_by_symbol={"S": 36.5},
        )

        self.assertEqual(monday.financing_cost, 3.0)
        self.assertEqual(monday.borrow_cost, 3.0)
        self.assertEqual(
            sorted(record.elapsed_days for record in monday.new_accruals),
            [3, 3],
        )

    def test_borrow_cost_uses_each_short_liability_and_estimated_fallback(self):
        original = replace(
            self._leveraged_short_account(),
            positions=(
                position(
                    "A",
                    PositionSide.SHORT,
                    10,
                    100.0,
                    current_price=100.0,
                    borrow_lifecycle=accrual_lifecycle("borrow-a"),
                ),
                position(
                    "B",
                    PositionSide.SHORT,
                    20,
                    50.0,
                    current_price=50.0,
                    borrow_lifecycle=accrual_lifecycle("borrow-b"),
                ),
            ),
            restricted_short_proceeds=2_000.0,
            margin_loan=0.0,
            financing_lifecycle=None,
        )

        result = execution.accrue_carry_costs(
            original,
            as_of=date(2026, 7, 31),
            borrow_apr_by_symbol={"A": 36.5},
            estimated_borrow_apr_pct=18.25,
        )

        self.assertEqual(result.financing_cost, 0.0)
        self.assertEqual(result.borrow_cost, 1.5)

    def test_missing_short_price_degrades_per_symbol_without_blocking_other_costs(self):
        mixed = replace(
            self._leveraged_short_account(),
            positions=(
                position(
                    "A",
                    PositionSide.SHORT,
                    10,
                    100.0,
                    current_price=100.0,
                    borrow_lifecycle=accrual_lifecycle("borrow-a"),
                ),
                position(
                    "B",
                    PositionSide.SHORT,
                    10,
                    50.0,
                    borrow_lifecycle=accrual_lifecycle("borrow-b"),
                ),
            ),
            restricted_short_proceeds=1_500.0,
        )
        first = execution.accrue_carry_costs(
            mixed,
            as_of=date(2026, 7, 31),
            financing_apr_pct=10.0,
            borrow_apr_by_symbol={"A": 36.5, "B": 36.5},
        )

        self.assertEqual(first.financing_cost, 1.0)
        self.assertEqual(first.borrow_cost, 1.0)
        self.assertEqual(
            [(item.symbol, item.reason) for item in first.diagnostics],
            [("B", "SHORT_PRICE_MISSING")],
        )
        self.assertEqual(
            {(item.cost_type.value, item.symbol) for item in first.new_accruals},
            {("FINANCING", None), ("BORROW", "A")},
        )

        recovered = execution.accrue_carry_costs(
            first.account,
            as_of=date(2026, 7, 31),
            prices={"B": 50.0},
            financing_apr_pct=10.0,
            borrow_apr_by_symbol={"A": 36.5, "B": 36.5},
        )
        self.assertEqual(recovered.financing_cost, 0.0)
        self.assertEqual(recovered.borrow_cost, 0.5)
        self.assertEqual(
            [
                (item.cost_type.value, item.symbol)
                for item in recovered.new_accruals
            ],
            [("BORROW", "B")],
        )

    def test_invalid_cost_inputs_fail_closed(self):
        for invalid in (math.nan, math.inf, -1.0, True):
            with self.subTest(invalid=invalid), self.assertRaises((TypeError, ValueError)):
                execution.accrue_carry_costs(
                    self._leveraged_short_account(),
                    as_of=date(2026, 7, 31),
                    financing_apr_pct=invalid,
                )


class RebalancePlanningContractTests(unittest.TestCase):
    @staticmethod
    def _market(quotes):
        return MarketSnapshot(id="market-1", occurred_at=NOW, quotes=quotes)

    def test_risk_close_overrides_rebalance_and_keeps_original_semantics(self):
        held = position("A", side=PositionSide.SHORT, quantity=10)
        risk = risk_close(
            held,
            snapshot_id="market-1",
            reason="MARGIN_CALL",
        )
        policy = execution.execution_policy("us", default_portfolio_config())

        result = execution.plan_rebalance_intents(
            account(positions=(held,)),
            (target("A", PositionSide.SHORT, 5.0),),
            self._market({"A": {"price": 100.0, "bar_volume": 1_000}}),
            policy,
            account_equity=10_000.0,
            risk_intents=(risk,),
        )

        self.assertEqual(result.intents, (risk,))
        self.assertEqual(result.intents[0].semantic_tuple(), ("SHORT", "BUY", "CLOSE"))

    def test_stage_emits_typed_order_intents_without_mutating_account(self):
        original = account()
        before = copy.deepcopy(original)
        policy = execution.execution_policy("us", default_portfolio_config())
        stage = execution.RebalanceIntentStage(
            original,
            self._market({"AAPL": {"price": 100.0, "bar_volume": 1_000}}),
            policy,
            account_equity=10_000.0,
        )
        outputs = PipelineRunner((stage,)).run(
            StageInput(
                run_id="run-1",
                strategy_id="strategy-1",
                strategy_version=2,
                as_of=NOW.isoformat(),
                market_snapshot_id="market-1",
                portfolio_snapshot_id="account-1",
                upstream_facts=(
                    {"kind": "borrow_targets", "items": (target(),)},
                    {"kind": "risk_intents", "items": ()},
                ),
            )
        )

        self.assertEqual(outputs[0].facts[0]["kind"], "order_intents")
        self.assertEqual(outputs[0].facts[0]["items"][0].semantic_tuple(), ("LONG", "BUY", "OPEN"))
        self.assertEqual(original, before)

    def test_stage_rejects_duplicate_target_facts(self):
        policy = execution.execution_policy("us", default_portfolio_config())
        stage = execution.RebalanceIntentStage(
            account(), self._market({}), policy, account_equity=1_000.0
        )
        with self.assertRaises(PipelineContractError):
            stage.evaluate(
                StageInput(
                    run_id="run-1",
                    strategy_id="strategy-1",
                    strategy_version=2,
                    as_of=NOW.isoformat(),
                    market_snapshot_id="market-1",
                    portfolio_snapshot_id="account-1",
                    upstream_facts=(
                        {"kind": "borrow_targets", "items": ()},
                        {"kind": "borrow_targets", "items": ()},
                    ),
                )
            )


if __name__ == "__main__":
    unittest.main()
