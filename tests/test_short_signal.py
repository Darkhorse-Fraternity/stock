import math
import unittest

from stock_recommender.portfolio_engine.config import ShortPolicy
from stock_recommender.portfolio_engine.contracts import PositionSide
from stock_recommender.portfolio_engine.short_signal import ShortTrendBreakdownV1
from stock_recommender.portfolio_engine.signal_ports import (
    SIGNAL_MODELS,
    FactorRankLongAdapter,
    get_signal_model,
    register_signal_model,
)


def make_row(**updates):
    row = {
        "symbol": "TEST",
        "momentum20": -0.12,
        "momentum60": -0.25,
        "price": 70.0,
        "ma20": 80.0,
        "ma60": 95.0,
        "volatility20": 0.45,
        "turnover": 50_000_000.0,
        "one_day_return": -0.03,
        "cutoff_date": "2026-07-30",
    }
    row.update(updates)
    return row


class ShortTrendBreakdownTests(unittest.TestCase):
    def test_admits_persistent_liquid_breakdown(self):
        row = make_row(
            symbol="WEAK",
            momentum20=-0.12,
            momentum60=-0.25,
            price=70,
            ma20=80,
            ma60=95,
            volatility20=0.45,
            turnover=50_000_000,
            one_day_return=-0.03,
        )

        signals = ShortTrendBreakdownV1().evaluate(
            [row], event_calendar={"WEAK": None}
        )

        self.assertIsInstance(signals, tuple)
        self.assertEqual(
            [(item.symbol, item.side.value) for item in signals],
            [("WEAK", "SHORT")],
        )

    def test_does_not_chase_single_day_crash(self):
        row = make_row(momentum20=-0.12, momentum60=-0.25, one_day_return=-0.14)

        self.assertEqual(
            ShortTrendBreakdownV1().evaluate(
                [row], event_calendar={"TEST": None}
            ),
            (),
        )

    def test_blocks_event_window_and_extreme_volatility(self):
        event_row = make_row(symbol="EVENT")
        volatile_row = make_row(symbol="VOL", volatility20=0.81)

        signals = ShortTrendBreakdownV1().evaluate(
            [event_row, volatile_row], event_calendar={"EVENT": 1, "VOL": None}
        )

        self.assertEqual(signals, ())

    def test_requires_both_negative_momenta(self):
        for field in ("momentum20", "momentum60"):
            for value in (0.0, 0.01):
                with self.subTest(field=field, value=value):
                    row = make_row(**{field: value})
                    self.assertEqual(
                        ShortTrendBreakdownV1().evaluate(
                            [row], event_calendar={"TEST": None}
                        ),
                        (),
                    )

    def test_requires_price_strictly_below_both_moving_averages(self):
        rows = (
            make_row(symbol="AT20", price=80, ma20=80, ma60=95),
            make_row(symbol="ABOVE20", price=81, ma20=80, ma60=95),
            make_row(symbol="AT60", price=95, ma20=100, ma60=95),
            make_row(symbol="ABOVE60", price=96, ma20=100, ma60=95),
        )

        signals = ShortTrendBreakdownV1().evaluate(
            rows,
            event_calendar={row["symbol"]: None for row in rows},
        )

        self.assertEqual(signals, ())

    def test_turnover_floor_is_inclusive_at_twenty_million_usd(self):
        below = make_row(symbol="BELOW", turnover=19_999_999.99)
        boundary = make_row(symbol="BOUNDARY", turnover=20_000_000)

        signals = ShortTrendBreakdownV1().evaluate(
            [below, boundary],
            event_calendar={"BELOW": None, "BOUNDARY": None},
        )

        self.assertEqual([item.symbol for item in signals], ["BOUNDARY"])

    def test_event_calendar_distinguishes_missing_no_event_and_distance(self):
        model = ShortTrendBreakdownV1()
        for distance in (0, 1, 2):
            with self.subTest(distance=distance):
                self.assertEqual(
                    model.evaluate(
                        [make_row()], event_calendar={"TEST": distance}
                    ),
                    (),
                )

        self.assertEqual(model.evaluate([make_row()], event_calendar={}), ())
        self.assertEqual(
            [
                item.symbol
                for item in model.evaluate(
                    [make_row(symbol="CLEAR"), make_row(symbol="LATER")],
                    event_calendar={"CLEAR": None, "LATER": 3},
                )
            ],
            ["CLEAR", "LATER"],
        )

    def test_one_day_return_must_be_strictly_greater_than_minus_ten_percent(self):
        model = ShortTrendBreakdownV1()
        for one_day_return in (-0.100001, -0.10):
            with self.subTest(one_day_return=one_day_return):
                self.assertEqual(
                    model.evaluate(
                        [make_row(one_day_return=one_day_return)],
                        event_calendar={"TEST": None},
                    ),
                    (),
                )

        self.assertEqual(
            len(
                model.evaluate(
                    [make_row(one_day_return=-0.099999)],
                    event_calendar={"TEST": None},
                )
            ),
            1,
        )

    def test_volatility_ceiling_is_inclusive_and_converts_policy_percent(self):
        default_model = ShortTrendBreakdownV1()
        self.assertEqual(
            len(
                default_model.evaluate(
                    [make_row(volatility20=0.80)],
                    event_calendar={"TEST": None},
                )
            ),
            1,
        )
        self.assertEqual(
            default_model.evaluate(
                [make_row(volatility20=math.nextafter(0.80, math.inf))],
                event_calendar={"TEST": None},
            ),
            (),
        )

        policy_model = ShortTrendBreakdownV1(
            policy=ShortPolicy(maximum_volatility_20d_pct=40.0)
        )
        self.assertEqual(
            len(
                policy_model.evaluate(
                    [make_row(volatility20=0.40)],
                    event_calendar={"TEST": None},
                )
            ),
            1,
        )
        self.assertEqual(
            policy_model.evaluate(
                [make_row(volatility20=0.400001)],
                event_calendar={"TEST": None},
            ),
            (),
        )

    def test_ranking_ids_and_limit_are_deterministic(self):
        rows = [
            make_row(
                symbol=f"S{index:02d}",
                momentum20=-0.02 - index * 0.005,
                momentum60=-0.04 - index * 0.009,
                price=75 - index * 0.5,
                volatility20=0.20 + index * 0.02,
                turnover=20_000_000 + index * 4_000_000,
            )
            for index in range(12)
        ]
        calendar = {row["symbol"]: None for row in rows}
        model = ShortTrendBreakdownV1()

        forward = model.evaluate(rows, event_calendar=calendar)
        reverse = model.evaluate(reversed(rows), event_calendar=calendar)

        fingerprint = lambda items: [
            (item.symbol, item.score, item.thesis_id) for item in items
        ]
        self.assertEqual(fingerprint(forward), fingerprint(reverse))
        self.assertEqual(len(forward), 10)
        self.assertTrue(
            all(item.requested_weight_pct == 5.0 for item in forward)
        )
        for item in forward:
            self.assertEqual(item.side, PositionSide.SHORT)
            self.assertIn("short_trend_breakdown_v1", item.thesis_id)
            self.assertIn(item.symbol, item.thesis_id)
            self.assertIn("2026-07-30", item.thesis_id)
            self.assertEqual(item.facts["ranking_score"], item.score)
            self.assertEqual(item.facts["cutoff_date"], "2026-07-30")
            self.assertIn("ranking_components", item.facts)

    def test_ties_break_by_symbol_independently_of_input_order(self):
        rows = [make_row(symbol=symbol) for symbol in ("ZZZ", "AAA", "MMM")]
        calendar = {row["symbol"]: None for row in rows}

        forward = ShortTrendBreakdownV1().evaluate(
            rows, event_calendar=calendar
        )
        reverse = ShortTrendBreakdownV1().evaluate(
            reversed(rows), event_calendar=calendar
        )

        self.assertEqual([item.symbol for item in forward], ["AAA", "MMM", "ZZZ"])
        self.assertEqual(
            [item.symbol for item in forward], [item.symbol for item in reverse]
        )

    def test_invalid_required_numbers_and_cutoff_are_safely_rejected(self):
        numeric_fields = (
            "momentum20",
            "momentum60",
            "price",
            "ma20",
            "ma60",
            "volatility20",
            "turnover",
            "one_day_return",
        )
        invalid_values = (None, math.nan, math.inf, -math.inf, "invalid", object())
        model = ShortTrendBreakdownV1()
        for field in numeric_fields:
            for value in invalid_values:
                with self.subTest(field=field, value=repr(value)):
                    self.assertEqual(
                        model.evaluate(
                            [make_row(**{field: value})],
                            event_calendar={"TEST": None},
                        ),
                        (),
                    )
        for cutoff in (None, "", "not-a-date", object()):
            with self.subTest(cutoff=repr(cutoff)):
                self.assertEqual(
                    model.evaluate(
                        [make_row(cutoff_date=cutoff)],
                        event_calendar={"TEST": None},
                    ),
                    (),
                )

    def test_parseable_numeric_strings_and_booleans_are_safely_rejected(self):
        parseable_strings = {
            "momentum20": "-0.12",
            "momentum60": "-0.25",
            "price": "70",
            "ma20": "80.0",
            "ma60": "95.0",
            "volatility20": "0.45",
            "turnover": "50000000",
            "one_day_return": "-0.03",
        }
        model = ShortTrendBreakdownV1()
        for field, value in parseable_strings.items():
            with self.subTest(field=field, kind="numeric_string"):
                self.assertEqual(
                    model.evaluate(
                        [make_row(**{field: value})],
                        event_calendar={"TEST": None},
                    ),
                    (),
                )
        for field in parseable_strings:
            for value in (False, True):
                with self.subTest(field=field, value=value, kind="bool"):
                    self.assertEqual(
                        model.evaluate(
                            [make_row(**{field: value})],
                            event_calendar={"TEST": None},
                        ),
                        (),
                    )

    def test_overflowing_integers_are_safely_rejected_for_required_numbers(self):
        numeric_fields = (
            "momentum20",
            "momentum60",
            "price",
            "ma20",
            "ma60",
            "volatility20",
            "turnover",
            "one_day_return",
        )
        model = ShortTrendBreakdownV1()
        for field in numeric_fields:
            for sign, value in (("positive", 10**10000), ("negative", -(10**10000))):
                with self.subTest(field=field, sign=sign):
                    try:
                        signals = model.evaluate(
                            [make_row(**{field: value})],
                            event_calendar={"TEST": None},
                        )
                    except Exception as exc:
                        self.fail(
                            f"{field} {sign} overflow raised "
                            f"{type(exc).__name__}"
                        )
                    self.assertEqual(signals, ())

    def test_invalid_rows_do_not_change_deterministic_valid_result(self):
        valid = make_row(symbol="VALID")
        invalid = make_row(symbol="INVALID", momentum20=math.nan)
        calendar = {"VALID": None, "INVALID": None}
        model = ShortTrendBreakdownV1()

        first = model.evaluate([invalid, valid], event_calendar=calendar)
        second = model.evaluate([valid, invalid], event_calendar=calendar)

        self.assertEqual(
            [(item.symbol, item.score, item.thesis_id) for item in first],
            [(item.symbol, item.score, item.thesis_id) for item in second],
        )

    def test_duplicate_symbol_tie_selects_latest_cutoff_independent_of_order(self):
        older = make_row(
            symbol="DUP",
            cutoff_date="2026-07-29",
            momentum20=-0.20,
            momentum60=-0.30,
            price=60.0,
            ma20=90.0,
            ma60=100.0,
            volatility20=0.70,
            turnover=20_000_000,
        )
        newer = make_row(
            symbol="DUP",
            cutoff_date="2026-07-30",
            momentum20=-0.05,
            momentum60=-0.10,
            price=80.0,
            ma20=90.0,
            ma60=100.0,
            volatility20=0.20,
            turnover=60_000_000,
        )
        model = ShortTrendBreakdownV1()

        forward = model.evaluate(
            [older, newer], event_calendar={"DUP": None}
        )
        reverse = model.evaluate(
            [newer, older], event_calendar={"DUP": None}
        )

        self.assertEqual(len(forward), 1)
        self.assertEqual(forward[0].score, 0.5)
        self.assertEqual(forward[0].facts["cutoff_date"], "2026-07-30")
        self.assertEqual(forward[0].symbol, reverse[0].symbol)
        self.assertEqual(forward[0].thesis_id, reverse[0].thesis_id)
        self.assertEqual(forward[0].facts, reverse[0].facts)


