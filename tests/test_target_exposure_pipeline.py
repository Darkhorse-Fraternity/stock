import math
import unittest
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from enum import Enum

from stock_recommender.pipeline import (
    PipelineContractError,
    PipelineRunner,
    StageInput,
    StageOutput,
)
from stock_recommender.portfolio_engine.config import (
    ExposurePolicy,
    effective_exposure_policy,
)
from stock_recommender.portfolio_engine.contracts import (
    DecisionBatch,
    ExecutionFill,
    MarketSnapshot,
    OrderIntent,
    OrderSide,
    PortfolioEvent,
    PositionEffect,
    PositionSide,
    SignalCandidate,
    TargetPosition,
)
from stock_recommender.portfolio_engine.exposure import (
    ExposureBudgetStage,
    ExposureDiagnostic,
    allocate_exposure,
)
from stock_recommender.portfolio_engine.target_pipeline import (
    TargetNettingStage,
    net_signal_candidates,
)


def signal(
    symbol,
    side="LONG",
    score=0.9,
    weight=10.0,
    model_id="model-v1",
    thesis_id=None,
):
    return SignalCandidate(
        symbol=symbol,
        side=PositionSide(side),
        score=score,
        requested_weight_pct=weight,
        model_id=model_id,
        thesis_id=thesis_id or f"thesis-{symbol}-{side}",
    )


def target(
    symbol,
    side="LONG",
    score=0.9,
    weight=10.0,
    model_id="model-v1",
    thesis_id=None,
):
    return TargetPosition(
        symbol=symbol,
        side=PositionSide(side),
        target_weight_pct=weight,
        signal_score=score,
        model_id=model_id,
        thesis_id=thesis_id or f"thesis-{symbol}-{side}",
    )


def policy(mode="LONG_SHORT", **updates):
    values = {
        "mode": mode,
        "max_positions": 10,
        "max_gross_exposure_pct": 150.0,
        "max_net_exposure_pct": 120.0,
        "max_long_exposure_pct": 120.0,
        "max_short_exposure_pct": 30.0,
        "max_long_position_pct": 15.0,
        "max_short_position_pct": 5.0,
    }
    values.update(updates)
    return effective_exposure_policy(values)


def stage_input(*facts):
    return StageInput(
        run_id="run-1",
        strategy_id="strategy-1",
        strategy_version=3,
        as_of="2026-07-31T08:00:00+08:00",
        market_snapshot_id="market-1",
        portfolio_snapshot_id="portfolio-1",
        upstream_facts=tuple(facts),
    )


class MutableLeaf:
    def __init__(self, value="mutable"):
        self.value = value


class MutableEnum(Enum):
    VALUE = []


class DeepImmutableEnum(Enum):
    VALUE = ("safe", frozenset({1, 2}))


