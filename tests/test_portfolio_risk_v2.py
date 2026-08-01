import math
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

from stock_recommender.pipeline import PipelineContractError, PipelineRunner, StageInput
from stock_recommender.portfolio_engine.config import MarginPolicy, ShortPolicy
from stock_recommender.portfolio_engine.contracts import (
    AccountSnapshot,
    MarketSnapshot,
    OrderIntent,
    OrderSide,
    PositionEffect,
    PositionSide,
    PositionSnapshot,
)
from stock_recommender.portfolio_engine.risk import (
    COVER_ONLY,
    DERISK,
    INSOLVENT_HALT,
    MANUAL_HALT,
    MARGIN_CALL,
    NORMAL,
    WARNING,
    PortfolioRiskStage,
    RiskDecision,
    RiskError,
    default_policies,
    evaluate_forced_deleveraging,
    evaluate_portfolio_drawdown,
    evaluate_position_risk,
    evaluate_squeeze,
    plan_forced_deleveraging,
)
from stock_recommender.portfolio_engine.valuation import value_account


NOW = datetime(2026, 7, 31, 8, tzinfo=timezone.utc)


def position(
    symbol="XYZ",
    side=PositionSide.LONG,
    *,
    entry=100.0,
    current=100.0,
    quantity=10,
    peak=None,
    trough=None,
    trailing_active=False,
    position_mode=NORMAL,
):
    return PositionSnapshot(
        symbol=symbol,
        side=side,
        quantity=quantity,
        average_cost=entry,
        current_price=current,
        peak_price=peak,
        trough_price=trough,
        trailing_active=trailing_active,
        position_mode=position_mode,
    )


def account(*positions, cash=0.0, loan=0.0, restricted=0.0, snapshot_id="acct-1"):
    return AccountSnapshot(
        id=snapshot_id,
        strategy_id="strategy-1",
        strategy_revision=3,
        occurred_at=NOW,
        available_cash=cash,
        restricted_short_proceeds=restricted,
        margin_loan=loan,
        positions=tuple(positions),
    )


def stage_input(*facts):
    return StageInput(
        run_id="run-1",
        strategy_id="strategy-1",
        strategy_version=3,
        as_of=NOW.isoformat(),
        market_snapshot_id="market-1",
        portfolio_snapshot_id="acct-1",
        upstream_facts=tuple(facts),
    )


