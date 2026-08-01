from __future__ import annotations

import math
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from stock_recommender.backtest import evaluate_approval_gate, run_walk_forward_backtest
from stock_recommender.parameters import load_strategy_config
from stock_recommender.portfolio_backtest import (
    EngineReplayFrame,
    HistoricalBorrowBook,
    replay_engine_frames,
)
from stock_recommender.portfolio_engine.config import (
    default_exposure_policy,
    default_margin_policy,
    default_short_policy,
)
from stock_recommender.portfolio_engine.contracts import (
    MarketSnapshot,
    PositionSide,
    SignalCandidate,
)
from stock_recommender.portfolio_engine.signal_ports import FactorRankLongAdapter


CUTOFF = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)


class StaticSignals:
    def __init__(self, model_id: str, side: PositionSide, symbols: tuple[str, ...]):
        self.model_id = model_id
        self.side = side
        self.symbols = symbols

    def evaluate(self, rows, event_calendar):
        del rows, event_calendar
        weight = 15.0 if self.side is PositionSide.LONG else 5.0
        return tuple(
            SignalCandidate(
                symbol=symbol,
                side=self.side,
                score=1.0 - index / 100,
                requested_weight_pct=weight,
                model_id=self.model_id,
                thesis_id=f"{self.model_id}:{symbol}:2026-07-31",
            )
            for index, symbol in enumerate(self.symbols)
        )


class RowWeightSignals:
    model_id = "long-row-weight-v1"

    def evaluate(self, rows, event_calendar):
        del event_calendar
        row = rows[0]
        return (
            SignalCandidate(
                symbol=str(row["symbol"]),
                side=PositionSide.LONG,
                score=1.0,
                requested_weight_pct=float(row["requested_weight_pct"]),
                model_id=self.model_id,
                thesis_id="long-row-weight-v1:L:2026-07-31",
            ),
        )


def long_short_strategy() -> dict:
    exposure = default_exposure_policy()
    exposure["mode"] = "LONG_SHORT"
    margin = default_margin_policy()
    margin["financing_apr_pct"] = 8.0
    short = default_short_policy()
    short.update(
        {
            "signal_model": "short-static-v1",
            "estimated_borrow_apr_pct": 10.0,
        }
    )
    return {
        "version": 6,
        "id": "backtest-us",
        "revision": 7,
        "market": "us",
        "signal": {"model": "long-static-v1"},
        "lifecycle": {"stage": "paper"},
        "exposure_policy": exposure,
        "margin_policy": margin,
        "short_policy": short,
        "portfolio": {
            "initial_cash": 100_000.0,
            "commission_rate_pct": 0.01,
            "minimum_commission_cny": 0.0,
            "stamp_duty_rate_pct": 0.0,
            "transfer_fee_rate_pct": 0.0,
            "slippage_bps": 0.0,
            "max_bar_participation_pct": 100.0,
        },
    }


def long_only_replay_strategy(*, model_id: str) -> dict:
    value = long_short_strategy()
    value["id"] = f"backtest-{model_id}"
    value["signal"]["model"] = model_id
    value["exposure_policy"]["mode"] = "LONG_ONLY"
    return value


def single_market(
    snapshot_id: str,
    occurred_at: datetime,
    *,
    price: float = 100.0,
    bar_volume: int = 1_000_000,
) -> MarketSnapshot:
    return MarketSnapshot(
        id=snapshot_id,
        occurred_at=occurred_at,
        quotes={
            "L": {
                "price": price,
                "bar_open": price,
                "bar_high": price,
                "bar_low": price,
                "bar_volume": bar_volume,
                "volume_ratio": 1.0,
                "one_day_return": 0.0,
            }
        },
    )


def market(snapshot_id: str, occurred_at: datetime, price: float = 100.0) -> MarketSnapshot:
    symbols = tuple(f"L{index}" for index in range(8)) + ("S0", "S1")
    return MarketSnapshot(
        id=snapshot_id,
        occurred_at=occurred_at,
        quotes={
            symbol: {
                "price": price,
                "bar_open": price,
                "bar_high": price,
                "bar_low": price,
                "bar_volume": 1_000_000,
                "volume_ratio": 1.0,
                "one_day_return": 0.0,
            }
            for symbol in symbols
        },
    )


