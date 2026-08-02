from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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


SYMBOLS = ("AAPL", "MSFT", "NVDA")


class MonthStaticLongSignals:
    model_id = "month-static-long-v2"
    side = PositionSide.LONG

    def evaluate(self, rows, event_calendar):
        del event_calendar
        cutoff = str(rows[0]["cutoff_date"])
        return tuple(
            SignalCandidate(
                symbol=symbol,
                side=PositionSide.LONG,
                score=1.0 - index / 100.0,
                requested_weight_pct=15.0,
                model_id=self.model_id,
                thesis_id=f"{self.model_id}:{symbol}:{cutoff}",
            )
            for index, symbol in enumerate(SYMBOLS)
        )


def trading_dates(start: date, count: int) -> tuple[date, ...]:
    sessions = []
    current = start
    while len(sessions) < count:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    return tuple(sessions)


def strategy() -> dict:
    return {
        "version": 6,
        "id": "month-v2-replay",
        "revision": 1,
        "market": "us",
        "signal": {"model": MonthStaticLongSignals.model_id},
        "lifecycle": {"stage": "paper"},
        "exposure_policy": default_exposure_policy(),
        "margin_policy": default_margin_policy(),
        "short_policy": default_short_policy(),
        "portfolio": {
            "initial_cash": 100_000.0,
            "commission_rate_pct": 0.0,
            "minimum_commission_cny": 0.0,
            "stamp_duty_rate_pct": 0.0,
            "transfer_fee_rate_pct": 0.0,
            "slippage_bps": 0.0,
            "max_bar_participation_pct": 100.0,
        },
    }


def market(snapshot_id: str, occurred_at: datetime, session_index: int) -> MarketSnapshot:
    quotes = {}
    for symbol_index, symbol in enumerate(SYMBOLS):
        price = 100.0 + session_index * 0.5 + symbol_index
        quotes[symbol] = {
            "price": price,
            "bar_open": price,
            "bar_high": price,
            "bar_low": price,
            "bar_volume": 1_000_000.0,
            "volume_ratio": 1.0,
            "one_day_return": 0.005,
        }
    return MarketSnapshot(
        id=snapshot_id,
        occurred_at=occurred_at,
        quotes=quotes,
    )


def month_frames() -> tuple[EngineReplayFrame, ...]:
    frames = []
    for index, session in enumerate(trading_dates(date(2026, 1, 5), 22)):
        cutoff_at = datetime.combine(
            session,
            time(20, 0),
            tzinfo=timezone.utc,
        )
        cutoff = market(f"month-plan-{index:02d}", cutoff_at, index)
        process = market(
            f"month-process-{index:02d}",
            cutoff_at + timedelta(minutes=1),
            index,
        )
        frames.extend(
            (
                EngineReplayFrame.plan(
                    cutoff,
                    analyzed_rows=(
                        {
                            "symbol": SYMBOLS[0],
                            "cutoff_date": session.isoformat(),
                        },
                    ),
                    event_calendar={symbol: None for symbol in SYMBOLS},
                ),
                EngineReplayFrame.process(process, record_nav=True),
            )
        )
    return tuple(frames)


class OneMonthPipelineIntegrationTests(unittest.TestCase):
    def test_22_sessions_replay_through_v2_engine_without_runtime_ledger_pollution(self):
        frames = month_frames()
        registry = {
            MonthStaticLongSignals.model_id: MonthStaticLongSignals(),
        }
        borrow = HistoricalBorrowBook.from_raw({}, history_complete=False)
        with tempfile.TemporaryDirectory() as directory:
            runtime_ledger = Path(directory) / "must-not-be-created.json"
            with patch.dict(
                os.environ,
                {"STOCK_AGENT_PORTFOLIO_PATH": str(runtime_ledger)},
            ):
                paper = replay_engine_frames(
                    strategy=strategy(),
                    frames=frames,
                    borrow_book=borrow,
                    signal_registry=registry,
                    replay_mode="paper",
                )
                backtest = replay_engine_frames(
                    strategy=strategy(),
                    frames=frames,
                    borrow_book=borrow,
                    signal_registry=registry,
                    replay_mode="backtest",
                )

            self.assertFalse(runtime_ledger.exists())

        self.assertEqual(len(paper.nav_series), 22)
        self.assertEqual(len(paper.position_snapshots), 22)
        self.assertEqual(
            sum(bool(frame) for frame in paper.signal_fingerprints),
            22,
        )
        self.assertLessEqual(len(paper.final_positions), 10)
        self.assertTrue(paper.final_positions)
        self.assertEqual(paper.event_fingerprints, backtest.event_fingerprints)
        self.assertEqual(paper.fill_fingerprints, backtest.fill_fingerprints)
        self.assertEqual(paper.position_snapshots, backtest.position_snapshots)
        self.assertEqual(paper.nav_series, backtest.nav_series)


if __name__ == "__main__":
    unittest.main()
