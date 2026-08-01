import copy
import math
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from typing import get_type_hints

from stock_recommender.portfolio_engine import borrow, margin, ports
from stock_recommender.portfolio_engine.config import MarginPolicy, ShortPolicy
from stock_recommender.portfolio_engine.contracts import (
    AccountSnapshot,
    OrderIntent,
    OrderSide,
    PositionEffect,
    PositionSide,
    PositionSnapshot,
    TargetPosition,
)
from stock_recommender.pipeline import PipelineContractError, PipelineRunner, StageInput
from stock_recommender.portfolio_engine.valuation import ValuationError


NOW = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


def target(symbol, side):
    return TargetPosition(
        symbol=symbol,
        side=side,
        target_weight_pct=5.0,
        signal_score=0.9,
        model_id="model-v1",
        thesis_id=f"thesis-{symbol}",
    )


def account(*, cash, positions=(), **updates):
    values = dict(
        id="account-1",
        strategy_id="strategy-1",
        strategy_revision=1,
        occurred_at=NOW,
        available_cash=cash,
        positions=positions,
    )
    values.update(updates)
    return AccountSnapshot(**values)


def intent(
    symbol="L",
    *,
    position_side=PositionSide.LONG,
    position_effect=PositionEffect.OPEN,
    order_side=None,
    quantity=100,
):
    if order_side is None:
        order_side = (
            OrderSide.BUY
            if (
                position_side is PositionSide.LONG
                and position_effect in {PositionEffect.OPEN, PositionEffect.INCREASE}
            )
            or (
                position_side is PositionSide.SHORT
                and position_effect in {PositionEffect.REDUCE, PositionEffect.CLOSE}
            )
            else OrderSide.SELL
        )
    return OrderIntent(
        id=f"intent-{symbol}-{position_effect.value}",
        symbol=symbol,
        position_side=position_side,
        order_side=order_side,
        position_effect=position_effect,
        quantity=quantity,
        reason="rebalance",
        created_snapshot_id="account-1",
    )


def position(symbol="L", side=PositionSide.LONG, quantity=100, average_cost=1.0):
    return PositionSnapshot(
        symbol=symbol,
        side=side,
        quantity=quantity,
        average_cost=average_cost,
    )


def stage_input(*facts):
    return StageInput(
        run_id="run-1",
        strategy_id="strategy-1",
        strategy_version=1,
        as_of=NOW.isoformat(),
        market_snapshot_id="market-1",
        portfolio_snapshot_id="account-1",
        upstream_facts=tuple(facts),
    )


class BorrowMarginAdmissionTests(unittest.TestCase):
    def test_unavailable_borrow_data_blocks_only_short_targets(self):
        self.assertTrue(hasattr(borrow, "BorrowSnapshot"), "BorrowSnapshot is missing")
        long_target = target("L", PositionSide.LONG)
        short_target = target("S", PositionSide.SHORT)

        result = borrow.admit_borrow(
            (long_target, short_target),
            borrow.BorrowSnapshot.unavailable("borrow-1"),
            ShortPolicy(),
            (),
        )

        self.assertEqual(result.admitted_targets, (long_target,))
        self.assertEqual(
            [(item.symbol, item.reason) for item in result.rejections],
            [("S", "BORROW_DATA_MISSING")],
        )

    def test_existing_short_becomes_cover_only_when_shortable_is_false(self):
        self.assertTrue(hasattr(borrow, "BorrowSecurity"), "BorrowSecurity is missing")
        existing_short = PositionSnapshot(
            symbol="S",
            side=PositionSide.SHORT,
            quantity=10,
            average_cost=100.0,
        )
        snapshot = borrow.BorrowSnapshot(
            id="borrow-1",
            status="AVAILABLE",
            securities={
                "S": borrow.BorrowSecurity(
                    symbol="S",
                    shortable=False,
                    easy_to_borrow=True,
                    borrow_apr_pct=2.0,
                )
            },
        )

        result = borrow.admit_borrow((), snapshot, ShortPolicy(), (existing_short,))

        self.assertEqual(result.position_modes["S"], "COVER_ONLY")

    def test_projected_margin_rate_below_buffer_rejects_risk_increase(self):
        self.assertTrue(hasattr(margin, "admit_margin"), "admit_margin is missing")
        result = margin.admit_margin(
            account(cash=39.0),
            intent(),
            {"L": 1.0},
            MarginPolicy(),
        )

        self.assertFalse(result.admitted)
        self.assertEqual(result.reason, "MARGIN_BUFFER_BREACH")
        self.assertAlmostEqual(result.projected_metrics.margin_rate_pct, 39.0)


