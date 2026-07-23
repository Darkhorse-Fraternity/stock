import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from stock_recommender.historical_data import (
    AkshareCniProvider,
    UniverseSnapshot,
    audit_historical_dataset,
    build_historical_dataset,
    write_historical_dataset,
)


class FakeAkshareClient:
    def __init__(self):
        self.stock_calls = []

    def index_detail_hist_cni(self, *, symbol):
        self.index_symbol = symbol
        return [
            {"日期": "2026-06-30", "样本代码": "1", "样本简称": "样本一"},
            {"日期": "2026-06-30", "样本代码": "300002", "样本简称": "ST 样本"},
            {"日期": "2026-06-30", "样本代码": "600003", "样本简称": "退市样本"},
            {"日期": "2026-06-30", "样本代码": "600004", "样本简称": "样本四"},
        ]

    def stock_zh_a_hist(self, **kwargs):
        self.stock_calls.append(kwargs)
        return [{"日期": "2026-07-01", "开盘": 10, "收盘": 11, "最高": 12, "最低": 9, "成交量": 100}]

    def index_zh_a_hist(self, **kwargs):
        self.benchmark_call = kwargs
        return [{"日期": "2026-07-01", "开盘": 100, "收盘": 101, "最高": 102, "最低": 99, "成交量": 1000}]


class FakeProvider:
    universe_name = "测试 AI"
    universe_symbol = "399284"
    benchmark_name = "测试 AI 指数"
    benchmark_symbol = "399284"

    def universe_snapshots(self):
        names = {"600001": "甲", "600002": "乙", "600003": "丙"}
        return [UniverseSnapshot(date(2026, 6, 30), tuple(names), names)]

    def security_history(self, symbol, start, end, *, name=None):
        rows = []
        day = start
        price = 10.0 + int(symbol[-1])
        while day <= end:
            rows.append(
                {
                    "date": day.isoformat(),
                    "open": price,
                    "close": price + 0.1,
                    "high": price + 0.2,
                    "low": price - 0.2,
                    "volume": 100_000,
                    "turnover": 1_000_000,
                    "name": name,
                }
            )
            price += 0.01
            day += timedelta(days=1)
        return rows

    def benchmark_history(self, start, end):
        rows = []
        day = start
        price = 1000.0
        while day <= end:
            rows.append(
                {
                    "date": day.isoformat(),
                    "open": price,
                    "close": price + 1,
                    "high": price + 2,
                    "low": price - 2,
                    "volume": 1_000_000,
                    "turnover": 10_000_000,
                }
            )
            price += 1
            day += timedelta(days=1)
        return rows

    def intraday_execution(self, symbol, start, end):
        result = {}
        day = start
        while day <= end:
            result[day.isoformat()] = {
                "entry_price": 12.0,
                "exit_price": 12.1,
                "open_volume": 10_000,
                "close_volume": 8_000,
            }
            day += timedelta(days=1)
        return result


class HistoricalDataTests(unittest.TestCase):
    def test_akshare_adapter_filters_non_a_share_and_risk_names(self):
        client = FakeAkshareClient()
        provider = AkshareCniProvider(client=client)

        snapshots = provider.universe_snapshots()
        history = provider.security_history("600004", date(2026, 1, 1), date(2026, 7, 1), name="样本四")

        self.assertEqual(snapshots[0].symbols, ("000001", "600004"))
        self.assertEqual(history[0]["name"], "样本四")
        self.assertEqual(client.stock_calls[0]["adjust"], "")

    def test_builder_clips_to_first_safe_day_and_keeps_execution_gate_closed(self):
        dataset = build_historical_dataset(
            FakeProvider(),
            evaluation_start=date(2026, 6, 23),
            evaluation_end=date(2026, 7, 22),
            warmup_calendar_days=120,
            workers=2,
        )

        self.assertEqual(dataset["evaluation_period"]["start"], "2026-07-01")
        self.assertEqual(dataset["universe_by_date"]["2026-06-30"], ["600001", "600002", "600003"])
        self.assertTrue(dataset["metadata"]["point_in_time_complete"])
        self.assertTrue(dataset["metadata"]["benchmark_complete"])
        self.assertFalse(dataset["metadata"]["execution_data_complete"])
        self.assertTrue(dataset["metadata"]["quality_audit"]["passed"])

        with tempfile.TemporaryDirectory() as directory:
            target = write_historical_dataset(dataset, Path(directory) / "dataset.json")
            loaded = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(loaded["metadata"]["source"], "akshare_public_cnindex_eastmoney")

    def test_audit_reports_missing_warmup(self):
        dataset = {
            "panel": {"600001": [{"date": "2026-07-01", "close": 10}]},
            "benchmark": [{"date": "2026-07-01", "close": 100}],
            "evaluation_period": {"start": "2026-07-01", "end": "2026-07-02"},
        }

        audit = audit_historical_dataset(dataset)

        self.assertFalse(audit["passed"])
        self.assertIn("部分证券不足 61 个交易日预热数据", audit["issues"])

    def test_builder_can_verify_exact_intraday_execution_fields(self):
        dataset = build_historical_dataset(
            FakeProvider(),
            evaluation_start=date(2026, 7, 1),
            evaluation_end=date(2026, 7, 3),
            warmup_calendar_days=120,
            workers=2,
            include_intraday_execution=True,
        )

        first = dataset["panel"]["600001"][-3]
        self.assertEqual(first["entry_price"], 12.0)
        self.assertGreater(first["upper_limit"], first["lower_limit"])
        self.assertTrue(dataset["metadata"]["execution_data_complete"])
        self.assertEqual(dataset["metadata"]["execution_price_mode"], "intraday_0935_1500")


if __name__ == "__main__":
    unittest.main()