class DirectionAwarePositionRiskTests(unittest.TestCase):
    def test_short_stop_loss_closes_full_position_with_buy(self):
        result = evaluate_position_risk(
            position(side=PositionSide.SHORT, current=106.1),
            default_policies(),
            snapshot_id="snapshot-7",
        )

        decision = result[0]
        self.assertEqual(decision.reason, "SHORT_STOP_LOSS")
        self.assertEqual(decision.position_effect, PositionEffect.CLOSE)
        self.assertEqual(decision.intent.order_side, OrderSide.BUY)
        self.assertEqual(decision.intent.position_side, PositionSide.SHORT)
        self.assertEqual(decision.intent.quantity, 10)
        self.assertEqual(decision.intent.created_snapshot_id, "snapshot-7")
        self.assertFalse(decision.intent.increases_risk)

    def test_long_stop_loss_closes_full_position_with_sell(self):
        decision = evaluate_position_risk(position(current=91.9))[0]

        self.assertEqual(decision.reason, "LONG_STOP_LOSS")
        self.assertEqual(decision.intent.order_side, OrderSide.SELL)
        self.assertEqual(decision.intent.position_side, PositionSide.LONG)
        self.assertEqual(decision.intent.position_effect, PositionEffect.CLOSE)

    def test_short_trailing_stop_is_inclusive_at_four_percent_rebound(self):
        exact = position(
            side=PositionSide.SHORT,
            current=83.2,
            trough=80.0,
            trailing_active=True,
        )
        below = position(
            side=PositionSide.SHORT,
            current=math.nextafter(83.2, -math.inf),
            trough=80.0,
            trailing_active=True,
        )

        self.assertEqual(evaluate_position_risk(exact)[0].reason, "SHORT_TRAILING_STOP")
        self.assertEqual(evaluate_position_risk(below).intents, ())

    def test_long_trailing_stop_is_inclusive_at_five_percent_drawdown(self):
        exact = position(current=114.0, peak=120.0, trailing_active=True)
        above = position(
            current=math.nextafter(114.0, math.inf),
            peak=120.0,
            trailing_active=True,
        )

        self.assertEqual(evaluate_position_risk(exact)[0].reason, "LONG_TRAILING_STOP")
        self.assertEqual(evaluate_position_risk(above).intents, ())

    def test_fixed_stop_thresholds_are_inclusive_and_nextafter_safe(self):
        long_exact = evaluate_position_risk(position(current=92.0))
        long_above = evaluate_position_risk(
            position(current=math.nextafter(92.0, math.inf))
        )
        short_exact = evaluate_position_risk(
            position(side=PositionSide.SHORT, current=106.0)
        )
        short_below = evaluate_position_risk(
            position(
                side=PositionSide.SHORT,
                current=math.nextafter(106.0, -math.inf),
            )
        )

        self.assertEqual(long_exact[0].reason, "LONG_STOP_LOSS")
        self.assertEqual(long_above.intents, ())
        self.assertEqual(short_exact[0].reason, "SHORT_STOP_LOSS")
        self.assertEqual(short_below.intents, ())

    def test_activation_and_anchor_updates_return_new_immutable_position(self):
        original_long = position(current=110.0, peak=105.0)
        original_short = position(
            side=PositionSide.SHORT,
            current=92.0,
            trough=95.0,
        )

        long_result = evaluate_position_risk(original_long)
        short_result = evaluate_position_risk(original_short)

        self.assertEqual(long_result.intents, ())
        self.assertTrue(long_result.updated_position.trailing_active)
        self.assertEqual(long_result.updated_position.peak_price, 110.0)
        self.assertFalse(original_long.trailing_active)
        self.assertEqual(original_long.peak_price, 105.0)
        self.assertEqual(short_result.intents, ())
        self.assertTrue(short_result.updated_position.trailing_active)
        self.assertEqual(short_result.updated_position.trough_price, 92.0)
        with self.assertRaises(FrozenInstanceError):
            long_result.updated_position.trailing_active = False

    def test_anchors_update_before_activation_without_an_exit(self):
        long_result = evaluate_position_risk(position(current=105.0, peak=102.0))
        short_result = evaluate_position_risk(
            position(side=PositionSide.SHORT, current=95.0, trough=97.0)
        )

        self.assertEqual(long_result.updated_position.peak_price, 105.0)
        self.assertFalse(long_result.updated_position.trailing_active)
        self.assertEqual(short_result.updated_position.trough_price, 95.0)
        self.assertFalse(short_result.updated_position.trailing_active)
        self.assertEqual(long_result.decisions, ())
        self.assertEqual(short_result.decisions, ())

    def test_fixed_stop_has_priority_and_only_one_close_is_emitted(self):
        long_result = evaluate_position_risk(
            position(current=91.0, peak=120.0, trailing_active=True)
        )
        short_result = evaluate_position_risk(
            position(
                side=PositionSide.SHORT,
                current=107.0,
                trough=80.0,
                trailing_active=True,
            )
        )

        self.assertEqual([item.reason for item in long_result], ["LONG_STOP_LOSS"])
        self.assertEqual([item.reason for item in short_result], ["SHORT_STOP_LOSS"])
        self.assertEqual(len(long_result.intents), 1)
        self.assertEqual(len(short_result.intents), 1)

    def test_cover_only_does_not_mean_immediate_liquidation(self):
        result = evaluate_position_risk(
            position(
                side=PositionSide.SHORT,
                current=100.0,
                position_mode=COVER_ONLY,
            )
        )

        self.assertEqual(result.position_mode, COVER_ONLY)
        self.assertEqual(result.intents, ())

    def test_intent_ids_are_deterministic_and_snapshot_scoped(self):
        held = position(side=PositionSide.SHORT, current=106.1)

        first = evaluate_position_risk(held, snapshot_id="snapshot-1").intents[0]
        second = evaluate_position_risk(held, snapshot_id="snapshot-1").intents[0]
        next_snapshot = evaluate_position_risk(
            held, snapshot_id="snapshot-2"
        ).intents[0]

        self.assertEqual(first.id, second.id)
        self.assertNotEqual(first.id, next_snapshot.id)

    def test_missing_current_price_raises_risk_error(self):
        with self.assertRaisesRegex(RiskError, "current_price"):
            evaluate_position_risk(position(current=None))

    def test_active_long_trailing_without_peak_anchor_fails_closed(self):
        held = position(current=120.0, trailing_active=True, peak=None)

        with self.assertRaisesRegex(RiskError, "peak_price"):
            evaluate_position_risk(held)

    def test_active_short_trailing_without_trough_anchor_fails_closed(self):
        held = position(
            side=PositionSide.SHORT,
            current=80.0,
            trailing_active=True,
            trough=None,
        )

        with self.assertRaisesRegex(RiskError, "trough_price"):
            evaluate_position_risk(held)

    def test_risk_decision_rejects_a_forged_reason(self):
        held = position(current=92.0)
        forged_intent = OrderIntent(
            id="forged-1",
            symbol=held.symbol,
            position_side=held.side,
            order_side=OrderSide.SELL,
            position_effect=PositionEffect.CLOSE,
            quantity=held.quantity,
            reason="NOT_THE_DECISION_REASON",
            created_snapshot_id="snapshot-1",
        )

        with self.assertRaises(ValueError):
            RiskDecision(
                reason="LONG_STOP_LOSS",
                position_effect=PositionEffect.CLOSE,
                position_mode=NORMAL,
                updated_position=held,
                intent=forged_intent,
            )

    def test_risk_decision_rejects_wrong_close_side_for_long_and_short(self):
        cases = (
            (position(), OrderSide.BUY),
            (position(side=PositionSide.SHORT), OrderSide.SELL),
        )

        for held, wrong_order_side in cases:
            with self.subTest(side=held.side), self.assertRaises(ValueError):
                intent = OrderIntent(
                    id=f"wrong-side-{held.side.value}",
                    symbol=held.symbol,
                    position_side=held.side,
                    order_side=wrong_order_side,
                    position_effect=PositionEffect.CLOSE,
                    quantity=held.quantity,
                    reason="RISK_CLOSE",
                    created_snapshot_id="snapshot-1",
                )
                RiskDecision(
                    reason="RISK_CLOSE",
                    position_effect=PositionEffect.CLOSE,
                    position_mode=NORMAL,
                    updated_position=held,
                    intent=intent,
                )

    def test_risk_decision_rejects_non_close_intent(self):
        held = position(quantity=10)
        forged_intent = OrderIntent(
            id="forged-reduce",
            symbol=held.symbol,
            position_side=held.side,
            order_side=OrderSide.SELL,
            position_effect=PositionEffect.REDUCE,
            quantity=held.quantity,
            reason="RISK_REDUCE",
            created_snapshot_id="snapshot-1",
        )

        with self.assertRaises(ValueError):
            RiskDecision(
                reason="RISK_REDUCE",
                position_effect=PositionEffect.REDUCE,
                position_mode=NORMAL,
                updated_position=held,
                intent=forged_intent,
            )

    def test_risk_decision_rejects_forged_intent_identity(self):
        held = position()
        forged_intent = OrderIntent(
            id="forged-identity",
            symbol=held.symbol,
            position_side=held.side,
            order_side=OrderSide.SELL,
            position_effect=PositionEffect.CLOSE,
            quantity=held.quantity,
            reason="RISK_CLOSE",
            created_snapshot_id="snapshot-1",
        )

        with self.assertRaises(ValueError):
            RiskDecision(
                reason="RISK_CLOSE",
                position_effect=PositionEffect.CLOSE,
                position_mode=NORMAL,
                updated_position=held,
                intent=forged_intent,
            )