class BorrowContractTests(unittest.TestCase):
    def test_borrow_provider_returns_validated_snapshot_contract(self):
        return_type = get_type_hints(ports.BorrowProvider.snapshot)["return"]

        self.assertIs(return_type, borrow.BorrowSnapshot)

    def test_security_validates_exact_booleans_and_optional_apr(self):
        valid = borrow.BorrowSecurity("S", True, False, None)
        self.assertIsNone(valid.borrow_apr_pct)
        for field_name in ("shortable", "easy_to_borrow"):
            for invalid in (1, 0, "true", None):
                values = dict(
                    symbol="S",
                    shortable=True,
                    easy_to_borrow=True,
                    borrow_apr_pct=None,
                )
                values[field_name] = invalid
                with self.subTest(field=field_name, invalid=invalid), self.assertRaises(
                    TypeError
                ):
                    borrow.BorrowSecurity(**values)
        for invalid in (True, "1", -1, math.nan, math.inf, -math.inf):
            with self.subTest(apr=invalid), self.assertRaises((TypeError, ValueError)):
                borrow.BorrowSecurity("S", True, True, invalid)

    def test_snapshot_copies_and_deeply_freezes_security_mapping(self):
        security = borrow.BorrowSecurity("S", True, True, 2)
        source = {"S": security}
        snapshot = borrow.BorrowSnapshot("borrow-1", "AVAILABLE", source)
        source.clear()

        self.assertEqual(snapshot.securities["S"], security)
        self.assertEqual(snapshot.securities["S"].borrow_apr_pct, 2.0)
        self.assertIs(copy.deepcopy(snapshot), snapshot)
        with self.assertRaises(TypeError):
            snapshot.securities["X"] = security
        with self.assertRaises(FrozenInstanceError):
            snapshot.status = "UNAVAILABLE"

    def test_snapshot_validates_status_security_types_and_matching_keys(self):
        security = borrow.BorrowSecurity("S", True, True, None)
        for invalid in (None, 1, "STALE"):
            with self.subTest(status=invalid), self.assertRaises((TypeError, ValueError)):
                borrow.BorrowSnapshot("borrow-1", invalid, {})
        with self.assertRaisesRegex(ValueError, "match"):
            borrow.BorrowSnapshot("borrow-1", "AVAILABLE", {"X": security})
        with self.assertRaises(TypeError):
            borrow.BorrowSnapshot("borrow-1", "AVAILABLE", {"S": {}})

    def test_result_values_are_frozen_and_deepcopy_safe(self):
        result = borrow.admit_borrow(
            (target("S", PositionSide.SHORT),),
            borrow.BorrowSnapshot(
                "borrow-1",
                "AVAILABLE",
                {"S": borrow.BorrowSecurity("S", True, True, None)},
            ),
            ShortPolicy(),
            (),
        )

        self.assertIs(copy.deepcopy(result), result)
        self.assertIsNone(result.borrow_apr_by_symbol["S"])
        with self.assertRaises(TypeError):
            result.borrow_apr_by_symbol["S"] = 9.0
        with self.assertRaises(FrozenInstanceError):
            result.admitted_targets = ()