def frames() -> tuple[EngineReplayFrame, ...]:
    symbols = tuple(f"L{index}" for index in range(8)) + ("S0", "S1")
    return (
        EngineReplayFrame.plan(
            market("cutoff", CUTOFF),
            analyzed_rows=(
                {"symbol": "L0", "cutoff_date": "2026-07-31", "score": 1.0},
            ),
            event_calendar={symbol: None for symbol in symbols},
        ),
        EngineReplayFrame.process(
            market("entry", CUTOFF + timedelta(days=3, minutes=1)),
            record_nav=True,
        ),
        EngineReplayFrame.process(
            market("day-2", CUTOFF + timedelta(days=4, minutes=1)),
            record_nav=True,
        ),
    )


def estimated_borrow_book() -> HistoricalBorrowBook:
    return HistoricalBorrowBook.from_raw({}, history_complete=False)


def complete_borrow_book() -> HistoricalBorrowBook:
    rows = {}
    symbols = tuple(f"L{index}" for index in range(8)) + ("S0", "S1")
    for day in ("2026-07-31", "2026-08-03", "2026-08-04"):
        rows[day] = {
            symbol: {
                "shortable": True,
                "easy_to_borrow": True,
                "available_quantity": 1_000_000,
                "borrow_apr_pct": 10.0,
            }
            for symbol in symbols
        }
    return HistoricalBorrowBook.from_raw(rows, history_complete=True)


def high_apr_borrow_book() -> HistoricalBorrowBook:
    rows = {}
    symbols = tuple(f"L{index}" for index in range(8)) + ("S0", "S1")
    for day in ("2026-07-31", "2026-08-03", "2026-08-04"):
        rows[day] = {
            symbol: {
                "shortable": True,
                "easy_to_borrow": True,
                "available_quantity": 1_000_000,
                "borrow_apr_pct": 15.0,
            }
            for symbol in symbols
        }
    return HistoricalBorrowBook.from_raw(rows, history_complete=True)


def replay(
    *,
    mode: str,
    multiplier: float,
    book: HistoricalBorrowBook,
    strategy_value: dict | None = None,
):
    registry = {
        "long-static-v1": StaticSignals(
            "long-static-v1", PositionSide.LONG, tuple(f"L{index}" for index in range(8))
        ),
        "short-static-v1": StaticSignals(
            "short-static-v1", PositionSide.SHORT, ("S0", "S1")
        ),
    }
    return replay_engine_frames(
        strategy=strategy_value or long_short_strategy(),
        frames=frames(),
        borrow_book=book,
        signal_registry=registry,
        replay_mode=mode,
        cost_multiplier=multiplier,
    )


def gate_metrics() -> dict:
    return {
        "history_days": 300,
        "oos_events": 100,
        "oos_months": 12,
        "positive_fold_ratio": 1.0,
        "mean_excess_return_pct": 1.0,
        "stressed_mean_excess_return_pct": 0.5,
        "maximum_drawdown_pct": -1.0,
        "dsr_probability": 1.0,
    }


def gate_validation() -> dict:
    return {
        "history_days_min": 180,
        "minimum_oos_events": 40,
        "minimum_oos_months": 3,
        "minimum_positive_fold_ratio": 0.6,
        "maximum_drawdown_pct": 20,
        "minimum_dsr_probability": 0.9,
    }


def gate_metadata(*, mode: str, borrow_complete: bool) -> dict:
    return {
        "point_in_time_complete": True,
        "benchmark_complete": True,
        "strategy_parity_complete": True,
        "signal_model": "factor_rank_v1",
        "execution_parity_complete": True,
        "execution_data_complete": True,
        "corporate_actions_complete": True,
        "exposure_mode": mode,
        "borrow_history_complete": borrow_complete,
        "event_calendar_history_complete": True,
    }


def production_short_strategy() -> dict:
    value = load_strategy_config(path=Path("/missing"))
    value["id"] = "production-short-backtest"
    value["market"] = "us"
    value["parameters"]["market"]["value"] = "us"
    value["exposure_policy"]["mode"] = "LONG_SHORT"
    value["validation"].update(
        {
            "lookback_days": 60,
            "history_days_min": 61,
            "holding_period_days": 3,
            "top_n": 2,
            "minimum_oos_events": 1,
            "minimum_oos_months": 0,
            "minimum_positive_fold_ratio": 0,
            "minimum_dsr_probability": 0,
            "maximum_drawdown_pct": 100,
        }
    )
    return value