class SqueezeRiskTests(unittest.TestCase):
    def test_exact_squeeze_threshold_sets_cover_only_for_short(self):
        decision = evaluate_squeeze(
            position(side=PositionSide.SHORT),
            {"daily_rise_pct": 10.0, "volume_ratio": 3.0},
            ShortPolicy(),
        )

        self.assertEqual(decision.position_mode, COVER_ONLY)
        self.assertEqual(decision.reason, "SHORT_SQUEEZE")

    def test_either_squeeze_condition_below_threshold_stays_normal(self):
        held = position(side=PositionSide.SHORT)

        self.assertEqual(
            evaluate_squeeze(
                held,
                {"daily_rise_pct": math.nextafter(10.0, -math.inf), "volume_ratio": 3.0},
                ShortPolicy(),
            ).position_mode,
            NORMAL,
        )
        self.assertEqual(
            evaluate_squeeze(
                held,
                {"daily_rise_pct": 10.0, "volume_ratio": math.nextafter(3.0, -math.inf)},
                ShortPolicy(),
            ).position_mode,
            NORMAL,
        )

    def test_squeeze_does_not_restrict_longs_and_preserves_existing_mode(self):
        quote = {"daily_rise_pct": 99.0, "volume_ratio": 99.0}

        self.assertEqual(evaluate_squeeze(position(), quote).position_mode, NORMAL)
        restricted = position(
            side=PositionSide.SHORT,
            position_mode=COVER_ONLY,
        )
        self.assertEqual(
            evaluate_squeeze(
                restricted,
                {"daily_rise_pct": 0.0, "volume_ratio": 0.0},
            ).position_mode,
            COVER_ONLY,
        )

    def test_missing_squeeze_data_does_not_invent_a_signal(self):
        decision = evaluate_squeeze(position(side=PositionSide.SHORT), {})

        self.assertEqual(decision.position_mode, NORMAL)
        self.assertEqual(decision.reason, "SQUEEZE_DATA_INVALID")