class BorrowBehaviorTests(unittest.TestCase):
    def test_missing_security_and_explicit_flags_have_stable_reasons(self):
        cases = (
            (
                borrow.BorrowSnapshot("missing", "AVAILABLE", {}),
                "BORROW_DATA_MISSING",
            ),
            (
                borrow.BorrowSnapshot(
                    "not-shortable",
                    "AVAILABLE",
                    {"S": borrow.BorrowSecurity("S", False, True, None)},
                ),
                "SHORT_NOT_SHORTABLE",
            ),
            (
                borrow.BorrowSnapshot(
                    "hard",
                    "AVAILABLE",
                    {"S": borrow.BorrowSecurity("S", True, False, None)},
                ),
                "NOT_EASY_TO_BORROW",
            ),
        )
        for snapshot, expected in cases:
            with self.subTest(expected=expected):
                result = borrow.admit_borrow(
                    (target("S", PositionSide.SHORT),),
                    snapshot,
                    ShortPolicy(),
                    (),
                )
                self.assertEqual(result.rejections[0].reason, expected)
                self.assertEqual(result.rejections[0].symbol, "S")

    def test_policy_can_ignore_explicit_flags_without_inventing_apr(self):
        snapshot = borrow.BorrowSnapshot(
            "borrow-1",
            "AVAILABLE",
            {"S": borrow.BorrowSecurity("S", False, False, None)},
        )
        policy = replace(
            ShortPolicy(),
            require_shortable=False,
            require_easy_to_borrow=False,
        )

        result = borrow.admit_borrow(
            (target("S", PositionSide.SHORT),), snapshot, policy, ()
        )

        self.assertEqual([item.symbol for item in result.admitted_targets], ["S"])
        self.assertIsNone(result.borrow_apr_by_symbol["S"])

    def test_nonblocking_missing_data_admits_short_with_unknown_apr(self):
        policy = replace(ShortPolicy(), block_on_borrow_data_missing=False)

        result = borrow.admit_borrow(
            (target("S", PositionSide.SHORT),),
            borrow.BorrowSnapshot.unavailable("borrow-1"),
            policy,
            (),
        )

        self.assertEqual([item.symbol for item in result.admitted_targets], ["S"])
        self.assertIsNone(result.borrow_apr_by_symbol["S"])
        self.assertNotEqual(
            result.borrow_apr_by_symbol["S"], policy.estimated_borrow_apr_pct
        )

    def test_existing_short_mode_uses_required_flags_but_long_is_unaffected(self):
        snapshot = borrow.BorrowSnapshot(
            "borrow-1",
            "AVAILABLE",
            {"S": borrow.BorrowSecurity("S", False, False, None)},
        )
        existing = (
            position("L", PositionSide.LONG, 10, 10),
            position("S", PositionSide.SHORT, 10, 10),
        )
        restricted = borrow.admit_borrow((), snapshot, ShortPolicy(), existing)
        permissive = borrow.admit_borrow(
            (),
            snapshot,
            replace(
                ShortPolicy(),
                require_shortable=False,
                require_easy_to_borrow=False,
            ),
            existing,
        )

        self.assertNotIn("L", restricted.position_modes)
        self.assertEqual(restricted.position_modes["S"], "COVER_ONLY")
        self.assertEqual(permissive.position_modes["S"], "NORMAL")

    def test_admission_preserves_input_order_and_rejects_duplicate_symbols(self):
        targets = (
            target("Z", PositionSide.LONG),
            target("A", PositionSide.LONG),
            target("S", PositionSide.SHORT),
        )
        before = copy.deepcopy(targets)

        result = borrow.admit_borrow(
            targets,
            borrow.BorrowSnapshot.unavailable("borrow-1"),
            ShortPolicy(),
            (),
        )

        self.assertEqual([item.symbol for item in result.admitted_targets], ["Z", "A"])
        self.assertEqual(targets, before)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            borrow.admit_borrow(
                (targets[0], target("Z", PositionSide.SHORT)),
                borrow.BorrowSnapshot.unavailable("borrow-1"),
                ShortPolicy(),
                (),
            )