def production_short_dataset(*, days: int = 100) -> dict:
    start = date(2024, 1, 1)
    symbols = ("WEAK0", "WEAK1", "WEAK2")
    panel: dict[str, list[dict]] = {}
    for index, symbol in enumerate(symbols):
        rows = []
        decline = 0.002 + index * 0.0002
        for offset in range(days):
            current = start + timedelta(days=offset)
            close = 200.0 * ((1.0 - decline) ** offset)
            rows.append(
                {
                    "date": current.isoformat(),
                    "open": close * 1.001,
                    "close": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "volume": 2_000_000,
                    "turnover": 50_000_000.0,
                    "open_volume": 2_000_000,
                    "close_volume": 2_000_000,
                    "entry_price": close * 1.001,
                    "exit_price": close,
                }
            )
        panel[symbol] = rows
    days_by_date = [start + timedelta(days=offset) for offset in range(days)]
    return {
        "panel": panel,
        "benchmark": [
            {
                "date": day.isoformat(),
                "open": 100.0,
                "close": 100.0,
                "volume": 1,
            }
            for day in days_by_date
        ],
        "universe_by_date": {
            day.isoformat(): list(symbols) for day in days_by_date
        },
        "borrow_history": {
            day.isoformat(): {
                symbol: {
                    "shortable": True,
                    "easy_to_borrow": True,
                    "available_quantity": 1_000_000,
                    "borrow_apr_pct": 8.0,
                }
                for symbol in symbols
            }
            for day in days_by_date
        },
        "event_calendar_history": {
            day.isoformat(): {symbol: None for symbol in symbols}
            for day in days_by_date
        },
        "evaluation_period": {"start": "2024-03-20", "end": "2024-03-22"},
        "metadata": {
            "point_in_time_complete": True,
            "benchmark_complete": True,
            "strategy_parity_complete": True,
            "execution_data_complete": True,
            "execution_price_mode": "intraday_0935_1500",
            "corporate_actions_complete": True,
            "borrow_history_complete": True,
            "event_calendar_history_complete": True,
            "parameter_trials": 1,
        },
    }