class PortfolioDrawdownRiskTests(unittest.TestCase):
    @staticmethod
    def metrics(equity):
        return value_account(account(cash=equity), {}).metrics

    def test_exact_drawdown_thresholds(self):
        cases = (
            (88.0000001, NORMAL),
            (88.0, WARNING),
            (86.0, DERISK),
            (85.0, MANUAL_HALT),
        )

        for equity, expected in cases:
            with self.subTest(equity=equity):
                result = evaluate_portfolio_drawdown(self.metrics(equity), 100.0)
                self.assertEqual(result.state, expected)
                self.assertEqual(result.diagnostic["state"], expected)

    def test_small_equity_drawdown_thresholds_and_nextafter_are_exact(self):
        cases = (
            (0.0088, 12.0, WARNING, NORMAL),
            (0.0086, 14.0, DERISK, WARNING),
            (0.0085, 15.0, MANUAL_HALT, DERISK),
        )

        for equity, expected_pct, exact_state, safer_state in cases:
            with self.subTest(threshold=expected_pct):
                exact = evaluate_portfolio_drawdown(self.metrics(equity), 0.01)
                safer = evaluate_portfolio_drawdown(
                    self.metrics(math.nextafter(equity, math.inf)),
                    0.01,
                )
                breached = evaluate_portfolio_drawdown(
                    self.metrics(math.nextafter(equity, -math.inf)),
                    0.01,
                )

                self.assertEqual(exact.drawdown_pct, expected_pct)
                self.assertEqual(exact.state, exact_state)
                self.assertEqual(safer.state, safer_state)
                self.assertEqual(breached.state, exact_state)

    def test_equity_at_or_below_zero_has_insolvent_priority(self):
        for equity in (0.0, -1.0):
            with self.subTest(equity=equity):
                result = evaluate_portfolio_drawdown(self.metrics(equity), 0.0)
                self.assertEqual(result.state, INSOLVENT_HALT)

    def test_invalid_peak_equity_raises_for_solvent_account(self):
        for peak in (0.0, -1.0, math.inf, math.nan):
            with self.subTest(peak=peak), self.assertRaises(RiskError):
                evaluate_portfolio_drawdown(self.metrics(100.0), peak)


