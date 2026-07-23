import unittest
from datetime import date, timedelta

from stock_recommender.backtest import _score_features
from stock_recommender.parameters import default_strategy_config
from stock_recommender.runtime import StrategyRuntimeError, assert_strategy_runnable
from stock_recommender.signal_engine import (
    SIGNAL_FEATURE_FIELDS,
    extract_signal_features,
    rank_signal_rows,
    score_feature_map,
)


class SignalEngineTests(unittest.TestCase):
    @staticmethod
    def _history(rows=70):
        start = date(2026, 1, 1)
        return [
            {
                "date": start + timedelta(days=index),
                "open": 10 + index / 10,
                "close": 10 + index / 10,
                "high": 10.2 + index / 10,
                "low": 9.8 + index / 10,
                "volume": 1000 + index * 10,
            }
            for index in range(rows)
        ]

    def test_signal_cutoff_excludes_same_day_and_future_rows(self):
        cutoff = date(2026, 4, 1)
        history = self._history()
        baseline = extract_signal_features(history, cutoff=cutoff)
        modified = extract_signal_features(
            [
                *history,
                {"date": cutoff, "close": 9999, "open": 9999, "volume": 999999},
                {"date": cutoff + timedelta(days=1), "close": 1, "open": 1, "volume": 1},
            ],
            cutoff=cutoff,
        )

        self.assertIsNotNone(baseline)
        self.assertEqual(
            {field: baseline[field] for field in SIGNAL_FEATURE_FIELDS},
            {field: modified[field] for field in SIGNAL_FEATURE_FIELDS},
        )
        self.assertLess(baseline["history_latest_date"], cutoff.isoformat())

    def test_live_and_backtest_use_identical_cross_sectional_scores(self):
        features = {
            "600001": {field: float(index + 1) for index, field in enumerate(SIGNAL_FEATURE_FIELDS)},
            "600002": {field: float(10 - index) for index, field in enumerate(SIGNAL_FEATURE_FIELDS)},
            "600003": {field: 3.0 for field in SIGNAL_FEATURE_FIELDS},
        }
        rows = [{"symbol": symbol, "percent": 0.0, "signal_features": item} for symbol, item in features.items()]

        live = [(item["symbol"], item["score"]) for item in rank_signal_rows(rows)]
        shared = score_feature_map(features)
        replay = _score_features(features)

        self.assertEqual(live, shared)
        self.assertEqual(replay, shared)

    def test_tied_single_candidate_score_is_neutral(self):
        features = {"600001": {field: 1.0 for field in SIGNAL_FEATURE_FIELDS}}
        self.assertEqual(score_feature_map(features), [("600001", 50.0)])


class StrategyRuntimeTests(unittest.TestCase):
    def test_draft_can_preview_but_cannot_run_scheduled(self):
        strategy = default_strategy_config()
        assert_strategy_runnable(strategy, execution_kind="preview", mode="report")
        with self.assertRaises(StrategyRuntimeError):
            assert_strategy_runnable(strategy, execution_kind="scheduled", mode="report")

    def test_paper_can_run_and_unapproved_live_cannot(self):
        strategy = default_strategy_config()
        strategy["lifecycle"]["stage"] = "paper"
        assert_strategy_runnable(strategy, execution_kind="scheduled", mode="report")
        strategy["lifecycle"]["stage"] = "live"
        with self.assertRaises(StrategyRuntimeError):
            assert_strategy_runnable(strategy, execution_kind="scheduled", mode="report")


if __name__ == "__main__":
    unittest.main()