class MarginBehaviorTests(unittest.TestCase):
    def test_margin_state_thresholds_are_exact(self):
        cases = (
            (29.999, margin.MARGIN_CALL, False, "MARGIN_CALL"),
            (30.0, margin.REDUCE_ONLY, False, "MARGIN_BUFFER_BREACH"),
            (39.999, margin.REDUCE_ONLY, False, "MARGIN_BUFFER_BREACH"),
            (40.0, margin.NORMAL, True, None),
        )
        for cash, expected_state, admitted, reason in cases:
            with self.subTest(cash=cash):
                result = margin.admit_margin(
                    account(cash=cash), intent(), {"L": 1.0}, MarginPolicy()
                )
                self.assertEqual(result.state, expected_state)
                self.assertEqual(result.admitted, admitted)
                self.assertEqual(result.reason, reason)
                self.assertAlmostEqual(result.projected_metrics.margin_rate_pct, cash)

    def test_economic_buffer_boundary_is_stable_across_binary_float_rounding(self):
        result = margin.admit_margin(
            account(cash=0.12),
            intent(quantity=3),
            {"L": 0.1},
            MarginPolicy(),
        )

        self.assertEqual(result.state, margin.NORMAL)
        self.assertTrue(result.admitted)
        self.assertEqual(result.difference, 0.0)

    def test_risk_reduction_is_admitted_during_margin_call(self):
        original = account(
            cash=0.0,
            positions=(position(quantity=100),),
            margin_loan=90.0,
        )
        reduce_intent = intent(
            position_effect=PositionEffect.REDUCE,
            order_side=OrderSide.SELL,
            quantity=10,
        )

        result = margin.admit_margin(
            original, reduce_intent, {"L": 1.0}, MarginPolicy()
        )

        self.assertTrue(result.admitted)
        self.assertIsNone(result.reason)
        self.assertEqual(result.state, margin.MARGIN_CALL)

    def test_nonpositive_equity_is_margin_call_while_empty_gross_is_normal(self):
        for cash in (0.0, -1.0):
            with self.subTest(cash=cash):
                result = margin.admit_margin(
                    account(cash=cash), intent(), {"L": 1.0}, MarginPolicy()
                )
                self.assertEqual(result.state, margin.MARGIN_CALL)
                self.assertFalse(result.admitted)
                self.assertEqual(result.reason, "MARGIN_CALL")

        close = margin.admit_margin(
            account(cash=0.0, positions=(position(),)),
            intent(position_effect=PositionEffect.CLOSE),
            {"L": 1.0},
            MarginPolicy(),
        )
        self.assertTrue(close.admitted)
        self.assertEqual(close.state, margin.NORMAL)
        self.assertEqual(close.projected_metrics.margin_rate_pct, math.inf)

    def test_result_rejects_nonfinite_numeric_fields(self):
        result = margin.admit_margin(
            account(cash=50.0), intent(), {"L": 1.0}, MarginPolicy()
        )
        for field_name in (
            "required_margin",
            "available_buying_power",
            "difference",
            "maintenance_margin_pct",
            "buffer_threshold_pct",
        ):
            with self.subTest(field=field_name), self.assertRaises(ValueError):
                replace(result, **{field_name: math.inf})

    def test_long_buy_consumes_cash_then_creates_margin_loan_without_mutation(self):
        original = account(cash=39.0)
        before = copy.deepcopy(original)

        projected = margin.project_account_for_intent(original, intent(), {"L": 1.0})
        result = margin.admit_margin(original, intent(), {"L": 1.0}, MarginPolicy())

        self.assertEqual(original, before)
        self.assertEqual(projected.available_cash, 0.0)
        self.assertEqual(projected.margin_loan, 61.0)
        self.assertEqual(projected.positions[0].quantity, 100)
        self.assertEqual(result.current_metrics.equity, 39.0)
        self.assertEqual(result.projected_metrics.margin_loan, 61.0)
        self.assertEqual(result.projected_metrics.long_market_value, 100.0)

    def test_long_increase_recomputes_average_cost(self):
        original = account(
            cash=5.0,
            positions=(position(quantity=10, average_cost=1.0),),
            margin_loan=5.0,
        )
        increase = intent(
            position_effect=PositionEffect.INCREASE,
            quantity=5,
        )

        projected = margin.project_account_for_intent(original, increase, {"L": 3.0})

        self.assertEqual(projected.available_cash, 0.0)
        self.assertEqual(projected.margin_loan, 15.0)
        self.assertEqual(projected.positions[0].quantity, 15)
        self.assertAlmostEqual(projected.positions[0].average_cost, 5.0 / 3.0)

    def test_short_open_balances_restricted_proceeds_and_liability_once(self):
        short_open = intent("S", position_side=PositionSide.SHORT)

        projected = margin.project_account_for_intent(
            account(cash=50.0), short_open, {"S": 1.0}
        )
        result = margin.admit_margin(
            account(cash=50.0), short_open, {"S": 1.0}, MarginPolicy()
        )

        self.assertEqual(projected.available_cash, 50.0)
        self.assertEqual(projected.restricted_short_proceeds, 100.0)
        self.assertEqual(projected.margin_loan, 0.0)
        self.assertEqual(result.projected_metrics.short_liability, 100.0)
        self.assertEqual(result.projected_metrics.equity, 50.0)
        self.assertEqual(result.projected_metrics.margin_rate_pct, 50.0)

    def test_missing_quote_blocks_increase_but_not_reduction(self):
        increasing = margin.admit_margin(
            account(cash=100.0), intent(), {}, MarginPolicy()
        )
        reducing = margin.admit_margin(
            account(cash=0.0, positions=(position(),), margin_loan=50.0),
            intent(position_effect=PositionEffect.REDUCE, quantity=10),
            {},
            MarginPolicy(),
        )

        self.assertFalse(increasing.admitted)
        self.assertEqual(increasing.reason, "MARKET_PRICE_MISSING")
        self.assertTrue(reducing.admitted)
        self.assertIsNone(reducing.reason)

    def test_valuation_overflow_is_not_mislabeled_as_missing_market_data(self):
        huge_position = position(quantity=10**308)
        original = account(cash=1.0, positions=(huge_position,))
        open_other = intent("X", quantity=1)

        with self.assertRaises(ValuationError):
            margin.admit_margin(
                original,
                open_other,
                {"L": 10.0, "X": 1.0},
                MarginPolicy(),
            )

    def test_required_margin_buying_power_and_difference_are_money_amounts(self):
        result = margin.admit_margin(
            account(cash=39.0), intent(), {"L": 1.0}, MarginPolicy()
        )

        self.assertAlmostEqual(result.required_margin, 40.0)
        self.assertAlmostEqual(result.available_buying_power, 97.5)
        self.assertAlmostEqual(result.difference, 1.0)

    def test_result_rejects_inconsistent_derived_amounts(self):
        result = margin.admit_margin(
            account(cash=50.0), intent(), {"L": 1.0}, MarginPolicy()
        )

        with self.assertRaisesRegex(ValueError, "required_margin"):
            replace(result, required_margin=result.required_margin + 1.0)

    def test_invalid_intent_semantics_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "order_side"):
            margin.admit_margin(
                account(cash=100.0),
                intent(order_side=OrderSide.SELL),
                {"L": 1.0},
                MarginPolicy(),
            )
        with self.assertRaisesRegex(ValueError, "INCREASE"):
            margin.admit_margin(
                account(cash=100.0),
                intent(position_effect=PositionEffect.INCREASE, quantity=1),
                {"L": 1.0},
                MarginPolicy(),
            )