class ForcedDeleveragingTests(unittest.TestCase):
    def test_margin_call_returns_only_risk_reducing_full_closes(self):
        held = position(symbol="A", current=None, quantity=1)
        stressed = account(held, loan=70.001)

        intents = plan_forced_deleveraging(stressed, {"A": 100.0})

        self.assertTrue(intents)
        self.assertEqual(intents[0].reason, MARGIN_CALL)
        self.assertTrue(all(not item.increases_risk for item in intents))
        self.assertTrue(
            all(item.position_effect in {PositionEffect.REDUCE, PositionEffect.CLOSE} for item in intents)
        )

    def test_candidates_sort_by_margin_release_independent_of_input_order(self):
        positions = (
            position(symbol="B", current=None, quantity=2),
            position(symbol="C", current=None, quantity=3),
            position(symbol="A", current=None, quantity=1),
        )
        prices = {"A": 100.0, "B": 100.0, "C": 100.0}
        forward = account(*positions, loan=590.0)
        reverse = account(*reversed(positions), loan=590.0)

        forward_result = evaluate_forced_deleveraging(forward, prices)
        reverse_result = evaluate_forced_deleveraging(reverse, prices)

        self.assertEqual(
            [item.intent.symbol for item in forward_result.candidates],
            ["C", "B", "A"],
        )
        self.assertEqual(
            [item.symbol for item in forward_result.intents],
            [item.symbol for item in reverse_result.intents],
        )
        self.assertEqual(
            [item.id for item in forward_result.intents],
            [item.id for item in reverse_result.intents],
        )

    def test_revalues_after_each_close_and_stops_at_exact_buffer(self):
        stressed = account(
            position(symbol="L400", current=None, quantity=4),
            position(symbol="L300", current=None, quantity=3),
            position(symbol="L200", current=None, quantity=2),
            loan=700.0,
        )
        prices = {"L400": 100.0, "L300": 100.0, "L200": 100.0}

        result = evaluate_forced_deleveraging(stressed, prices)

        self.assertEqual([item.symbol for item in result.intents], ["L400"])
        self.assertEqual(result.final_margin_rate_pct, 40.0)

    def test_margin_thirty_is_reduce_only_not_forced_but_just_below_is_called(self):
        held = position(symbol="A", current=None, quantity=1)

        exact = evaluate_forced_deleveraging(account(held, loan=70.0), {"A": 100.0})
        below = evaluate_forced_deleveraging(
            account(held, loan=70.001), {"A": 100.0}
        )

        self.assertEqual(exact.intents, ())
        self.assertEqual(exact.state, "REDUCE_ONLY")
        self.assertEqual(below.state, MARGIN_CALL)
        self.assertTrue(below.intents)

    def test_small_decimal_margin_thirty_boundary_is_exact(self):
        held = position(
            symbol="MICRO",
            entry=0.03,
            current=None,
            quantity=1,
        )

        exact = evaluate_forced_deleveraging(
            account(held, loan=0.021),
            {"MICRO": 0.03},
        )
        breached = evaluate_forced_deleveraging(
            account(held, loan=math.nextafter(0.021, math.inf)),
            {"MICRO": 0.03},
        )

        self.assertEqual(exact.initial_margin_rate_pct, 30.0)
        self.assertEqual(exact.state, "REDUCE_ONLY")
        self.assertEqual(exact.intents, ())
        self.assertEqual(breached.state, MARGIN_CALL)

    def test_small_decimal_forced_close_stops_at_exact_forty(self):
        stressed = account(
            position(symbol="L3", entry=0.01, current=None, quantity=3),
            position(symbol="L2", entry=0.01, current=None, quantity=2),
            position(symbol="L1", entry=0.01, current=None, quantity=1),
            loan=0.048,
        )
        prices = {"L3": 0.01, "L2": 0.01, "L1": 0.01}

        result = evaluate_forced_deleveraging(stressed, prices)

        self.assertEqual([item.symbol for item in result.intents], ["L3"])
        self.assertEqual(result.final_margin_rate_pct, 40.0)

    def test_insolvent_account_returns_no_intents(self):
        stressed = account(
            position(symbol="A", current=None, quantity=1),
            loan=100.0,
        )

        result = evaluate_forced_deleveraging(stressed, {"A": 100.0})

        self.assertEqual(result.state, INSOLVENT_HALT)
        self.assertEqual(result.intents, ())

    def test_missing_price_skips_only_that_position_and_reports_diagnostic(self):
        priced = position(symbol="A", current=100.0, quantity=1)
        missing = position(symbol="B", current=300.0, quantity=1)
        stressed = account(priced, missing, loan=390.0)

        result = evaluate_forced_deleveraging(stressed, {"A": 100.0})

        self.assertTrue(result.intents)
        self.assertEqual([item.symbol for item in result.intents], ["A"])
        self.assertIn("B", result.missing_price_symbols)
        self.assertTrue(any(item["code"] == "MISSING_PRICE" for item in result.diagnostics))

    def test_forced_plan_is_deterministic_and_does_not_mutate_account(self):
        held = position(symbol="A", current=None, quantity=1)
        stressed = account(held, loan=70.001)
        original = deepcopy(stressed)

        first = plan_forced_deleveraging(stressed, {"A": 100.0})
        second = plan_forced_deleveraging(stressed, {"A": 100.0})

        self.assertEqual(first, second)
        self.assertEqual(stressed, original)


