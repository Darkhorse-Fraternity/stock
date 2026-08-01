from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

from stock_recommender.backtest import evaluate_approval_gate
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


def replay(*, mode: str, multiplier: float, book: HistoricalBorrowBook):
    registry = {
        "long-static-v1": StaticSignals(
            "long-static-v1", PositionSide.LONG, tuple(f"L{index}" for index in range(8))
        ),
        "short-static-v1": StaticSignals(
            "short-static-v1", PositionSide.SHORT, ("S0", "S1")
        ),
    }
    return replay_engine_frames(
        strategy=long_short_strategy(),
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
    }


class LongShortBacktestTests(unittest.TestCase):
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
            cost_multiplier=1.0,
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