class AdmissionStageTests(unittest.TestCase):
    def test_borrow_stage_runs_with_plain_deepcopy_safe_facts(self):
        stage = borrow.BorrowAdmissionStage(
            borrow.BorrowSnapshot.unavailable("borrow-1"), ShortPolicy(), ()
        )
        outputs = PipelineRunner((stage,)).run(
            stage_input(
                {
                    "kind": "exposure_targets",
                    "items": (
                        target("L", PositionSide.LONG),
                        target("S", PositionSide.SHORT),
                    ),
                }
            )
        )

        self.assertEqual(
            [fact["kind"] for fact in outputs[0].facts],
            ["borrow_targets", "borrow_diagnostic", "position_modes"],
        )
        self.assertEqual(copy.deepcopy(outputs[0].facts), outputs[0].facts)
        self.assertIsInstance(outputs[0].facts[1]["borrow_apr_by_symbol"], dict)
        self.assertIsInstance(outputs[0].facts[2]["items"], dict)

    def test_borrow_stage_rejects_duplicate_input_fact_and_target(self):
        stage = borrow.BorrowAdmissionStage(
            borrow.BorrowSnapshot.unavailable("borrow-1"), ShortPolicy(), ()
        )
        fact = {"kind": "exposure_targets", "items": ()}
        with self.assertRaises(PipelineContractError):
            PipelineRunner((stage,)).run(stage_input(fact, fact.copy()))
        with self.assertRaises(PipelineContractError):
            PipelineRunner((stage,)).run(
                stage_input(
                    {
                        "kind": "exposure_targets",
                        "items": (
                            target("S", PositionSide.SHORT),
                            target("S", PositionSide.SHORT),
                        ),
                    }
                )
            )

    def test_margin_stage_runs_and_outputs_admitted_rejected_diagnostics(self):
        stage = margin.MarginAdmissionStage(
            account(cash=50.0), {"L": 1.0, "S": 1.0}, MarginPolicy()
        )
        outputs = PipelineRunner((stage,)).run(
            stage_input(
                {
                    "kind": "order_intents",
                    "items": (
                        intent("L", quantity=100),
                        intent("S", position_side=PositionSide.SHORT, quantity=100),
                    ),
                }
            )
        )
        facts = outputs[0].facts

        self.assertEqual(
            [fact["kind"] for fact in facts],
            [
                "margin_admitted_intents",
                "margin_rejected_intents",
                "margin_diagnostics",
            ],
        )
        self.assertEqual([item.symbol for item in facts[0]["items"]], ["L"])
        self.assertEqual([item.symbol for item in facts[1]["items"]], ["S"])
        self.assertEqual(copy.deepcopy(facts), facts)
        self.assertIsInstance(facts[2]["items"][0], dict)

    def test_margin_stage_missing_fact_is_empty_and_duplicate_fact_errors(self):
        stage = margin.MarginAdmissionStage(
            account(cash=50.0), {"L": 1.0}, MarginPolicy()
        )
        output = PipelineRunner((stage,)).run(stage_input())[0]
        self.assertEqual(output.facts[0]["items"], ())
        fact = {"kind": "order_intents", "items": ()}
        with self.assertRaises(PipelineContractError):
            PipelineRunner((stage,)).run(stage_input(fact, fact.copy()))


if __name__ == "__main__":
    unittest.main()