class PortfolioRiskStageTests(unittest.TestCase):
    def test_stage_merges_borrow_and_squeeze_modes_and_emits_plain_facts(self):
        short = position(
            symbol="S",
            side=PositionSide.SHORT,
            current=None,
        )
        snapshot = MarketSnapshot(
            id="market-1",
            occurred_at=NOW,
            quotes={
                "S": {"price": 100.0, "daily_rise_pct": 10.0, "volume_ratio": 3.0}
            },
        )
        stage = PortfolioRiskStage(
            account(short, cash=100.0, restricted=100.0),
            snapshot,
            peak_equity=100.0,
            borrow_position_modes={"S": NORMAL},
        )

        output = stage.evaluate(stage_input())
        facts = {fact["kind"]: fact for fact in output.facts}

        self.assertEqual(facts["position_modes"]["items"], {"S": COVER_ONLY})
        self.assertIn("risk_intents", facts)
        self.assertIn("risk_diagnostic", facts)
        self.assertTrue(all(type(fact) is dict for fact in output.facts))

    def test_stage_accepts_missing_position_modes_and_rejects_duplicates(self):
        stage = PortfolioRiskStage(account(cash=100.0), {}, peak_equity=100.0)

        stage.evaluate(stage_input())
        duplicate = stage_input(
            {"kind": "position_modes", "items": {}},
            {"kind": "position_modes", "items": {}},
        )
        with self.assertRaisesRegex(PipelineContractError, "duplicate"):
            stage.evaluate(duplicate)

    def test_pipeline_runner_can_deepcopy_risk_facts(self):
        stage = PortfolioRiskStage(account(cash=100.0), {}, peak_equity=100.0)

        outputs = PipelineRunner((stage,)).run(stage_input())

        self.assertEqual(outputs[0].stage, "portfolio_risk")


if __name__ == "__main__":
    unittest.main()