class LongShortBacktestTests(unittest.TestCase):
    def test_replay_counts_one_close_only_after_partial_close_becomes_filled(self):
        strategy_value = long_only_replay_strategy(model_id="long-static-v1")
        strategy_value["portfolio"]["max_bar_participation_pct"] = 100.0
        result = replay_engine_frames(
            strategy=strategy_value,
            frames=(
                EngineReplayFrame.plan(
                    single_market("close-plan", CUTOFF),
                    analyzed_rows=({"symbol": "L"},),
                    event_calendar={"L": None},
                ),
                EngineReplayFrame.process(
                    single_market("close-open", CUTOFF + timedelta(days=3, minutes=1)),
                ),
                EngineReplayFrame.process(
                    single_market(
                        "close-risk",
                        CUTOFF + timedelta(days=3, minutes=2),
                        price=90.0,
                    ),
                ),
                EngineReplayFrame.process(
                    single_market(
                        "close-partial",
                        CUTOFF + timedelta(days=3, minutes=3),
                        price=90.0,
                        bar_volume=60,
                    ),
                ),
                EngineReplayFrame.process(
                    single_market(
                        "close-filled",
                        CUTOFF + timedelta(days=3, minutes=4),
                        price=90.0,
                    ),
                    record_nav=True,
                ),
            ),
            borrow_book=estimated_borrow_book(),
            signal_registry={
                "long-static-v1": StaticSignals(
                    "long-static-v1", PositionSide.LONG, ("L",)
                )
            },
        )
        close_intent_id = next(
            item[0]
            for frame in result.intent_fingerprints
            for item in frame
            if item[4] == "CLOSE"
        )
        close_statuses = [
            item[4]
            for frame in result.fill_fingerprints
            for item in frame
            if item[0] == close_intent_id
        ]

        self.assertEqual(close_statuses, ["PARTIAL", "FILLED"])
        self.assertEqual(result.closed_trades, 1)
        self.assertEqual(result.final_positions, ())

    def test_replay_does_not_count_filled_reduce_as_closed_trade(self):
        strategy_value = long_only_replay_strategy(model_id=RowWeightSignals.model_id)
        strategy_value["portfolio"]["max_bar_participation_pct"] = 100.0
        result = replay_engine_frames(
            strategy=strategy_value,
            frames=(
                EngineReplayFrame.plan(
                    single_market("reduce-plan-open", CUTOFF),
                    analyzed_rows=({"symbol": "L", "requested_weight_pct": 10.0},),
                    event_calendar={"L": None},
                ),
                EngineReplayFrame.process(
                    single_market("reduce-open", CUTOFF + timedelta(days=3, minutes=1)),
                ),
                EngineReplayFrame.plan(
                    single_market("reduce-plan", CUTOFF + timedelta(days=3, minutes=2)),
                    analyzed_rows=({"symbol": "L", "requested_weight_pct": 5.0},),
                    event_calendar={"L": None},
                ),
                EngineReplayFrame.process(
                    single_market("reduce-filled", CUTOFF + timedelta(days=3, minutes=3)),
                    record_nav=True,
                ),
            ),
            borrow_book=estimated_borrow_book(),
            signal_registry={RowWeightSignals.model_id: RowWeightSignals()},
        )

        self.assertTrue(
            any(
                item[4] == "REDUCE"
                for frame in result.intent_fingerprints
                for item in frame
            )
        )
        self.assertEqual(result.closed_trades, 0)
        self.assertEqual(result.final_positions[0][2], 50)

    def test_top_level_us_market_uses_complete_us_borrow_history(self):
        result = replay(
            mode="backtest",
            multiplier=1.0,
            book=complete_borrow_book(),
        )

        self.assertFalse(result.metadata["borrow_cost_estimated"])
        self.assertTrue(result.metadata["borrow_history_complete"])

    def test_raw_high_apr_rejection_is_stable_across_cost_multipliers(self):
        strategy_value = long_short_strategy()
        strategy_value["short_policy"]["estimated_borrow_apr_pct"] = 5.0
        results = tuple(
            replay(
                mode="backtest",
                multiplier=multiplier,
                book=high_apr_borrow_book(),
                strategy_value=strategy_value,
            )
            for multiplier in (0.0, 1.0, 2.0)
        )
        baseline = results[1]

        self.assertTrue(
            any(
                item[1] == "SHORT"
                for frame in baseline.signal_fingerprints
                for item in frame
            )
        )
        for result in results:
            self.assertFalse(result.metadata["borrow_cost_estimated"])
            self.assertTrue(result.metadata["borrow_history_complete"])
            self.assertEqual(result.signal_fingerprints, baseline.signal_fingerprints)
            self.assertEqual(result.intent_fingerprints, baseline.intent_fingerprints)
            self.assertEqual(result.fill_fingerprints, baseline.fill_fingerprints)
            self.assertEqual(result.position_snapshots, baseline.position_snapshots)
            self.assertFalse(
                any(
                    item[2] == "SHORT"
                    for frame in result.intent_fingerprints
                    for item in frame
                )
            )
            self.assertFalse(
                any(
                    item[1] == "SHORT"
                    for snapshot in result.position_snapshots
                    for item in snapshot
                )
            )
            self.assertEqual(result.metrics["borrow_cost"], 0.0)

    def test_production_walk_forward_opens_short_and_passes_data_gates(self):
        result = run_walk_forward_backtest(
            production_short_dataset(),
            production_short_strategy(),
        )
        checks = {item["id"]: item for item in result["approval_gate"]["checks"]}

        self.assertGreater(result["metrics"]["maximum_positions_observed"], 0)
        self.assertGreater(result["metrics"]["borrow_cost"], 0)
        self.assertTrue(result["metadata"]["borrow_history_complete"])
        self.assertTrue(result["metadata"]["event_calendar_history_complete"])
        self.assertTrue(checks["borrow_history"]["passed"])
        self.assertTrue(checks["event_calendar"]["passed"])

    def test_walk_forward_propagates_filled_close_count_to_top_level_metrics(self):
        dataset = production_short_dataset()
        execution_prices = {
            "2024-03-20": (100.0, 100.0),
            "2024-03-21": (100.0, 120.0),
            "2024-03-22": (120.0, 120.0),
        }
        for rows in dataset["panel"].values():
            for row in rows:
                prices = execution_prices.get(row["date"])
                if prices is None:
                    continue
                entry_price, exit_price = prices
                row.update(
                    {
                        "open": entry_price,
                        "close": exit_price,
                        "high": max(entry_price, exit_price) + 1.0,
                        "low": min(entry_price, exit_price) - 1.0,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                    }
                )

        result = run_walk_forward_backtest(dataset, production_short_strategy())

        self.assertGreater(result["metrics"]["closed_trades"], 0)
        self.assertEqual(
            result["metrics"]["closed_trades"],
            sum(fold["closed_trades"] for fold in result["folds"]),
        )

    def test_production_walk_forward_never_reads_future_event_calendar(self):
        strategy = production_short_strategy()
        safe_at_cutoff = production_short_dataset()
        cutoff_only = "2024-03-19"
        future_signal_day = "2024-03-20"
        safe_at_cutoff["evaluation_period"] = {
            "start": future_signal_day,
            "end": future_signal_day,
        }
        safe_at_cutoff["event_calendar_history"] = {
            cutoff_only: {
                symbol: None for symbol in safe_at_cutoff["panel"]
            },
            future_signal_day: {
                symbol: 0 for symbol in safe_at_cutoff["panel"]
            },
        }

        result = run_walk_forward_backtest(safe_at_cutoff, strategy)

        self.assertEqual(result["sample_events"][0]["symbols"], [])
        self.assertGreater(result["metrics"]["maximum_positions_observed"], 0)

    def test_shared_analyzed_universe_marks_only_preselected_long_rows(self):
        rows = (
            {
                "symbol": "LONG",
                "score": 0.9,
                "cutoff_date": "2026-07-31",
                "selected_for_long": True,
            },
            {
                "symbol": "SHORT-UNIVERSE",
                "score": 0.8,
                "cutoff_date": "2026-07-31",
                "selected_for_long": False,
            },
        )

        signals = FactorRankLongAdapter().evaluate(rows, {})

        self.assertEqual([item.symbol for item in signals], ["LONG"])

    def test_paper_and_backtest_same_snapshots_match_each_recorded_day(self):
        paper = replay(mode="paper", multiplier=1.0, book=complete_borrow_book())
        backtest = replay(mode="backtest", multiplier=1.0, book=complete_borrow_book())

        self.assertEqual(paper.event_fingerprints, backtest.event_fingerprints)
        self.assertEqual(paper.fill_fingerprints, backtest.fill_fingerprints)
        self.assertEqual(paper.position_snapshots, backtest.position_snapshots)
        self.assertEqual(paper.nav_series, backtest.nav_series)
        self.assertGreater(len(backtest.nav_series), 1)

    def test_replay_exposes_signal_and_intent_fingerprints(self):
        result = replay(
            mode="backtest",
            multiplier=1.0,
            book=complete_borrow_book(),
        )

        self.assertTrue(hasattr(result, "signal_fingerprints"))
        self.assertTrue(hasattr(result, "intent_fingerprints"))
        self.assertTrue(result.signal_fingerprints[0])
        self.assertTrue(result.intent_fingerprints[0])

    def test_double_cost_stress_increases_financing_and_estimated_borrow(self):
        normal = replay(mode="backtest", multiplier=1.0, book=estimated_borrow_book())
        stressed = replay(mode="backtest", multiplier=2.0, book=estimated_borrow_book())

        self.assertGreater(normal.metrics["financing_cost"], 0)
        self.assertGreater(normal.metrics["borrow_cost"], 0)
        self.assertGreater(stressed.metrics["financing_cost"], normal.metrics["financing_cost"])
        self.assertGreater(stressed.metrics["borrow_cost"], normal.metrics["borrow_cost"])
        self.assertEqual(normal.metadata["borrow_cost_estimated"], True)
        self.assertEqual(normal.metadata["borrow_history_complete"], False)
        self.assertEqual(stressed.metadata["cost_multiplier"], 2.0)
        self.assertEqual(stressed.event_fingerprints, normal.event_fingerprints)
        self.assertEqual(stressed.fill_fingerprints, normal.fill_fingerprints)
        self.assertEqual(stressed.position_snapshots, normal.position_snapshots)

    def test_historical_borrow_stress_changes_costs_without_changing_path(self):
        zero = replay(mode="backtest", multiplier=0.0, book=high_apr_borrow_book())
        normal = replay(mode="backtest", multiplier=1.0, book=high_apr_borrow_book())
        stressed = replay(mode="backtest", multiplier=2.0, book=high_apr_borrow_book())

        for candidate in (zero, stressed):
            self.assertEqual(candidate.signal_fingerprints, normal.signal_fingerprints)
            self.assertEqual(candidate.intent_fingerprints, normal.intent_fingerprints)
            self.assertEqual(candidate.event_fingerprints, normal.event_fingerprints)
            self.assertEqual(candidate.fill_fingerprints, normal.fill_fingerprints)
            self.assertEqual(candidate.position_snapshots, normal.position_snapshots)
            self.assertEqual(candidate.final_positions, normal.final_positions)
            self.assertEqual(
                candidate.metadata["borrow_apr_pct"],
                normal.metadata["borrow_apr_pct"],
            )

        self.assertEqual(normal.metadata["borrow_apr_pct"]["maximum"], 15.0)
        self.assertEqual(zero.metrics["transaction_fees"], 0.0)
        self.assertEqual(normal.metrics["transaction_fees"], 0.0)
        self.assertEqual(stressed.metrics["transaction_fees"], 0.0)
        for cost_name in ("financing_cost", "borrow_cost"):
            with self.subTest(cost_name=cost_name):
                self.assertEqual(zero.metrics[cost_name], 0.0)
                self.assertGreater(normal.metrics[cost_name], 0.0)
                self.assertAlmostEqual(
                    stressed.metrics[cost_name],
                    normal.metrics[cost_name] * 2.0,
                    places=9,
                )

    def test_short_live_gate_requires_complete_historical_borrow(self):
        failed = evaluate_approval_gate(
            gate_metrics(), gate_validation(), gate_metadata(mode="LONG_SHORT", borrow_complete=False)
        )
        passed = evaluate_approval_gate(
            gate_metrics(), gate_validation(), gate_metadata(mode="LONG_SHORT", borrow_complete=True)
        )
        long_only = evaluate_approval_gate(
            gate_metrics(), gate_validation(), gate_metadata(mode="LONG_ONLY", borrow_complete=False)
        )

        failed_check = next(item for item in failed["checks"] if item["id"] == "borrow_history")
        self.assertFalse(failed_check["passed"])
        self.assertTrue(next(item for item in passed["checks"] if item["id"] == "borrow_history")["passed"])
        self.assertTrue(next(item for item in long_only["checks"] if item["id"] == "borrow_history")["passed"])

    def test_future_borrow_snapshot_is_not_used_and_invalid_costs_fail_closed(self):
        future = HistoricalBorrowBook.from_raw(
            {
                "2026-08-05": {
                    "S0": {
                        "shortable": False,
                        "easy_to_borrow": False,
                        "borrow_apr_pct": 99.0,
                    }
                }
            },
            history_complete=True,
        )
        resolution = future.resolve(
            CUTOFF + timedelta(days=4),
            ("S0",),
            estimated_borrow_apr_pct=10.0,
        )
        self.assertTrue(resolution.estimated)
        self.assertEqual(resolution.snapshot.securities["S0"].borrow_apr_pct, 10.0)
        self.assertFalse(resolution.history_complete)

        for invalid in (-1.0, math.nan, math.inf):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                replay(mode="backtest", multiplier=invalid, book=estimated_borrow_book())

    def test_final_nav_is_a_mark_without_fabricating_close(self):
        result = replay(mode="backtest", multiplier=1.0, book=complete_borrow_book())

        self.assertEqual(result.final_positions, result.position_snapshots[-1])
        self.assertTrue(result.final_positions)
        self.assertNotIn("BACKTEST_LIQUIDATION", result.event_types)
        self.assertAlmostEqual(result.final_nav, result.nav_series[-1], places=9)


if __name__ == "__main__":
    unittest.main()