class SignalRegistryTests(unittest.TestCase):
    def test_duplicate_model_id_is_rejected_without_overwriting_original(self):
        class DummyModel:
            model_id = "test_only_duplicate_guard"
            side = PositionSide.SHORT

            def evaluate(self, rows, event_calendar):
                return ()

        first = DummyModel()
        second = DummyModel()
        try:
            register_signal_model(first)
            with self.assertRaisesRegex(
                ValueError,
                "重复信号模型：test_only_duplicate_guard",
            ):
                register_signal_model(second)
            self.assertIs(SIGNAL_MODELS[first.model_id], first)
        finally:
            SIGNAL_MODELS.pop(first.model_id, None)

    def test_registry_contains_short_trend_model(self):
        model = get_signal_model("short_trend_breakdown_v1")

        self.assertEqual(model.model_id, "short_trend_breakdown_v1")
        self.assertEqual(model.side, PositionSide.SHORT)


class FactorRankLongAdapterTests(unittest.TestCase):
    def test_preserves_existing_selection_order_score_and_facts(self):
        selections = (
            {
                "symbol": "LOWER_FIRST",
                "score": 31.25,
                "requested_weight_pct": 7.0,
                "thesis_id": "existing-thesis-1",
                "name": "Lower score deliberately first",
                "reasons": ["fact one"],
            },
            {
                "symbol": "HIGHER_SECOND",
                "score": 98.5,
                "requested_weight_pct": 8.0,
                "thesis_id": "existing-thesis-2",
                "name": "Higher score deliberately second",
                "reasons": ["fact two"],
            },
        )

        signals = FactorRankLongAdapter().evaluate(
            selections, event_calendar={}
        )

        self.assertIsInstance(signals, tuple)
        self.assertEqual(
            [item.symbol for item in signals],
            ["LOWER_FIRST", "HIGHER_SECOND"],
        )
        self.assertEqual([item.score for item in signals], [31.25, 98.5])
        self.assertTrue(all(item.side == PositionSide.LONG for item in signals))
        self.assertTrue(
            all(item.model_id == "factor_rank_v1" for item in signals)
        )
        self.assertEqual(signals[0].requested_weight_pct, 7.0)
        self.assertEqual(signals[1].requested_weight_pct, 8.0)
        self.assertEqual(signals[0].thesis_id, "existing-thesis-1")
        self.assertEqual(signals[1].thesis_id, "existing-thesis-2")
        self.assertEqual(
            signals[0].facts["name"], "Lower score deliberately first"
        )
        self.assertEqual(signals[0].facts["reasons"], ("fact one",))


if __name__ == "__main__":
    unittest.main()