class TargetNettingTests(unittest.TestCase):
    def test_same_symbol_opposing_targets_are_netted(self):
        targets = net_signal_candidates(
            (
                signal("ABC", "LONG", 0.9, 15),
                signal("ABC", "SHORT", 0.8, 5),
            )
        )

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].side, PositionSide.LONG)
        self.assertEqual(targets[0].target_weight_pct, 10.0)

    def test_same_direction_duplicates_sum_and_exact_opposites_disappear(self):
        signals = (
            signal("SUM", "LONG", 0.7, 4, model_id="lower"),
            signal("SUM", "LONG", 0.9, 6, model_id="winner"),
            signal("ZERO", "LONG", 0.8, 5),
            signal("ZERO", "SHORT", 0.6, 5),
        )

        targets = net_signal_candidates(signals)

        self.assertEqual([(item.symbol, item.target_weight_pct) for item in targets], [("SUM", 10.0)])
        self.assertEqual(targets[0].model_id, "winner")
        self.assertEqual(signals[0].requested_weight_pct, 4)

    def test_representative_comes_from_winning_direction_with_stable_ties(self):
        inputs = (
            signal("ABC", "LONG", 0.99, 2, model_id="unselected"),
            signal("ABC", "SHORT", 0.8, 4, model_id="z-model", thesis_id="z"),
            signal("ABC", "SHORT", 0.8, 3, model_id="a-model", thesis_id="a"),
        )

        forward = net_signal_candidates(inputs)
        reverse = net_signal_candidates(tuple(reversed(inputs)))

        self.assertEqual(forward, reverse)
        self.assertEqual(forward[0].side, PositionSide.SHORT)
        self.assertEqual(forward[0].target_weight_pct, 5.0)
        self.assertEqual((forward[0].model_id, forward[0].thesis_id), ("a-model", "a"))

    def test_net_targets_sort_by_score_then_symbol_then_side(self):
        inputs = (
            signal("ZZZ", "SHORT", 0.8, 2),
            signal("BBB", "LONG", 0.9, 2),
            signal("AAA", "SHORT", 0.9, 2),
            signal("TOP", "LONG", 1.0, 2),
        )

        forward = net_signal_candidates(inputs)
        reverse = net_signal_candidates(tuple(reversed(inputs)))

        self.assertEqual(forward, reverse)
        self.assertEqual([item.symbol for item in forward], ["TOP", "AAA", "BBB", "ZZZ"])

    def test_invalid_signal_numbers_raise_without_illegal_output(self):
        for field, value in (
            ("weight", 0.0),
            ("weight", -1.0),
            ("weight", math.nan),
            ("weight", math.inf),
            ("score", math.nan),
            ("score", math.inf),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                net_signal_candidates((signal("BAD", **{field: value}),))

    def test_overflowing_signal_numbers_raise_value_error(self):
        with self.assertRaises(ValueError):
            net_signal_candidates((signal("HUGE", weight=10**10000),))


class ExposureAllocationTests(unittest.TestCase):
    def test_long_short_targets_obey_all_system_caps(self):
        raw = tuple(
            target(f"L{index}", "LONG", 1 - index / 100, 15)
            for index in range(10)
        ) + tuple(
            target(f"S{index}", "SHORT", 0.8 - index / 100, 5)
            for index in range(6)
        )

        admitted, diagnostic = allocate_exposure(raw, policy("LONG_SHORT"))

        self.assertLessEqual(len(admitted), 10)
        self.assertLessEqual(diagnostic.gross_exposure_pct, 150.0)
        self.assertLessEqual(abs(diagnostic.net_exposure_pct), 120.0)
        self.assertLessEqual(diagnostic.long_exposure_pct, 120.0)
        self.assertLessEqual(diagnostic.short_exposure_pct, 30.0)

    def test_long_only_rejects_short_targets_first(self):
        admitted, diagnostic = allocate_exposure(
            (target("S", "SHORT", 1.0, 5),),
            policy("LONG_ONLY"),
        )

        self.assertEqual(admitted, ())
        self.assertEqual(diagnostic.rejections[0]["reason"], "MODE_DISALLOWS_SHORT")

    def test_long_leveraged_also_rejects_shorts_and_caps_long_at_120(self):
        raw = tuple(target(f"L{index}", weight=15) for index in range(9)) + (
            target("SHORT", "SHORT", weight=5),
        )

        admitted, diagnostic = allocate_exposure(raw, policy("LONG_LEVERAGED"))

        self.assertTrue(all(item.side == PositionSide.LONG for item in admitted))
        self.assertAlmostEqual(diagnostic.long_exposure_pct, 120.0)
        self.assertAlmostEqual(diagnostic.gross_exposure_pct, 120.0)
        self.assertAlmostEqual(diagnostic.net_exposure_pct, 120.0)
        self.assertIn("MODE_DISALLOWS_SHORT", [item["reason"] for item in diagnostic.rejections])

    def test_long_only_effective_exposure_is_100_percent(self):
        raw = tuple(target(f"L{index}", weight=15) for index in range(8))

        admitted, diagnostic = allocate_exposure(raw, policy("LONG_ONLY"))

        self.assertAlmostEqual(sum(item.target_weight_pct for item in admitted), 100.0)
        self.assertAlmostEqual(diagnostic.gross_exposure_pct, 100.0)
        self.assertAlmostEqual(diagnostic.net_exposure_pct, 100.0)

    def test_per_position_caps_run_before_selection_and_are_diagnostic(self):
        raw = (
            target("LONG", "LONG", score=0.9, weight=99),
            target("SHORT", "SHORT", score=0.8, weight=99),
        )

        admitted, diagnostic = allocate_exposure(raw, policy())

        self.assertEqual(
            [(item.symbol, item.target_weight_pct) for item in admitted],
            [("LONG", 15.0), ("SHORT", 5.0)],
        )
        caps = [item for item in diagnostic.rejections if item["reason"] == "POSITION_CAP"]
        self.assertEqual([item["symbol"] for item in caps], ["LONG", "SHORT"])

    def test_selection_uses_score_then_symbol_and_rejects_remainder(self):
        raw = (
            target("ZZZ", score=0.9, weight=2),
            target("AAA", score=0.9, weight=2),
            target("TOP", score=1.0, weight=2),
        )

        admitted, diagnostic = allocate_exposure(raw, policy(max_positions=2))

        self.assertEqual([item.symbol for item in admitted], ["TOP", "AAA"])
        self.assertEqual(
            [(item["symbol"], item["reason"]) for item in diagnostic.rejections],
            [("ZZZ", "MAX_POSITIONS")],
        )

    def test_direction_caps_scale_each_direction_pro_rata(self):
        raw = (
            target("L1", "LONG", score=1.0, weight=10),
            target("L2", "LONG", score=0.9, weight=5),
            target("S1", "SHORT", score=0.8, weight=4),
            target("S2", "SHORT", score=0.7, weight=2),
        )

        admitted, diagnostic = allocate_exposure(
            raw,
            policy(max_long_exposure_pct=9, max_short_exposure_pct=3),
        )
        weights = {item.symbol: item.target_weight_pct for item in admitted}

        self.assertAlmostEqual(weights["L1"] / weights["L2"], 2.0)
        self.assertAlmostEqual(weights["S1"] / weights["S2"], 2.0)
        self.assertAlmostEqual(diagnostic.long_exposure_pct, 9.0)
        self.assertAlmostEqual(diagnostic.short_exposure_pct, 3.0)
        self.assertEqual(
            {item["reason"] for item in diagnostic.rejections},
            {"DIRECTION_CAP"},
        )

    def test_scaled_direction_diagnostic_does_not_round_above_cap(self):
        weights = (
            7.0836786536417495,
            5.6942283498559165,
            3.149322095572157,
            7.317849847862213,
            13.399755638364526,
            5.847132105317011,
            9.111569944278905,
            11.507364437219431,
        )
        cap = 43.91463921734726

        _, diagnostic = allocate_exposure(
            tuple(
                target(f"L{index}", score=1 - index / 100, weight=weight)
                for index, weight in enumerate(weights)
            ),
            policy(max_long_exposure_pct=cap),
        )

        self.assertLessEqual(diagnostic.long_exposure_pct, cap)

    def test_gross_cap_scales_entire_portfolio_pro_rata(self):
        raw = (
            target("L1", "LONG", score=1.0, weight=12),
            target("L2", "LONG", score=0.9, weight=6),
            target("S1", "SHORT", score=0.8, weight=4),
            target("S2", "SHORT", score=0.7, weight=2),
        )

        admitted, diagnostic = allocate_exposure(raw, policy(max_gross_exposure_pct=12))
        weights = {item.symbol: item.target_weight_pct for item in admitted}

        self.assertAlmostEqual(weights["L1"] / weights["L2"], 2.0)
        self.assertAlmostEqual(weights["S1"] / weights["S2"], 2.0)
        self.assertAlmostEqual(weights["L1"] / weights["S1"], 3.0)
        self.assertAlmostEqual(diagnostic.gross_exposure_pct, 12.0)
        self.assertEqual(
            {item["reason"] for item in diagnostic.rejections},
            {"GROSS_CAP"},
        )

    def test_net_cap_only_reduces_dominant_direction(self):
        raw = (
            target("L1", "LONG", score=1.0, weight=12),
            target("L2", "LONG", score=0.9, weight=8),
            target("S1", "SHORT", score=0.8, weight=5),
        )

        admitted, diagnostic = allocate_exposure(raw, policy(max_net_exposure_pct=5))
        weights = {item.symbol: item.target_weight_pct for item in admitted}

        self.assertAlmostEqual(weights["S1"], 5.0)
        self.assertAlmostEqual(weights["L1"] / weights["L2"], 1.5)
        self.assertAlmostEqual(weights["L1"] + weights["L2"], 10.0)
        self.assertAlmostEqual(diagnostic.net_exposure_pct, 5.0)
        self.assertEqual(
            {item["reason"] for item in diagnostic.rejections},
            {"NET_CAP"},
        )

    def test_negative_net_cap_reduces_short_direction_without_adding_longs(self):
        raw = (
            target("L", "LONG", score=1.0, weight=5),
            target("S1", "SHORT", score=0.9, weight=5),
            target("S2", "SHORT", score=0.8, weight=5),
        )

        admitted, diagnostic = allocate_exposure(raw, policy(max_net_exposure_pct=2))
        weights = {item.symbol: item.target_weight_pct for item in admitted}

        self.assertAlmostEqual(weights["L"], 5.0)
        self.assertAlmostEqual(weights["S1"] + weights["S2"], 7.0)
        self.assertAlmostEqual(diagnostic.net_exposure_pct, -2.0)

    def test_input_order_does_not_change_targets_or_diagnostics(self):
        raw = (
            target("D", "SHORT", score=0.7, weight=9),
            target("B", "LONG", score=0.9, weight=20),
            target("C", "SHORT", score=0.8, weight=4),
            target("A", "LONG", score=0.9, weight=10),
        )

        forward = allocate_exposure(raw, policy(max_positions=3, max_gross_exposure_pct=15))
        reverse = allocate_exposure(tuple(reversed(raw)), policy(max_positions=3, max_gross_exposure_pct=15))

        self.assertEqual(forward, reverse)

    def test_invalid_targets_are_safely_rejected_and_diagnostics_stay_finite(self):
        raw = (
            target("GOOD", weight=4),
            target("ZERO", weight=0),
            target("NEGATIVE", weight=-1),
            target("NAN_WEIGHT", weight=math.nan),
            target("INF_WEIGHT", weight=math.inf),
            target("NAN_SCORE", score=math.nan),
            target("INF_SCORE", score=math.inf),
        )

        admitted, diagnostic = allocate_exposure(raw, policy())

        self.assertEqual([item.symbol for item in admitted], ["GOOD"])
        self.assertEqual(
            [item["reason"] for item in diagnostic.rejections],
            ["INVALID_TARGET"] * 6,
        )
        for value in (
            diagnostic.gross_exposure_pct,
            diagnostic.net_exposure_pct,
            diagnostic.long_exposure_pct,
            diagnostic.short_exposure_pct,
        ):
            self.assertTrue(math.isfinite(value))
        for item in diagnostic.rejections:
            for value in item.values():
                if isinstance(value, float):
                    self.assertTrue(math.isfinite(value))

    def test_overflowing_target_numbers_are_safely_rejected(self):
        admitted, diagnostic = allocate_exposure(
            (target("HUGE", weight=10**10000),),
            policy(),
        )

        self.assertEqual(admitted, ())
        self.assertEqual(diagnostic.rejections[0]["reason"], "INVALID_TARGET")

    def test_zero_position_limit_returns_empty_with_clear_diagnostics(self):
        admitted, diagnostic = allocate_exposure(
            (target("A", weight=5), target("B", weight=5)),
            policy(max_positions=0),
        )

        self.assertEqual(admitted, ())
        self.assertEqual(diagnostic.gross_exposure_pct, 0.0)
        self.assertEqual(
            [(item["symbol"], item["reason"]) for item in diagnostic.rejections],
            [("A", "MAX_POSITIONS"), ("B", "MAX_POSITIONS")],
        )

    def test_diagnostic_and_rejections_are_defensively_immutable(self):
        _, diagnostic = allocate_exposure(
            (target("LONG", weight=99),),
            policy(),
        )

        self.assertIsInstance(diagnostic, ExposureDiagnostic)
        self.assertIsInstance(diagnostic.rejections, tuple)
        self.assertIsInstance(diagnostic.rejections[0], Mapping)
        self.assertIs(deepcopy(diagnostic), diagnostic)
        with self.assertRaises(FrozenInstanceError):
            diagnostic.gross_exposure_pct = 0
        with self.assertRaises(TypeError):
            diagnostic.rejections[0]["reason"] = "changed"

    def test_direct_diagnostic_rejects_nonfinite_and_negative_exposures(self):
        for field_name in (
            "gross_exposure_pct",
            "net_exposure_pct",
            "long_exposure_pct",
            "short_exposure_pct",
        ):
            for value in (math.nan, math.inf, -math.inf):
                with self.subTest(field=field_name, value=value), self.assertRaises(
                    ValueError
                ):
                    ExposureDiagnostic(**{field_name: value})

        for field_name in (
            "gross_exposure_pct",
            "long_exposure_pct",
            "short_exposure_pct",
        ):
            with self.subTest(field=field_name), self.assertRaises(ValueError):
                ExposureDiagnostic(**{field_name: -1.0})

    def test_direct_diagnostic_requires_consistent_exposure_totals(self):
        with self.assertRaises(ValueError):
            ExposureDiagnostic(
                gross_exposure_pct=12.0,
                net_exposure_pct=5.0,
                long_exposure_pct=8.0,
                short_exposure_pct=3.0,
            )
        with self.assertRaises(ValueError):
            ExposureDiagnostic(
                gross_exposure_pct=11.0,
                net_exposure_pct=4.0,
                long_exposure_pct=8.0,
                short_exposure_pct=3.0,
            )

    def test_diagnostic_recursively_freezes_rejection_payloads(self):
        mutable_items = ["cap"]
        mutable_bytes = bytearray(b"proof")
        rejection = {
            "reason": "POSITION_CAP",
            "details": {
                "items": mutable_items,
                "proof": mutable_bytes,
            },
        }

        diagnostic = ExposureDiagnostic(rejections=(rejection,))
        mutable_items.append("mutated")
        mutable_bytes[0] = ord("X")
        rejection["details"]["new"] = True

        self.assertEqual(
            diagnostic.rejections[0]["details"]["items"],
            ("cap",),
        )
        self.assertEqual(
            diagnostic.rejections[0]["details"]["proof"],
            b"proof",
        )
        self.assertNotIn("new", diagnostic.rejections[0]["details"])
        self.assertIs(deepcopy(diagnostic), diagnostic)
        with self.assertRaises(TypeError):
            diagnostic.rejections[0]["details"]["new"] = True

    def test_diagnostic_rejects_unknown_mutable_rejection_leaf(self):
        with self.assertRaisesRegex(TypeError, "MutableLeaf"):
            ExposureDiagnostic(
                rejections=({"reason": "INVALID", "value": MutableLeaf()},)
            )

    def test_diagnostic_rejects_enum_with_mutable_value(self):
        with self.assertRaisesRegex(TypeError, "MutableEnum"):
            ExposureDiagnostic(
                rejections=(
                    {"reason": "INVALID", "value": MutableEnum.VALUE},
                )
            )


class TargetExposureStageTests(unittest.TestCase):
    def test_stages_emit_versioned_target_and_diagnostic_facts(self):
        signals = (
            signal("ABC", "LONG", 0.9, 15),
            signal("ABC", "SHORT", 0.8, 5),
            signal("SHORT", "SHORT", 0.7, 5),
        )
        net_stage = TargetNettingStage()

        net_output = net_stage.evaluate(
            stage_input({"kind": "signal_candidates", "items": signals})
        )
        exposure_output = ExposureBudgetStage(policy("LONG_ONLY")).evaluate(
            stage_input(*net_output.facts)
        )

        self.assertEqual(net_output.stage, "target_netting")
        self.assertEqual(net_output.component_version, "1.0.0")
        self.assertEqual(net_output.facts[0]["kind"], "net_targets")
        self.assertIsInstance(net_output.facts[0]["items"], tuple)
        self.assertEqual(exposure_output.stage, "exposure_budget")
        self.assertEqual(exposure_output.component_version, "1.0.0")
        self.assertEqual(
            [fact["kind"] for fact in exposure_output.facts],
            ["exposure_targets", "exposure_diagnostic"],
        )
        self.assertEqual(
            exposure_output.facts[1]["rejections"][0]["reason"],
            "MODE_DISALLOWS_SHORT",
        )

    def test_missing_upstream_target_facts_produce_empty_outputs(self):
        net_output = TargetNettingStage().evaluate(stage_input())
        exposure_output = ExposureBudgetStage(policy()).evaluate(stage_input())

        self.assertEqual(net_output.facts[0]["items"], ())
        self.assertEqual(exposure_output.facts[0]["items"], ())
        self.assertEqual(exposure_output.facts[1]["gross_exposure_pct"], 0.0)
        self.assertEqual(exposure_output.facts[1]["rejections"], ())

    def test_duplicate_consumed_fact_is_a_contract_error(self):
        candidate_fact = {"kind": "signal_candidates", "items": ()}
        net_fact = {"kind": "net_targets", "items": ()}

        with self.assertRaises(PipelineContractError):
            TargetNettingStage().evaluate(stage_input(candidate_fact, candidate_fact))
        with self.assertRaises(PipelineContractError):
            ExposureBudgetStage(policy()).evaluate(stage_input(net_fact, net_fact))

    def test_exposure_output_can_be_passed_through_pipeline_runner(self):
        class CaptureStage:
            name = "capture"
            component_version = "1.0.0"

            def evaluate(self, current):
                return StageOutput(
                    stage=self.name,
                    component_version=self.component_version,
                    facts=({"kind": "captured", "count": len(current.upstream_facts)},),
                )

        outputs = PipelineRunner(
            (ExposureBudgetStage(policy()), CaptureStage())
        ).run(
            stage_input(
                {"kind": "net_targets", "items": (target("A", weight=99),)}
            )
        )

        self.assertEqual(outputs[-1].facts[0]["count"], 3)

    def test_real_runner_accepts_deeply_frozen_signal_candidate_facts(self):
        candidate = SignalCandidate(
            symbol="SHORT",
            side=PositionSide.SHORT,
            score=0.9,
            requested_weight_pct=5.0,
            model_id="short-v1",
            thesis_id="thesis-short",
            facts={"nested": {"labels": ["breakdown"]}},
        )

        outputs = PipelineRunner(
            (TargetNettingStage(), ExposureBudgetStage(policy("LONG_ONLY")))
        ).run(
            stage_input(
                {"kind": "signal_candidates", "items": (candidate,)}
            )
        )

        self.assertEqual(outputs[0].facts[0]["kind"], "net_targets")
        self.assertEqual(outputs[1].facts[0]["items"], ())
        rejection = outputs[1].facts[1]["rejections"][0]
        self.assertIsInstance(rejection, dict)
        self.assertEqual(rejection["reason"], "MODE_DISALLOWS_SHORT")


class PortfolioContractDeepcopyTests(unittest.TestCase):
    def test_deeply_frozen_domain_contracts_reuse_identity_on_deepcopy(self):
        now = datetime.now(timezone.utc)
        candidate = SignalCandidate(
            symbol="AAPL",
            side=PositionSide.LONG,
            score=0.9,
            requested_weight_pct=10.0,
            model_id="long-v1",
            thesis_id="thesis-1",
            facts={"nested": {"items": [1]}},
        )
        target_value = target("AAPL")
        intent = OrderIntent(
            id="intent-1",
            symbol="AAPL",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            quantity=1,
            reason="target",
            created_snapshot_id="market-1",
        )
        market = MarketSnapshot(
            id="market-1",
            occurred_at=now,
            quotes={"AAPL": {"levels": [{"bid": 99.0}]}},
        )
        fill = ExecutionFill(
            intent_id="intent-1",
            symbol="AAPL",
            quantity=1,
            price=100.0,
            fees=1.0,
            status="FILLED",
        )
        event = PortfolioEvent(
            id="event-1",
            type="FILL",
            occurred_at=now,
            data={"nested": {"items": [1]}},
        )
        decision = DecisionBatch(
            run_key="run-1",
            strategy_id="strategy-1",
            strategy_revision=1,
            portfolio_snapshot_id="portfolio-1",
            market_snapshot_id="market-1",
            intents=(intent,),
            fills=(fill,),
            events=(event,),
            diagnostics=({"code": "SAFE", "nested": {"items": [1]}},),
            stage_outputs=(
                StageOutput(
                    stage="test",
                    component_version="1",
                    facts=({"nested": {"items": [1]}},),
                ),
            ),
        )

        for value in (
            candidate,
            target_value,
            intent,
            market,
            fill,
            event,
            decision,
        ):
            with self.subTest(type=type(value).__name__):
                self.assertIs(deepcopy(value), value)

    def test_binary_buffers_are_copied_to_immutable_bytes(self):
        now = datetime.now(timezone.utc)

        def build_candidate(payload):
            return SignalCandidate(
                symbol="AAPL",
                side=PositionSide.LONG,
                score=0.9,
                requested_weight_pct=10.0,
                model_id="long-v1",
                thesis_id="thesis-1",
                facts={"payload": payload},
            ), lambda value: value.facts["payload"]

        def build_market(payload):
            return MarketSnapshot(
                id="market-1",
                occurred_at=now,
                quotes={"AAPL": {"payload": payload}},
            ), lambda value: value.quotes["AAPL"]["payload"]

        def build_event(payload):
            return PortfolioEvent(
                id="event-1",
                type="TEST",
                occurred_at=now,
                data={"payload": payload},
            ), lambda value: value.data["payload"]

        def build_decision(payload):
            return DecisionBatch(
                run_key="run-1",
                strategy_id="strategy-1",
                strategy_revision=1,
                portfolio_snapshot_id="portfolio-1",
                market_snapshot_id="market-1",
                diagnostics=({"payload": payload},),
            ), lambda value: value.diagnostics[0]["payload"]

        for name, builder in (
            ("candidate", build_candidate),
            ("market", build_market),
            ("event", build_event),
            ("decision", build_decision),
        ):
            with self.subTest(contract=name):
                source_bytes = bytearray(b"buffer")
                view_source = bytearray(b"memory")
                payload = {
                    "buffer": source_bytes,
                    "view": memoryview(view_source),
                }
                value, payload_of = builder(payload)
                source_bytes[0] = ord("X")
                view_source[0] = ord("Y")

                self.assertEqual(payload_of(value)["buffer"], b"buffer")
                self.assertEqual(payload_of(value)["view"], b"memory")
                self.assertIs(deepcopy(value), value)

    def test_unknown_mutable_leaves_are_rejected_by_mapping_contracts(self):
        now = datetime.now(timezone.utc)
        factories = (
            lambda leaf: SignalCandidate(
                symbol="AAPL",
                side=PositionSide.LONG,
                score=0.9,
                requested_weight_pct=10.0,
                model_id="long-v1",
                thesis_id="thesis-1",
                facts={"nested": {"leaf": leaf}},
            ),
            lambda leaf: MarketSnapshot(
                id="market-1",
                occurred_at=now,
                quotes={"AAPL": {"leaf": leaf}},
            ),
            lambda leaf: PortfolioEvent(
                id="event-1",
                type="TEST",
                occurred_at=now,
                data={"nested": {"leaf": leaf}},
            ),
            lambda leaf: DecisionBatch(
                run_key="run-1",
                strategy_id="strategy-1",
                strategy_revision=1,
                portfolio_snapshot_id="portfolio-1",
                market_snapshot_id="market-1",
                diagnostics=({"nested": {"leaf": leaf}},),
            ),
        )

        for factory in factories:
            with self.subTest(factory=factory), self.assertRaisesRegex(
                TypeError, "MutableLeaf"
            ):
                factory(MutableLeaf())

    def test_mutable_mapping_keys_are_rejected(self):
        mutable_key = MutableLeaf("key")

        with self.assertRaisesRegex(TypeError, "MutableLeaf"):
            SignalCandidate(
                symbol="AAPL",
                side=PositionSide.LONG,
                score=0.9,
                requested_weight_pct=10.0,
                model_id="long-v1",
                thesis_id="thesis-1",
                facts={mutable_key: "value"},
            )

    def test_signal_facts_reject_enum_with_mutable_value(self):
        with self.assertRaisesRegex(TypeError, "MutableEnum"):
            SignalCandidate(
                symbol="AAPL",
                side=PositionSide.LONG,
                score=0.9,
                requested_weight_pct=10.0,
                model_id="long-v1",
                thesis_id="thesis-1",
                facts={"value": MutableEnum.VALUE},
            )

    def test_deeply_immutable_enum_values_remain_supported(self):
        candidate = SignalCandidate(
            symbol="AAPL",
            side=PositionSide.LONG,
            score=0.9,
            requested_weight_pct=10.0,
            model_id="long-v1",
            thesis_id="thesis-1",
            facts={
                "side": PositionSide.LONG,
                "nested": DeepImmutableEnum.VALUE,
            },
        )
        diagnostic = ExposureDiagnostic(
            rejections=(
                {
                    "reason": "SAFE",
                    "side": PositionSide.LONG,
                    "nested": DeepImmutableEnum.VALUE,
                },
            )
        )

        self.assertIs(candidate.facts["side"], PositionSide.LONG)
        self.assertIs(candidate.facts["nested"], DeepImmutableEnum.VALUE)
        self.assertIs(
            diagnostic.rejections[0]["nested"],
            DeepImmutableEnum.VALUE,
        )


if __name__ == "__main__":
    unittest.main()
