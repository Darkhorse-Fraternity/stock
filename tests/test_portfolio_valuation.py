import copy
import math
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

from stock_recommender.portfolio_engine import contracts, valuation


REQUIRED_CONTRACTS = (
    "PositionSnapshot",
    "AccountSnapshot",
    "PortfolioMetrics",
    "ValuationResult",
)
REQUIRED_VALUATION_API = (
    "ValuationError",
    "position_market_value",
    "position_unrealized_pnl",
    "account_equity",
    "value_account",
)
VALUATION_API_AVAILABLE = all(
    hasattr(contracts, name) for name in REQUIRED_CONTRACTS
) and all(hasattr(valuation, name) for name in REQUIRED_VALUATION_API)


class PortfolioValuationApiRedTests(unittest.TestCase):
    def test_portfolio_valuation_contracts_and_functions_exist(self):
        missing = [
            f"contracts.{name}"
            for name in REQUIRED_CONTRACTS
            if not hasattr(contracts, name)
        ]
        missing.extend(
            f"valuation.{name}"
            for name in REQUIRED_VALUATION_API
            if not hasattr(valuation, name)
        )

        self.assertEqual([], missing)


@unittest.skipUnless(
    VALUATION_API_AVAILABLE,
    "valuation behavior requires the contract and function surface",
)
class PortfolioValuationTests(unittest.TestCase):
    NOW = datetime(2026, 8, 1, 9, 30, tzinfo=timezone.utc)

    def _position(self, **updates):
        values = {
            "symbol": "AAPL",
            "side": contracts.PositionSide.LONG,
            "quantity": 10,
            "average_cost": 100.0,
        }
        values.update(updates)
        return contracts.PositionSnapshot(**values)

    def _account(self, positions=(), **updates):
        values = {
            "id": "account-1",
            "strategy_id": "strategy-1",
            "strategy_revision": 1,
            "occurred_at": self.NOW,
            "available_cash": 100.0,
            "restricted_short_proceeds": 0.0,
            "margin_loan": 0.0,
            "accrued_financing_cost": 0.0,
            "accrued_borrow_cost": 0.0,
            "positions": positions,
        }
        values.update(updates)
        return contracts.AccountSnapshot(**values)

    def test_short_unrealized_pnl_moves_opposite_to_price(self):
        position = self._position(side=contracts.PositionSide.SHORT)
        account = self._account(positions=(position,))

        lower = valuation.value_account(account, {"AAPL": 80.0})
        higher = valuation.value_account(account, {"AAPL": 120.0})

        self.assertEqual(lower.positions[0].unrealized_pnl, 200.0)
        self.assertEqual(higher.positions[0].unrealized_pnl, -200.0)

    def test_long_unrealized_pnl_moves_with_price(self):
        position = self._position(side=contracts.PositionSide.LONG)
        account = self._account(positions=(position,))

        lower = valuation.value_account(account, {"AAPL": 80.0})
        higher = valuation.value_account(account, {"AAPL": 120.0})

        self.assertEqual(lower.positions[0].unrealized_pnl, -200.0)
        self.assertEqual(higher.positions[0].unrealized_pnl, 200.0)

    def test_position_market_value_is_positive_for_both_sides(self):
        for side in (contracts.PositionSide.LONG, contracts.PositionSide.SHORT):
            position = self._position(side=side, quantity=7)
            with self.subTest(side=side):
                self.assertEqual(
                    valuation.position_market_value(position, 123.0),
                    861.0,
                )

    def test_equity_and_gross_exposure_separate_short_liability(self):
        account = self._account(
            positions=(
                self._position(symbol="LONG", quantity=6),
                self._position(
                    symbol="SHORT",
                    side=contracts.PositionSide.SHORT,
                    quantity=9,
                ),
            ),
            available_cash=100.0,
            restricted_short_proceeds=1000.0,
            margin_loan=200.0,
        )

        result = valuation.value_account(account, {"LONG": 100.0, "SHORT": 100.0})

        self.assertEqual(result.metrics.long_market_value, 600.0)
        self.assertEqual(result.metrics.short_liability, 900.0)
        self.assertEqual(result.metrics.equity, 600.0)
        self.assertEqual(result.metrics.gross_exposure_pct, 250.0)

    def test_accrued_cost_is_statistical_and_not_deducted_twice(self):
        account = self._account(
            available_cash=992.0,
            accrued_financing_cost=8.0,
        )

        result = valuation.value_account(account, {})

        self.assertEqual(result.metrics.available_cash, 992.0)
        self.assertEqual(result.metrics.accrued_financing_cost, 8.0)
        self.assertEqual(result.metrics.equity, 992.0)

    def test_position_contract_rejects_invalid_quantity_cost_and_valued_price(self):
        for quantity in (1.5, True, "1", None):
            with self.subTest(field="quantity", value=quantity), self.assertRaisesRegex(
                TypeError,
                "integer",
            ):
                self._position(quantity=quantity)
        for quantity in (0, -1):
            with self.subTest(field="quantity", value=quantity), self.assertRaisesRegex(
                ValueError,
                "positive",
            ):
                self._position(quantity=quantity)
        for average_cost in (0, -1, True, "100", math.nan, math.inf, -math.inf):
            with self.subTest(
                field="average_cost", value=average_cost
            ), self.assertRaises((TypeError, ValueError)):
                self._position(average_cost=average_cost)
        for current_price in (0, -1, True, "100", math.nan, math.inf, -math.inf):
            with self.subTest(
                field="current_price", value=current_price
            ), self.assertRaises((TypeError, ValueError)):
                self._position(current_price=current_price)

    def test_position_snapshot_has_one_valuation_source_and_derived_properties(self):
        with self.assertRaises(TypeError):
            self._position(
                current_price=110.0,
                market_value=999.0,
                unrealized_pnl=-777.0,
            )

        unvalued = self._position()
        self.assertIsNone(unvalued.market_value)
        self.assertIsNone(unvalued.unrealized_pnl)

        long = self._position(current_price=110.0)
        short = self._position(
            side=contracts.PositionSide.SHORT,
            current_price=110.0,
        )
        self.assertEqual(long.market_value, 1100.0)
        self.assertEqual(short.market_value, 1100.0)
        self.assertEqual(long.unrealized_pnl, 100.0)
        self.assertEqual(short.unrealized_pnl, -100.0)

    def test_account_rejects_invalid_balances_and_costs(self):
        nonnegative_fields = (
            "restricted_short_proceeds",
            "margin_loan",
            "accrued_financing_cost",
            "accrued_borrow_cost",
        )
        for field_name in nonnegative_fields:
            for value in (-1, True, "1", math.nan, math.inf, -math.inf):
                with self.subTest(field=field_name, value=value), self.assertRaises(
                    (TypeError, ValueError)
                ):
                    self._account(**{field_name: value})

    def test_available_cash_can_be_negative_but_must_be_a_real_finite_number(self):
        account = self._account(available_cash=-25.5)
        self.assertEqual(account.available_cash, -25.5)

        for value in (True, "-25.5", math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                self._account(available_cash=value)

    def test_account_rejects_duplicate_or_opposite_side_symbol(self):
        duplicate = self._position(symbol="AAPL")
        for second in (
            self._position(symbol="AAPL"),
            self._position(symbol="AAPL", side=contracts.PositionSide.SHORT),
        ):
            with self.subTest(side=second.side), self.assertRaises(ValueError):
                self._account(positions=(duplicate, second))

    def test_account_copies_positions_to_an_immutable_tuple(self):
        source = [self._position()]
        account = self._account(positions=source)
        source.clear()

        self.assertIsInstance(account.positions, tuple)
        self.assertEqual(len(account.positions), 1)
        self.assertIs(copy.deepcopy(account), account)

    def test_contracts_are_frozen_and_validate_collection_item_types(self):
        position = self._position()
        account = self._account(positions=(position,))
        result = valuation.value_account(account, {"AAPL": 100.0})
        for value in (position, account, result.positions[0], result.metrics, result):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(
                FrozenInstanceError
            ):
                value.__setattr__(next(iter(value.__dataclass_fields__)), None)
        for positions in ("AAPL", {"AAPL": position}, (object(),)):
            with self.subTest(positions=positions), self.assertRaises(TypeError):
                self._account(positions=positions)

    def test_valuation_result_rejects_metrics_from_another_account(self):
        account = self._account(available_cash=100.0)
        other_metrics = contracts.PortfolioMetrics(
            available_cash=200.0,
            restricted_short_proceeds=0.0,
            margin_loan=0.0,
            accrued_financing_cost=0.0,
            accrued_borrow_cost=0.0,
            long_market_value=0.0,
            short_liability=0.0,
            equity=200.0,
            long_exposure_pct=0.0,
            short_exposure_pct=0.0,
            gross_exposure_pct=0.0,
            net_exposure_pct=0.0,
            margin_rate_pct=math.inf,
        )

        with self.assertRaisesRegex(ValueError, "available_cash"):
            contracts.ValuationResult(account=account, metrics=other_metrics)

    def test_valuation_result_checks_every_account_metric_balance_field(self):
        account = self._account(available_cash=100.0)
        metrics = valuation.value_account(account, {}).metrics
        mismatches = {
            "available_cash": 101.0,
            "restricted_short_proceeds": 1.0,
            "margin_loan": 1.0,
            "accrued_financing_cost": 1.0,
            "accrued_borrow_cost": 1.0,
        }

        for field_name, value in mismatches.items():
            with self.subTest(field=field_name), self.assertRaisesRegex(
                ValueError,
                field_name,
            ):
                contracts.ValuationResult(
                    account=self._account(**{field_name: value}),
                    positions=(),
                    metrics=metrics,
                )

    def test_valuation_result_copies_and_validates_valued_positions(self):
        account = self._account(positions=(self._position(),))
        valued = valuation.value_account(account, {"AAPL": 110.0})
        source = list(valued.positions)

        result = contracts.ValuationResult(
            account=account,
            positions=source,
            metrics=valued.metrics,
        )
        source.clear()

        self.assertIsInstance(result.positions, tuple)
        self.assertEqual(len(result.positions), 1)
        for positions in ("AAPL", {"AAPL": valued.positions[0]}, (object(),)):
            with self.subTest(positions=positions), self.assertRaises(TypeError):
                contracts.ValuationResult(
                    account=account,
                    positions=positions,
                    metrics=valued.metrics,
                )
        with self.assertRaisesRegex(ValueError, "current_price|position"):
            contracts.ValuationResult(
                account=account,
                positions=account.positions,
                metrics=valued.metrics,
            )

    def test_valuation_result_matches_position_identity_in_stable_order(self):
        account = self._account(
            positions=(
                self._position(symbol="AAPL"),
                self._position(
                    symbol="MSFT",
                    side=contracts.PositionSide.SHORT,
                ),
            )
        )
        valued = valuation.value_account(account, {"AAPL": 110.0, "MSFT": 90.0})

        mismatched_positions = (
            tuple(reversed(valued.positions)),
            (
                self._position(symbol="OTHER", current_price=110.0),
                valued.positions[1],
            ),
            (
                self._position(
                    symbol="AAPL",
                    side=contracts.PositionSide.SHORT,
                    current_price=110.0,
                ),
                valued.positions[1],
            ),
            (
                self._position(symbol="AAPL", quantity=11, current_price=110.0),
                valued.positions[1],
            ),
            (
                self._position(symbol="AAPL", average_cost=101.0, current_price=110.0),
                valued.positions[1],
            ),
        )
        for positions in mismatched_positions:
            with self.subTest(positions=positions), self.assertRaisesRegex(
                ValueError,
                "position",
            ):
                contracts.ValuationResult(
                    account=account,
                    positions=positions,
                    metrics=valued.metrics,
                )

    def test_valuation_result_allows_old_account_price_but_checks_derived_values(self):
        account = self._account(
            positions=(self._position(current_price=90.0),)
        )
        current = valuation.value_account(account, {"AAPL": 110.0})
        result = contracts.ValuationResult(
            account=account,
            positions=current.positions,
            metrics=current.metrics,
        )
        self.assertEqual(result.positions[0].current_price, 110.0)

        other_valuation = valuation.value_account(account, {"AAPL": 120.0})
        with self.assertRaisesRegex(ValueError, "long_market_value"):
            contracts.ValuationResult(
                account=account,
                positions=current.positions,
                metrics=other_valuation.metrics,
            )

    def test_valuation_result_accepts_empty_account_and_positions(self):
        account = self._account()
        metrics = valuation.value_account(account, {}).metrics

        result = contracts.ValuationResult(account=account, metrics=metrics)

        self.assertEqual(result.positions, ())

    def test_metrics_contract_allows_only_the_defined_infinity_patterns(self):
        values = {
            "available_cash": 100.0,
            "restricted_short_proceeds": 0.0,
            "margin_loan": 0.0,
            "accrued_financing_cost": 0.0,
            "accrued_borrow_cost": 0.0,
            "long_market_value": 0.0,
            "short_liability": 0.0,
            "equity": 100.0,
            "long_exposure_pct": 0.0,
            "short_exposure_pct": 0.0,
            "gross_exposure_pct": 0.0,
            "net_exposure_pct": 0.0,
            "margin_rate_pct": math.inf,
        }
        self.assertEqual(
            contracts.PortfolioMetrics(**values).margin_rate_pct,
            math.inf,
        )

        invalid_updates = (
            {"gross_exposure_pct": math.inf},
            {"margin_rate_pct": -math.inf},
            {"net_exposure_pct": math.nan},
            {
                "long_market_value": 100.0,
                "equity": 0.0,
                "long_exposure_pct": -math.inf,
                "gross_exposure_pct": math.inf,
                "net_exposure_pct": math.inf,
                "margin_rate_pct": 0.0,
            },
        )
        for updates in invalid_updates:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                contracts.PortfolioMetrics(**(values | updates))

    def test_metrics_contract_rejects_finite_accounting_and_ratio_conflicts(self):
        values = {
            "available_cash": 100.0,
            "restricted_short_proceeds": 1000.0,
            "margin_loan": 200.0,
            "accrued_financing_cost": 8.0,
            "accrued_borrow_cost": 3.0,
            "long_market_value": 600.0,
            "short_liability": 900.0,
            "equity": 600.0,
            "long_exposure_pct": 100.0,
            "short_exposure_pct": 150.0,
            "gross_exposure_pct": 250.0,
            "net_exposure_pct": -50.0,
            "margin_rate_pct": 40.0,
        }
        self.assertEqual(contracts.PortfolioMetrics(**values).equity, 600.0)

        invalid_updates = (
            {"equity": 600.01},
            {"long_exposure_pct": 100.01},
            {"short_exposure_pct": 150.01},
            {"gross_exposure_pct": 250.01},
            {"net_exposure_pct": -50.01},
            {"margin_rate_pct": 40.01},
        )
        for updates in invalid_updates:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                contracts.PortfolioMetrics(**(values | updates))

    def test_metrics_contract_accepts_exact_invariants_for_all_account_states(self):
        common = {
            "restricted_short_proceeds": 0.0,
            "margin_loan": 0.0,
            "accrued_financing_cost": 7.0,
            "accrued_borrow_cost": 2.0,
            "short_liability": 0.0,
            "short_exposure_pct": 0.0,
        }
        cases = (
            {
                "available_cash": 1000.0,
                "long_market_value": 1000.0,
                "equity": 2000.0,
                "long_exposure_pct": 50.0,
                "gross_exposure_pct": 50.0,
                "net_exposure_pct": 50.0,
                "margin_rate_pct": 200.0,
            },
            {
                "available_cash": -2000.0,
                "long_market_value": 1000.0,
                "equity": -1000.0,
                "long_exposure_pct": -100.0,
                "gross_exposure_pct": -100.0,
                "net_exposure_pct": -100.0,
                "margin_rate_pct": -100.0,
            },
            {
                "available_cash": -1000.0,
                "long_market_value": 1000.0,
                "equity": 0.0,
                "long_exposure_pct": math.inf,
                "gross_exposure_pct": math.inf,
                "net_exposure_pct": math.inf,
                "margin_rate_pct": 0.0,
            },
            {
                "available_cash": 100.0,
                "long_market_value": 0.0,
                "equity": 100.0,
                "long_exposure_pct": 0.0,
                "gross_exposure_pct": 0.0,
                "net_exposure_pct": 0.0,
                "margin_rate_pct": math.inf,
            },
        )
        for values in cases:
            with self.subTest(equity=values["equity"]):
                metrics = contracts.PortfolioMetrics(**(common | values))
                self.assertEqual(metrics.equity, values["equity"])

    def test_metrics_contract_rejects_overflowing_base_arithmetic(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            contracts.PortfolioMetrics(
                available_cash=1e308,
                restricted_short_proceeds=1e308,
                margin_loan=0.0,
                accrued_financing_cost=0.0,
                accrued_borrow_cost=0.0,
                long_market_value=0.0,
                short_liability=0.0,
                equity=0.0,
                long_exposure_pct=0.0,
                short_exposure_pct=0.0,
                gross_exposure_pct=0.0,
                net_exposure_pct=0.0,
                margin_rate_pct=math.inf,
            )

    def test_metrics_contract_rejects_near_zero_equity_with_infinite_exposure(self):
        with self.assertRaises(ValueError):
            contracts.PortfolioMetrics(
                available_cash=-1000.0,
                restricted_short_proceeds=0.0,
                margin_loan=0.0,
                accrued_financing_cost=0.0,
                accrued_borrow_cost=0.0,
                long_market_value=1000.0,
                short_liability=0.0,
                equity=5e-13,
                long_exposure_pct=math.inf,
                short_exposure_pct=0.0,
                gross_exposure_pct=math.inf,
                net_exposure_pct=math.inf,
                margin_rate_pct=0.0,
            )

    def test_metrics_contract_rejects_near_zero_exposure_without_positions(self):
        with self.assertRaises(ValueError):
            contracts.PortfolioMetrics(
                available_cash=100.0,
                restricted_short_proceeds=0.0,
                margin_loan=0.0,
                accrued_financing_cost=0.0,
                accrued_borrow_cost=0.0,
                long_market_value=0.0,
                short_liability=0.0,
                equity=100.0,
                long_exposure_pct=5e-13,
                short_exposure_pct=0.0,
                gross_exposure_pct=0.0,
                net_exposure_pct=-5e-13,
                margin_rate_pct=math.inf,
            )

    def test_prices_must_cover_every_position_and_extra_prices_are_ignored(self):
        account = self._account(
            positions=(
                self._position(symbol="AAPL"),
                self._position(symbol="MSFT"),
            )
        )

        with self.assertRaisesRegex(valuation.ValuationError, "MSFT"):
            valuation.value_account(account, {"AAPL": 100.0})

        expected = valuation.value_account(account, {"AAPL": 100.0, "MSFT": 200.0})
        actual = valuation.value_account(
            account,
            {"AAPL": 100.0, "MSFT": 200.0, "IGNORED": "not-a-price"},
        )
        self.assertEqual(actual, expected)

    def test_required_prices_must_be_real_finite_positive_numbers(self):
        account = self._account(positions=(self._position(),))
        for price in (0, -1, True, "100", math.nan, math.inf, -math.inf):
            with self.subTest(price=price), self.assertRaises(
                valuation.ValuationError
            ):
                valuation.value_account(account, {"AAPL": price})

    def test_valuation_is_pure_stable_and_updates_only_the_returned_copy(self):
        positions = (self._position(),)
        account = self._account(positions=positions)
        prices = {"AAPL": 125.0}
        account_before = copy.deepcopy(account)
        prices_before = copy.deepcopy(prices)

        first = valuation.value_account(account, prices)
        second = valuation.value_account(account, prices)

        self.assertEqual(first, second)
        self.assertEqual(account, account_before)
        self.assertEqual(prices, prices_before)
        self.assertIsNone(account.positions[0].current_price)
        self.assertIsNone(account.positions[0].market_value)
        self.assertIsNone(account.positions[0].unrealized_pnl)
        self.assertEqual(first.positions[0].current_price, 125.0)
        self.assertEqual(first.positions[0].market_value, 1250.0)
        self.assertEqual(first.positions[0].unrealized_pnl, 250.0)

    def test_exposure_and_margin_formulas_are_exact(self):
        account = self._account(
            positions=(
                self._position(symbol="LONG", quantity=6),
                self._position(
                    symbol="SHORT",
                    side=contracts.PositionSide.SHORT,
                    quantity=3,
                ),
            ),
            available_cash=700.0,
        )
        metrics = valuation.value_account(
            account, {"LONG": 100.0, "SHORT": 100.0}
        ).metrics
        equity = 1000.0

        self.assertEqual(metrics.equity, equity)
        self.assertEqual(metrics.long_exposure_pct, 600.0 / equity * 100.0)
        self.assertEqual(metrics.short_exposure_pct, 300.0 / equity * 100.0)
        self.assertEqual(metrics.gross_exposure_pct, 900.0 / equity * 100.0)
        self.assertEqual(metrics.net_exposure_pct, 300.0 / equity * 100.0)
        self.assertEqual(metrics.margin_rate_pct, equity / 900.0 * 100.0)

    def test_no_positions_has_positive_zero_exposures_and_infinite_margin_rate(self):
        metrics = valuation.value_account(self._account(), {}).metrics

        for name in (
            "long_market_value",
            "short_liability",
            "long_exposure_pct",
            "short_exposure_pct",
            "gross_exposure_pct",
            "net_exposure_pct",
        ):
            value = getattr(metrics, name)
            self.assertEqual(value, 0.0)
            self.assertEqual(math.copysign(1.0, value), 1.0)
        self.assertEqual(metrics.margin_rate_pct, math.inf)

    def test_zero_equity_with_positions_uses_defined_infinities(self):
        cases = (
            (
                self._account(
                    positions=(self._position(),),
                    available_cash=-1000.0,
                ),
                {"AAPL": 100.0},
                math.inf,
            ),
            (
                self._account(
                    positions=(
                        self._position(side=contracts.PositionSide.SHORT),
                    ),
                    available_cash=1000.0,
                ),
                {"AAPL": 100.0},
                -math.inf,
            ),
            (
                self._account(
                    positions=(
                        self._position(symbol="LONG"),
                        self._position(
                            symbol="SHORT",
                            side=contracts.PositionSide.SHORT,
                        ),
                    ),
                    available_cash=0.0,
                ),
                {"LONG": 100.0, "SHORT": 100.0},
                0.0,
            ),
        )
        for account, prices, expected_net in cases:
            with self.subTest(expected_net=expected_net):
                metrics = valuation.value_account(account, prices).metrics
                self.assertEqual(metrics.equity, 0.0)
                self.assertEqual(metrics.gross_exposure_pct, math.inf)
                self.assertEqual(metrics.net_exposure_pct, expected_net)
                self.assertEqual(metrics.margin_rate_pct, 0.0)
                self.assertFalse(
                    any(
                        math.isnan(getattr(metrics, name))
                        for name in metrics.__dataclass_fields__
                    )
                )

    def test_negative_equity_metrics_follow_the_same_formulas_without_nan(self):
        account = self._account(
            positions=(self._position(),),
            available_cash=-2000.0,
        )

        metrics = valuation.value_account(account, {"AAPL": 100.0}).metrics

        self.assertEqual(metrics.equity, -1000.0)
        self.assertEqual(metrics.long_exposure_pct, -100.0)
        self.assertEqual(metrics.short_exposure_pct, 0.0)
        self.assertEqual(metrics.gross_exposure_pct, -100.0)
        self.assertEqual(metrics.net_exposure_pct, -100.0)
        self.assertEqual(metrics.margin_rate_pct, -100.0)
        self.assertFalse(
            any(
                math.isnan(getattr(metrics, name))
                for name in metrics.__dataclass_fields__
            )
        )

    def test_direct_equity_formula_does_not_deduct_accrued_costs(self):
        account = self._account(
            available_cash=100.0,
            restricted_short_proceeds=1000.0,
            margin_loan=200.0,
            accrued_financing_cost=8.0,
            accrued_borrow_cost=3.0,
        )

        self.assertEqual(
            valuation.account_equity(
                account,
                long_market_value=600.0,
                short_liability=900.0,
            ),
            600.0,
        )

    def test_128_deterministic_long_short_combinations_preserve_identities(self):
        for index in range(1, 129):
            quantity = index % 17 + 1
            average_cost = float(40 + index % 31)
            price = float(25 + index * 0.75)
            long = self._position(
                symbol=f"LONG-{index}",
                quantity=quantity,
                average_cost=average_cost,
            )
            short = self._position(
                symbol=f"SHORT-{index}",
                side=contracts.PositionSide.SHORT,
                quantity=quantity,
                average_cost=average_cost,
            )
            account = self._account(
                positions=(long, short),
                available_cash=10000.0 + index,
                restricted_short_proceeds=price * quantity,
                margin_loan=float(index % 13),
            )
            prices = {long.symbol: price, short.symbol: price}
            original_account = copy.deepcopy(account)
            original_prices = copy.deepcopy(prices)

            result = valuation.value_account(account, prices)
            long_value, short_value = (
                position.market_value for position in result.positions
            )
            long_pnl, short_pnl = (
                position.unrealized_pnl for position in result.positions
            )
            metrics = result.metrics
            expected_equity = (
                account.available_cash
                + account.restricted_short_proceeds
                + long_value
                - short_value
                - account.margin_loan
            )

            with self.subTest(index=index):
                self.assertEqual(long_pnl, -short_pnl)
                self.assertEqual(metrics.equity, expected_equity)
                self.assertAlmostEqual(
                    metrics.gross_exposure_pct,
                    metrics.long_exposure_pct + metrics.short_exposure_pct,
                )
                self.assertAlmostEqual(
                    metrics.net_exposure_pct,
                    metrics.long_exposure_pct - metrics.short_exposure_pct,
                )
                self.assertEqual(account, original_account)
                self.assertEqual(prices, original_prices)


if __name__ == "__main__":
    unittest.main()
