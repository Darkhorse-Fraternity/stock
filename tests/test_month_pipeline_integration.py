import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_recommender.parameters import default_strategy_config
from stock_recommender.market_regime import evaluate_market_regime
from stock_recommender.portfolio import load_portfolio_account, monitor_portfolio, plan_daily_candidates
from stock_recommender.signal_engine import extract_signal_features, rank_signal_rows, select_ranked_signals


SHANGHAI = ZoneInfo("Asia/Shanghai")


def trading_dates(start: date, count: int) -> list[date]:
    rows = []
    current = start
    while len(rows) < count:
        if current.weekday() < 5:
            rows.append(current)
        current += timedelta(days=1)
    return rows


class OneMonthPipelineIntegrationTests(unittest.TestCase):
    def test_one_month_signal_to_portfolio_replay_uses_isolated_ledger(self):
        strategy = default_strategy_config()
        strategy.update({"id": "month-replay", "name": "月度隔离回放", "revision": 1})
        strategy["lifecycle"]["stage"] = "paper"
        dates = trading_dates(date(2026, 1, 5), 96)
        replay_dates = dates[-22:]
        symbols = [f"60000{index}" for index in range(1, 6)]
        histories = {}
        for symbol_index, symbol in enumerate(symbols, 1):
            histories[symbol] = [
                {
                    "date": day,
                    "open": 10 + symbol_index + index * (0.025 + symbol_index * 0.003),
                    "close": 10 + symbol_index + index * (0.026 + symbol_index * 0.003),
                    "high": 10.2 + symbol_index + index * (0.026 + symbol_index * 0.003),
                    "low": 9.8 + symbol_index + index * (0.025 + symbol_index * 0.003),
                    "volume": 1_000_000 + symbol_index * 10_000 + index * 1_000,
                }
                for index, day in enumerate(dates)
            ]

        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "month-portfolio.json"
            for replay_day in replay_dates:
                signal_rows = []
                for symbol in symbols:
                    features = extract_signal_features(histories[symbol], cutoff=replay_day)
                    self.assertIsNotNone(features)
                    day_row = next(item for item in histories[symbol] if item["date"] == replay_day)
                    signal_rows.append(
                        {
                            "symbol": symbol,
                            "name": symbol,
                            "price": day_row["open"],
                            "percent": features["latest_return"] * 100,
                            "signal_features": features,
                        }
                    )
                ranked = rank_signal_rows(signal_rows, strategy=strategy)
                selected = select_ranked_signals(ranked, 3, strategy=strategy)
                morning = datetime.combine(replay_day, time(8, 0), tzinfo=SHANGHAI)
                plan_daily_candidates(
                    strategy,
                    selected,
                    now=morning,
                    path=ledger,
                    market_regime=evaluate_market_regime(ranked, strategy),
                )
                prices = {row["symbol"]: row["price"] for row in signal_rows}

                def quotes(watchlist):
                    return [
                        {
                            "symbol": item["symbol"],
                            "name": item.get("name") or item["symbol"],
                            "price": prices[item["symbol"]],
                            "percent": 0.5,
                            "volume": 2_000_000,
                            "turnover": prices[item["symbol"]] * 2_000_000,
                        }
                        for item in watchlist
                    ], None

                account, _, _ = monitor_portfolio(
                    strategy,
                    now=datetime.combine(replay_day, time(9, 35), tzinfo=SHANGHAI),
                    path=ledger,
                    quote_fetcher=quotes,
                )
                account, _, _ = monitor_portfolio(
                    strategy,
                    now=datetime.combine(replay_day, time(9, 40), tzinfo=SHANGHAI),
                    path=ledger,
                    quote_fetcher=quotes,
                )
                self.assertLessEqual(len(account["positions"]), 10)

            account = load_portfolio_account(strategy["id"], path=ledger)
            self.assertIsNotNone(account)
            self.assertEqual(sum(key.startswith("daily:") for key in account["committed_run_keys"]), 22)
            self.assertEqual(account["signal_model"], "factor_rank_v1")
            self.assertTrue(any(event["type"] == "ORDER_FILLED" for event in account["events"]))
            self.assertGreaterEqual(len(account["nav_history"]), 22)
            self.assertTrue(ledger.is_file())


if __name__ == "__main__":
    unittest.main()
