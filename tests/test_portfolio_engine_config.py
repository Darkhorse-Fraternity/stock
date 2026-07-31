import math
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

from stock_recommender.parameters import normalize_strategy_config
from stock_recommender.pipeline import StageOutput
from stock_recommender.portfolio_engine.config import (
    ExposurePolicy,
    MarginPolicy,
    ShortPolicy,
    StrategyPolicyError,
    effective_exposure_policy,
    normalize_exposure_policy,
    normalize_margin_policy,
    normalize_short_policy,
    validate_strategy_policies,
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


class PortfolioEngineConfigTests(unittest.TestCase):
    def test_existing_strategy_normalizes_to_explicit_long_only(self):
        strategy = normalize_strategy_config({"name": "旧策略", "parameters": {}})

        self.assertEqual(strategy["exposure_policy"]["mode"], "LONG_ONLY")
        self.assertEqual(strategy["exposure_policy"]["max_positions"], 10)
        self.assertEqual(strategy["margin_policy"]["maintenance_margin_pct"], 30.0)
        self.assertEqual(
            strategy["short_policy"]["signal_model"],
            "short_trend_breakdown_v1",
        )

    def test_policy_cannot_exceed_system_hard_limits(self):
        policy = normalize_exposure_policy(
            {
                "mode": "LONG_SHORT",
                "max_positions": 99,
                "max_gross_exposure_pct": 500,
                "max_net_exposure_pct": 500,
                "max_long_exposure_pct": 500,
                "max_short_exposure_pct": 100,
                "max_long_position_pct": 100,
                "max_short_position_pct": 100,
            }
        )

        self.assertEqual(policy["max_positions"], 10)
        self.assertEqual(policy["max_gross_exposure_pct"], 150.0)
        self.assertEqual(policy["max_net_exposure_pct"], 120.0)
        self.assertEqual(policy["max_long_exposure_pct"], 120.0)
        self.assertEqual(policy["max_short_exposure_pct"], 30.0)
        self.assertEqual(policy["max_long_position_pct"], 15.0)
        self.assertEqual(policy["max_short_position_pct"], 5.0)

    def test_long_only_effective_caps_are_one_hundred_percent(self):
        effective = effective_exposure_policy(
            normalize_exposure_policy(
                {
                    "mode": "LONG_ONLY",
                    "max_gross_exposure_pct": 150,
                    "max_net_exposure_pct": 120,
                    "max_long_exposure_pct": 120,
                }
            )
        )

        self.assertEqual(effective.max_gross_exposure_pct, 100.0)
        self.assertEqual(effective.max_net_exposure_pct, 100.0)
        self.assertEqual(effective.max_long_exposure_pct, 100.0)
        self.assertEqual(effective.max_short_exposure_pct, 0.0)
        self.assertEqual(effective.max_short_position_pct, 0.0)

    def test_effective_caps_follow_each_enabled_mode(self):
        leveraged = effective_exposure_policy(
            normalize_exposure_policy({"mode": "LONG_LEVERAGED"})
        )
        long_short = effective_exposure_policy(
            normalize_exposure_policy({"mode": "LONG_SHORT"})
        )

        self.assertEqual(leveraged.max_gross_exposure_pct, 120.0)
        self.assertEqual(leveraged.max_net_exposure_pct, 120.0)
        self.assertEqual(leveraged.max_long_exposure_pct, 120.0)
        self.assertEqual(leveraged.max_short_exposure_pct, 0.0)
        self.assertEqual(long_short.max_gross_exposure_pct, 150.0)
        self.assertEqual(long_short.max_net_exposure_pct, 120.0)
        self.assertEqual(long_short.max_long_exposure_pct, 120.0)
        self.assertEqual(long_short.max_short_exposure_pct, 30.0)

    def test_non_us_strategy_cannot_enable_leverage_or_short(self):
        for mode in ("LONG_LEVERAGED", "LONG_SHORT"):
            with self.subTest(mode=mode), self.assertRaises(StrategyPolicyError):
                validate_strategy_policies(
                    {"market": "cn", "exposure_policy": {"mode": mode}}
                )

    def test_us_strategy_can_enable_leverage_or_short(self):
        for mode in ("LONG_LEVERAGED", "LONG_SHORT"):
            with self.subTest(mode=mode):
                validate_strategy_policies(
                    {"market": "us", "exposure_policy": {"mode": mode}}
                )

    def test_us_market_aliases_can_enable_leverage_or_short(self):
        for market in ("USA", "美股"):
            for mode in ("LONG_LEVERAGED", "LONG_SHORT"):
                with self.subTest(market=market, mode=mode):
                    validate_strategy_policies(
                        {"market": market, "exposure_policy": {"mode": mode}}
                    )

    def test_cn_market_aliases_still_reject_leverage_or_short(self):
        for market in ("CN", "A股"):
            for mode in ("LONG_LEVERAGED", "LONG_SHORT"):
                with self.subTest(market=market, mode=mode), self.assertRaises(
                    StrategyPolicyError
                ):
                    validate_strategy_policies(
                        {"market": market, "exposure_policy": {"mode": mode}}
                    )

    def test_conflicting_market_sources_are_rejected(self):
        for direct, parameter in (("us", "cn"), ("cn", "us")):
            with self.subTest(direct=direct, parameter=parameter), self.assertRaises(
                StrategyPolicyError
            ):
                validate_strategy_policies(
                    {
                        "market": direct,
                        "parameters": {
                            "market": {"enabled": True, "value": parameter}
                        },
                        "exposure_policy": {"mode": "LONG_SHORT"},
                    }
                )

    def test_equivalent_market_sources_are_allowed_after_normalization(self):
        for direct, parameter in (("USA", "美股"), ("CN", "A股")):
            strategy = {
                "market": direct,
                "parameters": {
                    "market": {"enabled": True, "value": parameter}
                },
                "exposure_policy": {"mode": "LONG_ONLY"},
            }

            validate_strategy_policies(strategy)

    def test_non_mapping_parameters_are_rejected_as_invalid_policy_input(self):
        for parameters in (None, "us", [], True):
            with self.subTest(parameters=parameters), self.assertRaisesRegex(
                StrategyPolicyError,
                "parameters|market|市场",
            ):
                validate_strategy_policies(
                    {
                        "parameters": parameters,
                        "exposure_policy": {"mode": "LONG_ONLY"},
                    }
                )

    def test_non_mapping_parameter_market_state_is_rejected(self):
        for state in (None, "us", [], True):
            with self.subTest(state=state), self.assertRaisesRegex(
                StrategyPolicyError,
                "parameters.market|market|市场",
            ):
                validate_strategy_policies(
                    {
                        "parameters": {"market": state},
                        "exposure_policy": {"mode": "LONG_ONLY"},
                    }
                )

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(StrategyPolicyError):
            normalize_exposure_policy({"mode": "UNLIMITED"})

    def test_invalid_numeric_policies_fall_back_to_safe_finite_values(self):
        exposure = normalize_exposure_policy(
            {
                "max_positions": -2,
                "max_gross_exposure_pct": math.nan,
                "max_net_exposure_pct": math.inf,
                "max_long_exposure_pct": -1,
                "max_short_exposure_pct": "invalid",
                "max_long_position_pct": -10,
                "max_short_position_pct": -10,
            }
        )
        margin = normalize_margin_policy(
            {
                "maintenance_margin_pct": -1,
                "liquidation_buffer_pct": math.nan,
                "financing_apr_pct": math.inf,
                "accrual_mode": "HOURLY",
            }
        )
        short = normalize_short_policy(
            {
                "estimated_borrow_apr_pct": -1,
                "cost_stress_multiplier": math.nan,
                "event_blackout_sessions": -2,
                "maximum_volatility_20d_pct": math.inf,
            }
        )

        for value in exposure.values():
            if isinstance(value, (int, float)):
                self.assertGreaterEqual(value, 0)
                self.assertTrue(math.isfinite(value))
        for policy in (margin, short):
            for value in policy.values():
                if isinstance(value, (int, float)):
                    self.assertGreaterEqual(value, 0)
                    self.assertTrue(math.isfinite(value))
        self.assertEqual(margin["accrual_mode"], "DAILY")

    def test_policy_dataclasses_are_frozen(self):
        for policy in (
            effective_exposure_policy(normalize_exposure_policy({})),
            MarginPolicy(**normalize_margin_policy({})),
            ShortPolicy(**normalize_short_policy({})),
        ):
            with self.subTest(type=type(policy).__name__), self.assertRaises(
                FrozenInstanceError
            ):
                policy.__setattr__(next(iter(policy.__dataclass_fields__)), None)

        self.assertIsInstance(
            effective_exposure_policy(normalize_exposure_policy({})), ExposurePolicy
        )


class PortfolioEngineContractTests(unittest.TestCase):
    def _intent(self, **updates):
        values = {
            "id": "intent-1",
            "symbol": "AAPL",
            "position_side": PositionSide.LONG,
            "order_side": OrderSide.BUY,
            "position_effect": PositionEffect.OPEN,
            "quantity": 1,
            "reason": "target",
            "created_snapshot_id": "market-1",
        }
        values.update(updates)
        return OrderIntent(**values)

    def test_enums_have_explicit_direction_values(self):
        self.assertEqual(PositionSide.LONG.value, "LONG")
        self.assertEqual(PositionSide.SHORT.value, "SHORT")
        self.assertEqual(OrderSide.BUY.value, "BUY")
        self.assertEqual(OrderSide.SELL.value, "SELL")
        self.assertEqual(PositionEffect.OPEN.value, "OPEN")
        self.assertEqual(PositionEffect.INCREASE.value, "INCREASE")
        self.assertEqual(PositionEffect.REDUCE.value, "REDUCE")
        self.assertEqual(PositionEffect.CLOSE.value, "CLOSE")

    def test_order_intent_requires_positive_integer_quantity(self):
        for quantity in (0, -1, 1.5, True):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                self._intent(quantity=quantity)

    def test_execution_fill_requires_positive_integer_quantity(self):
        for quantity in (0, -1, 1.5, False):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                ExecutionFill(
                    intent_id="intent-1",
                    symbol="AAPL",
                    quantity=quantity,
                    price=100.0,
                    fees=1.0,
                    status="FILLED",
                )

    def test_order_intent_risk_property_uses_position_effect(self):
        self.assertTrue(self._intent(position_effect=PositionEffect.OPEN).increases_risk)
        self.assertTrue(
            self._intent(position_effect=PositionEffect.INCREASE).increases_risk
        )
        self.assertFalse(self._intent(position_effect=PositionEffect.REDUCE).increases_risk)
        self.assertFalse(self._intent(position_effect=PositionEffect.CLOSE).increases_risk)

    def test_domain_dataclasses_are_frozen(self):
        now = datetime.now(timezone.utc)
        values = (
            SignalCandidate(
                symbol="AAPL",
                side=PositionSide.LONG,
                score=0.9,
                requested_weight_pct=10.0,
                model_id="long-v1",
                thesis_id="thesis-1",
            ),
            TargetPosition(
                symbol="AAPL",
                side=PositionSide.LONG,
                target_weight_pct=10.0,
                signal_score=0.9,
                model_id="long-v1",
                thesis_id="thesis-1",
            ),
            self._intent(),
            MarketSnapshot(id="market-1", occurred_at=now, quotes={}),
            ExecutionFill(
                intent_id="intent-1",
                symbol="AAPL",
                quantity=1,
                price=100.0,
                fees=1.0,
                status="FILLED",
            ),
            PortfolioEvent(id="event-1", type="FILL", occurred_at=now, data={}),
            DecisionBatch(
                run_key="run-1",
                strategy_id="strategy-1",
                strategy_revision=1,
                portfolio_snapshot_id="portfolio-1",
                market_snapshot_id="market-1",
            ),
        )

        for value in values:
            with self.subTest(type=type(value).__name__), self.assertRaises(
                FrozenInstanceError
            ):
                value.__setattr__(next(iter(value.__dataclass_fields__)), None)

    def test_mapping_defaults_are_not_shared(self):
        first = SignalCandidate(
            symbol="AAPL",
            side=PositionSide.LONG,
            score=0.9,
            requested_weight_pct=10.0,
            model_id="long-v1",
            thesis_id="thesis-1",
        )
        second = SignalCandidate(
            symbol="MSFT",
            side=PositionSide.LONG,
            score=0.8,
            requested_weight_pct=10.0,
            model_id="long-v1",
            thesis_id="thesis-2",
        )

        self.assertIsNot(first.facts, second.facts)

    def test_mapping_contracts_defensively_freeze_nested_values(self):
        facts = {
            "model": {
                "items": [
                    {"score": 0.9, "labels": {"trend", "quality"}},
                ]
            }
        }
        quotes = {
            "AAPL": {
                "price": 100.0,
                "levels": [{"bid": 99.0}],
            }
        }
        event_data = {"reasons": ["risk"], "details": {"level": "warning"}}
        candidate = SignalCandidate(
            symbol="AAPL",
            side=PositionSide.LONG,
            score=0.9,
            requested_weight_pct=10.0,
            model_id="long-v1",
            thesis_id="thesis-1",
            facts=facts,
        )
        snapshot = MarketSnapshot(
            id="market-1",
            occurred_at=datetime.now(timezone.utc),
            quotes=quotes,
        )
        event = PortfolioEvent(
            id="event-1",
            type="RISK",
            occurred_at=datetime.now(timezone.utc),
            data=event_data,
        )

        facts["model"]["items"][0]["score"] = 0.1
        facts["model"]["items"][0]["labels"].add("mutated")
        quotes["AAPL"]["price"] = 1.0
        quotes["AAPL"]["levels"][0]["bid"] = 1.0
        event_data["reasons"].append("mutated")
        event_data["details"]["level"] = "mutated"

        self.assertEqual(candidate.facts["model"]["items"][0]["score"], 0.9)
        self.assertNotIn(
            "mutated",
            candidate.facts["model"]["items"][0]["labels"],
        )
        self.assertEqual(snapshot.quotes["AAPL"]["price"], 100.0)
        self.assertEqual(snapshot.quotes["AAPL"]["levels"][0]["bid"], 99.0)
        self.assertEqual(event.data["reasons"], ("risk",))
        self.assertEqual(event.data["details"]["level"], "warning")
        with self.assertRaises(TypeError):
            candidate.facts["model"]["new"] = True
        with self.assertRaises(TypeError):
            snapshot.quotes["AAPL"]["price"] = 2.0
        with self.assertRaises(TypeError):
            event.data["details"]["level"] = "changed"

    def test_decision_batch_defensively_freezes_diagnostics_and_stage_outputs(self):
        diagnostics = [
            {
                "code": "SAFE",
                "details": {"reasons": ["bounded"], "tags": {"policy"}},
            }
        ]
        stage_facts = [{"kind": "signals", "items": [{"symbol": "AAPL"}]}]
        stage_diagnostics = [{"code": "STAGE_SAFE", "meta": {"items": [1]}}]
        output = StageOutput(
            stage="signal",
            component_version="1",
            facts=tuple(stage_facts),
            diagnostics=tuple(stage_diagnostics),
        )
        batch = DecisionBatch(
            run_key="run-1",
            strategy_id="strategy-1",
            strategy_revision=1,
            portfolio_snapshot_id="portfolio-1",
            market_snapshot_id="market-1",
            diagnostics=tuple(diagnostics),
            stage_outputs=(output,),
        )

        diagnostics[0]["code"] = "MUTATED"
        diagnostics[0]["details"]["reasons"].append("mutated")
        diagnostics[0]["details"]["tags"].add("mutated")
        stage_facts[0]["items"][0]["symbol"] = "MUTATED"
        stage_diagnostics[0]["meta"]["items"].append(2)

        self.assertEqual(batch.diagnostic_codes, ("SAFE",))
        self.assertEqual(
            batch.diagnostics[0]["details"]["reasons"],
            ("bounded",),
        )
        self.assertEqual(
            batch.diagnostics[0]["details"]["tags"],
            frozenset({"policy"}),
        )
        self.assertEqual(
            batch.stage_outputs[0].facts[0]["items"][0]["symbol"],
            "AAPL",
        )
        self.assertEqual(
            batch.stage_outputs[0].diagnostics[0]["meta"]["items"],
            (1,),
        )
        with self.assertRaises(TypeError):
            batch.diagnostics[0]["code"] = "CHANGED"
        with self.assertRaises(TypeError):
            batch.stage_outputs[0].facts[0]["kind"] = "changed"

    def test_decision_batch_reuses_stage_output_and_reports_codes(self):
        output = StageOutput(stage="signal", component_version="1")
        batch = DecisionBatch(
            run_key="run-1",
            strategy_id="strategy-1",
            strategy_revision=1,
            portfolio_snapshot_id="portfolio-1",
            market_snapshot_id="market-1",
            diagnostics=({"code": "BLOCKED"}, {"message": "informational"}),
            stage_outputs=(output,),
        )

        self.assertIsInstance(batch.stage_outputs[0], StageOutput)
        self.assertIsNot(batch.stage_outputs[0], output)
        self.assertEqual(batch.diagnostic_codes, ("BLOCKED",))


if __name__ == "__main__":
    unittest.main()
