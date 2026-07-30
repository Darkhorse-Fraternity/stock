import json
import socket
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from stock_recommender.cli import main
from stock_recommender.context import extract_market_payload, generate_agent_context
from stock_recommender.data_sources import fetch_board_quotes, fetch_sina_fallback_quotes, fetch_watchlist_quotes
from stock_recommender.parameters import default_strategy_config
from stock_recommender.recommendation import RecommendationOutput
from stock_recommender.reports import build_conservative_report, format_recommendation_snapshot, generate_ai_report, generate_report
from stock_recommender.schedule import parse_publish_hours, should_publish_now
from stock_recommender.selection import analyze_candidates, evaluate_tick_ignition, filter_candidates, select_agent_candidates
from stock_recommender.tracking import generate_saved_tracking_report, load_daily_selection, save_daily_selection
from stock_recommender.universe import parse_watchlist
from recommendation_fixtures import make_recommendation_plan
from stock_recommender.recommendation import recommendation_tracking_entries


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class FakeBytesResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.payload


class StockAgentTests(unittest.TestCase):
    def setUp(self):
        def history(symbol, **kwargs):
            numeric = int(symbol) if str(symbol).isdigit() else sum(ord(char) for char in str(symbol))
            slope = 0.08 + (1_000_000 - numeric) / 100_000_000
            start = date(2026, 2, 1)
            return [
                {
                    "date": start + timedelta(days=index),
                    "open": 10 + slope * index,
                    "close": 10 + slope * index,
                    "high": 10.2 + slope * index,
                    "low": 9.8 + slope * index,
                    "volume": 100_000 + index * 1_000,
                }
                for index in range(100)
            ]

        self.history_patch = mock.patch("stock_recommender.enrichment.fetch_daily_history", side_effect=history)
        self.history_patch.start()

    def tearDown(self):
        self.history_patch.stop()

    def test_parse_watchlist_supports_compact_and_json_formats(self):
        compact = parse_watchlist(
            "sh600519:贵州茅台:白酒，000858:五粮液:白酒;300750.SZ:宁德时代:新能源"
        )
        mapped = parse_watchlist(
            '{"600519": {"name": "贵州茅台", "sector": "白酒"}, "300750": "新能源"}'
        )

        self.assertEqual([item["symbol"] for item in compact], ["600519", "000858", "300750"])
        self.assertEqual(compact[0]["sector"], "白酒")
        self.assertEqual(compact[2]["name"], "宁德时代")
        self.assertEqual(mapped[0]["name"], "贵州茅台")
        self.assertEqual(mapped[1]["sector"], "新能源")

    def test_fetch_watchlist_quotes_preserves_configured_sector(self):
        seen_requests = []
        raw = (
            'var hq_str_sh600519="贵州茅台,1500.000,1490.000,1510.000,1520.000,'
            '1488.000,1510.000,1511.000,10000,15100000.000,0,0,0,0,0,0,0,0,0,0,'
            '0,0,0,0,0,0,0,0,0,0,2026-07-17,10:00:00,00";'
        ).encode("gb18030")

        quotes, error = fetch_watchlist_quotes(
            [{"symbol": "600519", "name": "茅台观察", "sector": "白酒"}],
            urlopen_func=lambda request, timeout: (seen_requests.append(request.full_url) or FakeBytesResponse(raw)),
        )

        self.assertIsNone(error)
        self.assertEqual(quotes[0]["symbol"], "600519")
        self.assertEqual(quotes[0]["name"], "茅台观察")
        self.assertEqual(quotes[0]["sector"], "白酒")
        self.assertEqual(quotes[0]["source"], "新浪财经实时行情（自选股池）")
        self.assertIn("list=sh600519", seen_requests[0])

    def test_filter_candidates_supports_sector_tags(self):
        rows = [
            {"symbol": "600519", "name": "贵州茅台", "sector": "白酒", "price": 1500, "turnover": 1},
            {
                "symbol": "300750",
                "name": "宁德时代",
                "sector": "锂电池",
                "sectors": ["锂电池", "新能源"],
                "price": 300,
                "turnover": 1,
            },
        ]

        filtered = filter_candidates(rows, sector_filters=["新能源"])

        self.assertEqual([row["symbol"] for row in filtered], ["300750"])

    def test_agent_context_uses_watchlist_and_applies_sector_filter(self):
        board_called = False

        def board_fetcher(board_code, **kwargs):
            nonlocal board_called
            board_called = True
            return [], "should not run"

        def watchlist_fetcher(entries):
            self.assertEqual([item["symbol"] for item in entries], ["600519", "300750"])
            return [
                {
                    "symbol": "600519",
                    "name": "贵州茅台",
                    "price": 1510,
                    "percent": 1.2,
                    "turnover": 500000000,
                    "source": "自选测试行情",
                },
                {
                    "symbol": "300750",
                    "name": "宁德时代",
                    "price": 300,
                    "percent": 2.1,
                    "turnover": 600000000,
                    "source": "自选测试行情",
                },
                {
                    "symbol": "600000",
                    "name": "池外股票",
                    "sector": "白酒",
                    "price": 10,
                    "percent": 9,
                    "turnover": 900000000,
                    "source": "自选测试行情",
                },
            ], None

        context = generate_agent_context(
            watchlist=[
                {"symbol": "600519", "sector": "白酒"},
                {"symbol": "300750", "sector": "新能源"},
            ],
            sector_filters=["白酒"],
            board_fetcher=board_fetcher,
            watchlist_fetcher=watchlist_fetcher,
        )
        payload = extract_market_payload(context)

        self.assertFalse(board_called)
        self.assertEqual(payload["universe_type"], "watchlist")
        self.assertEqual(payload["watchlist_size"], 2)
        self.assertEqual(payload["sector_filters"], ["白酒"])
        self.assertEqual([item["symbol"] for item in payload["candidates"]], ["600519"])
        self.assertEqual(payload["candidates"][0]["sector"], "白酒")

    def test_watchlist_failure_never_falls_back_outside_watchlist(self):
        fallback_called = False

        def fallback_fetcher(**kwargs):
            nonlocal fallback_called
            fallback_called = True
            return [{"symbol": "300130", "price": 10, "turnover": 1}], None

        context = generate_agent_context(
            watchlist=["600519"],
            watchlist_fetcher=lambda entries: ([], "watchlist timeout"),
            fallback_fetcher=fallback_fetcher,
        )
        payload = extract_market_payload(context)

        self.assertFalse(fallback_called)
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["fetch_error"], "watchlist timeout")

    def test_watchlist_report_displays_sector_filter(self):
        report = generate_report(
            watchlist=[{"symbol": "600519", "sector": "白酒"}],
            sector_filters=["白酒"],
            watchlist_fetcher=lambda entries: (
                [
                    {
                        "symbol": "600519",
                        "name": "贵州茅台",
                        "price": 1510,
                        "percent": 1.2,
                        "change": 18,
                        "turnover": 500000000,
                        "turnover_rate": 1.5,
                        "source": "自选测试行情",
                    }
                ],
                None,
            ),
        )

        self.assertIn("自选股池（板块：白酒）精选 1 只", report)
        self.assertIn("板块**: 白酒", report)
        self.assertIn("贵州茅台 (600519)", report)

    def test_cli_passes_watchlist_and_sector_filters_to_report(self):
        env = {
            "STOCK_AGENT_MODE": "report",
            "STOCK_AGENT_EXECUTION_KIND": "preview",
            "STOCK_AGENT_WATCHLIST": "600519:贵州茅台:白酒,300750:宁德时代:新能源",
            "STOCK_AGENT_SECTOR_FILTERS": "白酒",
            "STOCK_AGENT_OUTPUT": "",
        }
        plan = make_recommendation_plan([], now=datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc))
        output = RecommendationOutput(report="test report", plan=plan)
        with mock.patch.dict("os.environ", env, clear=True), mock.patch(
            "stock_recommender.cli.collect_recommendation_plan", return_value=plan
        ) as collect_plan, mock.patch(
            "stock_recommender.cli.render_report_result", return_value=output
        ):
            main()

        kwargs = collect_plan.call_args.kwargs
        self.assertEqual([item["symbol"] for item in kwargs["watchlist"]], ["600519", "300750"])
        self.assertEqual(kwargs["sector_filters"], ["白酒"])

    def test_schedule_only_publishes_on_weekday_trading_hours(self):
        monday_9am = datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc)
        monday_930am = datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc)
        monday_10am = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)
        monday_noon = datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc)
        monday_1pm = datetime(2026, 7, 20, 5, 0, tzinfo=timezone.utc)
        monday_301pm = datetime(2026, 7, 20, 7, 1, tzinfo=timezone.utc)
        saturday_10am = datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)

        self.assertFalse(should_publish_now(monday_9am))
        self.assertTrue(should_publish_now(monday_930am))
        self.assertTrue(should_publish_now(monday_10am))
        self.assertFalse(should_publish_now(monday_noon))
        self.assertTrue(should_publish_now(monday_1pm))
        self.assertFalse(should_publish_now(monday_301pm))
        self.assertFalse(should_publish_now(saturday_10am))
        self.assertEqual(parse_publish_hours("9,10,15"), (9, 10, 15))

    def test_cli_schedule_guard_skips_without_generating_report(self):
        env = {
            "STOCK_AGENT_MODE": "report",
            "STOCK_AGENT_SCHEDULE_GUARD": "1",
            "STOCK_AGENT_OUTPUT": "",
        }
        with mock.patch.dict("os.environ", env, clear=True), mock.patch(
            "stock_recommender.cli.should_publish_now", return_value=False
        ), mock.patch("stock_recommender.cli.collect_recommendation_plan") as collect_plan:
            main()

        collect_plan.assert_not_called()

    def test_cli_persists_recommendations_for_tracking_mode(self):
        generated_at = datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc)
        plan = make_recommendation_plan(
            [{"symbol": "300130", "name": "新国都", "price": 18.03}],
            now=generated_at,
        )
        recommendation = RecommendationOutput(report="test report", plan=plan)
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "selection.json")
            history_path = str(Path(directory) / "history.json")
            portfolio_path = str(Path(directory) / "portfolio.json")
            env = {
                "STOCK_AGENT_MODE": "report",
                "STOCK_AGENT_STATE_PATH": state_path,
                "STOCK_AGENT_HISTORY_PATH": history_path,
                "STOCK_AGENT_PORTFOLIO_PATH": portfolio_path,
                "STOCK_AGENT_OUTPUT": "",
            }
            from stock_recommender.parameters import default_strategy_config
            strategy = default_strategy_config()
            strategy["lifecycle"]["stage"] = "paper"
            with mock.patch.dict("os.environ", env, clear=True), mock.patch(
                "stock_recommender.cli.load_strategy_config", return_value=strategy
            ), mock.patch(
                "stock_recommender.cli.collect_recommendation_plan", return_value=plan
            ), mock.patch(
                "stock_recommender.cli.render_report_result", return_value=recommendation
            ):
                main()

            payload = json.loads(Path(state_path).read_text(encoding="utf-8"))

        self.assertEqual(payload["symbols"], ["300130"])

    def test_cli_persists_plan_before_ai_rendering(self):
        plan = make_recommendation_plan(
            [{"symbol": "300130", "name": "新国都", "price": 18.03}],
            now=datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc),
        )
        strategy = default_strategy_config()
        strategy["lifecycle"]["stage"] = "paper"
        calls = []
        render_options = {}

        def persist(*args, **kwargs):
            calls.append("persist")

        def render(*args, **kwargs):
            calls.append("render")
            render_options.update(kwargs)
            raise TimeoutError("llm timeout")

        env = {
            "STOCK_AGENT_MODE": "ai",
            "STOCK_AGENT_ENABLE_TICK": "1",
            "STOCK_AGENT_OUTPUT": "",
        }
        with mock.patch.dict("os.environ", env, clear=True), mock.patch(
            "stock_recommender.cli.load_strategy_config", return_value=strategy
        ), mock.patch(
            "stock_recommender.cli.collect_recommendation_plan", return_value=plan
        ), mock.patch(
            "stock_recommender.cli.save_daily_selection", side_effect=persist
        ), mock.patch(
            "stock_recommender.cli.render_ai_report_result", side_effect=render
        ):
            with self.assertRaisesRegex(TimeoutError, "llm timeout"):
                main()

        self.assertEqual(calls, ["persist", "render"])
        self.assertFalse(render_options["enable_tick"])

    def test_cli_caps_llm_timeout_to_remaining_run_budget(self):
        plan = make_recommendation_plan(
            [{"symbol": "300130", "name": "新国都", "price": 18.03}],
            now=datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc),
        )
        recommendation = RecommendationOutput(report="test report", plan=plan)
        strategy = default_strategy_config()
        strategy["lifecycle"]["stage"] = "paper"
        env = {
            "STOCK_AGENT_MODE": "ai",
            "STOCK_AGENT_EXECUTION_KIND": "preview",
            "STOCK_AGENT_LLM_TIMEOUT": "60",
            "STOCK_AGENT_RUN_BUDGET_SECONDS": "100",
            "STOCK_AGENT_ENABLE_TICK": "1",
            "STOCK_AGENT_OUTPUT": "",
        }
        with mock.patch.dict("os.environ", env, clear=True), mock.patch(
            "stock_recommender.cli.load_strategy_config", return_value=strategy
        ), mock.patch(
            "stock_recommender.cli.collect_recommendation_plan", return_value=plan
        ), mock.patch(
            "stock_recommender.cli.time.monotonic", side_effect=[0.0, 75.0]
        ), mock.patch(
            "stock_recommender.cli.render_ai_report_result", return_value=recommendation
        ) as render:
            main()

        self.assertEqual(render.call_args.kwargs["llm_timeout"], 25)
        self.assertTrue(render.call_args.kwargs["enable_tick"])

    def test_recommendation_snapshot_contains_volume_turnover_and_change(self):
        snapshot = format_recommendation_snapshot(
            [
                {
                    "symbol": "300130",
                    "name": "新国都",
                    "price": 18.03,
                    "percent": -1.31,
                    "volume": 129661,
                    "turnover": 231676726.04,
                    "signal_score": 82.5,
                }
            ],
            generated_at="07月17日 10:00",
        )

        self.assertIn("推荐股每小时成交与涨跌跟踪", snapshot)
        self.assertIn("涨跌幅 -1.31%", snapshot)
        self.assertIn("成交量 12.97 万手", snapshot)
        self.assertIn("成交额 2.3 亿", snapshot)
        self.assertIn("信号分 82.50/100", snapshot)
        plan = make_recommendation_plan(
            [{"symbol": "300130", "name": "新国都", "price": 18.03, "signal_score": 82.5}],
            now=datetime(2026, 7, 17, 2, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(recommendation_tracking_entries(plan)[0]["score"], 82.5)

    def test_daily_selection_is_saved_and_expires_next_day(self):
        recommendation_time = datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc)
        next_day = datetime(2026, 7, 21, 1, 30, tzinfo=timezone.utc)
        plan = make_recommendation_plan(
            [{"symbol": "300130", "name": "新国都", "price": 18.03}],
            now=recommendation_time,
        )

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "selection.json"
            saved = save_daily_selection(state_path, plan, now=recommendation_time)

            self.assertEqual(saved, ["300130"])
            self.assertEqual(load_daily_selection(state_path, now=recommendation_time), ["300130"])
            self.assertEqual(load_daily_selection(state_path, now=next_day), [])

    def test_saved_tracking_report_only_fetches_morning_recommendations(self):
        recommendation_time = datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc)
        tracking_time = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)
        plan = make_recommendation_plan(
            [
                {"symbol": "300130", "name": "新国都", "price": 18.03},
                {"symbol": "300750", "name": "宁德时代", "price": 300},
            ],
            now=recommendation_time,
        )

        def quote_fetcher(entries):
            self.assertEqual([item["symbol"] for item in entries], ["300130", "300750"])
            return [
                {
                    "symbol": "300130",
                    "name": "新国都",
                    "price": 18.5,
                    "percent": 2.1,
                    "volume": 150000,
                    "turnover": 280000000,
                    "source": "测试行情",
                },
                {
                    "symbol": "300750",
                    "name": "宁德时代",
                    "price": 305,
                    "percent": 1.5,
                    "volume": 80000,
                    "turnover": 900000000,
                    "source": "测试行情",
                },
                {"symbol": "600000", "name": "池外股票", "price": 10, "percent": 9, "volume": 1, "turnover": 1},
            ], None

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "selection.json"
            save_daily_selection(state_path, plan, now=recommendation_time)
            report = generate_saved_tracking_report(
                state_path=state_path,
                now=tracking_time,
                quote_fetcher=quote_fetcher,
            )

        self.assertIn("推荐股盘中情况报告", report)
        self.assertIn("新国都 (300130)", report)
        self.assertIn("宁德时代 (300750)", report)
        self.assertNotIn("池外股票", report)
        self.assertIn("涨跌幅 +2.10%", report)
        self.assertIn("成交量 15.00 万手", report)
        self.assertIn("不重新选股", report)

    def test_fetch_board_quotes_parses_ai_agent_board(self):
        seen_requests = []
        payload = {
            "data": {
                "diff": [
                    {
                        "f12": "300130",
                        "f14": "新国都",
                        "f2": 23.69,
                        "f3": 14.28,
                        "f4": 2.96,
                        "f5": 123,
                        "f6": 986014953.57,
                        "f8": 9.87,
                        "f9": 31.5,
                        "f10": 12.3,
                    }
                ]
            }
        }

        quotes, error = fetch_board_quotes(
            "BK0800",
            limit=10,
            page_size=10,
            urlopen_func=lambda request, timeout: (seen_requests.append(request.full_url) or FakeResponse(payload)),
        )

        self.assertIsNone(error)
        self.assertEqual(quotes[0]["symbol"], "300130")
        self.assertEqual(quotes[0]["name"], "新国都")
        self.assertEqual(quotes[0]["source"], "东方财富 人工智能(BK0800)")
        self.assertIn("pz=10", seen_requests[0])

    def test_fetch_board_quotes_pages_in_small_batches(self):
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(request.full_url)
            symbol = "300130" if len(calls) == 1 else "300857"
            return FakeResponse(
                {
                    "data": {
                        "diff": [
                            {
                                "f12": symbol,
                                "f14": "测试",
                                "f2": 10,
                                "f3": 1,
                                "f4": 0.1,
                                "f5": 10,
                                "f6": 100,
                                "f8": 1,
                                "f9": 10,
                                "f10": 2,
                            }
                        ]
                    }
                }
            )

        quotes, error = fetch_board_quotes(
            "BK0809",
            limit=2,
            page_size=1,
            urlopen_func=fake_urlopen,
        )

        self.assertIsNone(error)
        self.assertEqual([quote["symbol"] for quote in quotes], ["300130", "300857"])
        self.assertIn("pn=1", calls[0])
        self.assertIn("pn=2", calls[1])

    def test_fetch_board_quotes_bounds_curl_retries_and_total_timeout(self):
        completed = mock.Mock(returncode=1, stdout="", stderr="source unavailable")
        with mock.patch(
            "stock_recommender.data_sources.urllib.request.urlopen",
            side_effect=OSError("primary unavailable"),
        ), mock.patch(
            "stock_recommender.data_sources.subprocess.run",
            return_value=completed,
        ) as run:
            quotes, error = fetch_board_quotes("BK0800", timeout=3, retries=2)

        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--retry") + 1], "2")
        self.assertEqual(run.call_args.kwargs["timeout"], 11)
        self.assertEqual(quotes, [])
        self.assertEqual(error, "source unavailable")

    def test_fetch_board_quotes_fails_over_to_delay_domain(self):
        payload = json.dumps(
            {
                "data": {
                    "total": 1,
                    "diff": [
                        {
                            "f12": "300130",
                            "f14": "新国都",
                            "f2": 18.03,
                            "f3": 1.2,
                            "f4": 0.2,
                            "f5": 100,
                            "f6": 100_000_000,
                        }
                    ],
                }
            }
        )

        def curl(command, **kwargs):
            if "push2delay.eastmoney.com" in command[-1]:
                return mock.Mock(returncode=0, stdout=payload, stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="primary eof")

        with mock.patch(
            "stock_recommender.data_sources.urllib.request.urlopen",
            side_effect=OSError("primary unavailable"),
        ), mock.patch(
            "stock_recommender.data_sources.subprocess.run",
            side_effect=curl,
        ) as run:
            quotes, error = fetch_board_quotes("BK0800", timeout=1, retries=0)

        self.assertIsNone(error)
        self.assertEqual([item["symbol"] for item in quotes], ["300130"])
        self.assertEqual(run.call_count, 2)
        self.assertIn("push2delay.eastmoney.com", run.call_args.args[0][-1])

    def test_fetch_sina_fallback_quotes_parses_realtime_payload(self):
        seen_requests = []
        raw = (
            'var hq_str_sz300130="新国都,18.270,18.270,18.030,18.340,'
            '17.510,18.030,18.040,12966188,231676726.040,43295,18.030,'
            '7700,18.020,37200,18.010,22400,18.000,800,17.990,7600,'
            '18.040,15200,18.050,10100,18.060,6700,18.070,5000,18.080,'
            '2026-07-09,15:35:45,00,D|2900|52287.000";'
        ).encode("gb18030")

        quotes, error = fetch_sina_fallback_quotes(
            symbols=["300130"],
            board_name="AI智能体",
            urlopen_func=lambda request, timeout: (seen_requests.append(request.full_url) or FakeBytesResponse(raw)),
        )

        self.assertIsNone(error)
        self.assertEqual(quotes[0]["symbol"], "300130")
        self.assertEqual(quotes[0]["name"], "新国都")
        self.assertEqual(quotes[0]["price"], 18.03)
        self.assertAlmostEqual(quotes[0]["percent"], -1.31)
        self.assertEqual(quotes[0]["change"], -0.24)
        self.assertEqual(quotes[0]["volume"], 129661)
        self.assertEqual(quotes[0]["turnover"], 231676726.04)
        self.assertEqual(quotes[0]["source"], "新浪财经实时行情（AI智能体备用股池）")
        self.assertIn("list=sz300130", seen_requests[0])

    def test_agent_context_blocks_fixed_fallback_when_board_fetch_fails(self):
        def board_fetcher(board_code, **kwargs):
            return [], "eastmoney ssl eof"

        def fallback_fetcher(**kwargs):
            return [
                {
                    "symbol": "300130",
                    "name": "新国都",
                    "sector": "AI智能体",
                    "price": 18.03,
                    "percent": -1.31,
                    "change": -0.24,
                    "volume": 129661,
                    "turnover": 231676726.04,
                    "turnover_rate": 0.0,
                    "pe": 0.0,
                    "amplitude": 4.54,
                    "open": 18.27,
                    "prev_close": 18.27,
                    "source": "新浪财经实时行情（AI智能体备用股池）",
                }
            ], None

        context = generate_agent_context(
            board_fetcher=board_fetcher,
            fallback_fetcher=fallback_fetcher,
            candidate_limit=1,
        )
        payload = extract_market_payload(context)

        self.assertIn("禁止使用固定兜底名单", payload["fetch_error"])
        self.assertEqual(payload["data_quality"]["status"], "BLOCKED")
        self.assertEqual(payload["candidates"], [])

    def test_filter_candidates_removes_bse_st_and_empty_rows(self):
        rows = [
            {"symbol": "300130", "name": "新国都", "price": 23.69, "turnover": 1},
            {"symbol": "920171", "name": "志晟信息", "price": 19.15, "turnover": 1},
            {"symbol": "002000", "name": "ST 测试", "price": 5.0, "turnover": 1},
            {"symbol": "600000", "name": "空成交", "price": 1.0, "turnover": 0},
        ]

        filtered = filter_candidates(rows)

        self.assertEqual([row["symbol"] for row in filtered], ["300130"])

    def test_report_falls_back_when_board_fetch_times_out(self):
        def timeout_fetcher(board_code, **kwargs):
            raise socket.timeout("slow")

        report = generate_report(
            now=datetime(2026, 6, 22, 1, 0, tzinfo=timezone.utc),
            board_fetcher=timeout_fetcher,
            fallback_fetcher=lambda **kwargs: ([], "sina timeout"),
        )

        self.assertIn("策略运行报告** (2026 年06月22日)", report)
        self.assertIn("本次没有匹配股票，不新增持仓", report)
        self.assertIn("既有持仓继续由退出 Pipeline 管理", report)
        self.assertIn("slow", report)

    def test_report_uses_board_rows_and_beijing_time(self):
        def board_fetcher(board_code, **kwargs):
            return [
                {
                    "symbol": "300130",
                    "name": "新国都",
                    "sector": "AI智能体",
                    "price": 23.69,
                    "percent": 14.28,
                    "change": 2.96,
                    "volume": 100,
                    "turnover": 986014953.57,
                    "turnover_rate": 9.87,
                    "pe": 31.5,
                    "amplitude": 12.3,
                    "source": "东方财富 AI智能体(BK0809)",
                }
            ], None

        report = generate_report(
            now=datetime(2026, 6, 22, 1, 0, tzinfo=timezone.utc),
            board_fetcher=board_fetcher,
        )

        self.assertIn("新国都 (300130)", report)
        self.assertIn("更新时间：06月22日 09:00", report)
        self.assertIn("数据来源：东方财富 AI智能体(BK0809)", report)

    def test_filter_candidates_applies_explicit_float_market_cap_parameters(self):
        rows = [
            {"symbol": "603956", "name": "威派格", "price": 5.3, "turnover": 100000000, "float_market_cap": 3020503316},
            {"symbol": "600186", "name": "莲花控股", "price": 15.6, "turnover": 2200000000, "float_market_cap": 27910557340},
            {"symbol": "600000", "name": "未知市值", "price": 10.0, "turnover": 100000000},
        ]

        strategy = default_strategy_config()
        strategy["parameters"]["float_market_cap_min"] = {"enabled": True, "value": 2_000_000_000}
        strategy["parameters"]["float_market_cap_max"] = {"enabled": True, "value": 10_000_000_000}

        filtered = filter_candidates(rows, strategy=strategy)

        self.assertEqual([row["symbol"] for row in filtered], ["603956"])

    def test_evaluate_tick_ignition_detects_10s_price_volume_surge(self):
        ticks = []
        for second in range(0, 120, 10):
            ticks.append({"time": f"09:36:{second % 60:02d}", "price": 10.0, "volume": 50})
        ticks.extend([
            {"time": "09:38:00", "price": 10.00, "volume": 100},
            {"time": "09:38:05", "price": 10.12, "volume": 500},
            {"time": "09:38:10", "price": 10.24, "volume": 500},
        ])

        signal = evaluate_tick_ignition(ticks)

        self.assertTrue(signal["confirmed"])
        self.assertGreaterEqual(signal["price_change_10s"], 2.0)
        self.assertGreaterEqual(signal["volume_ratio"], 8.0)

    def test_evaluate_tick_ignition_rejects_flat_price(self):
        ticks = [
            {"time": "09:36:00", "price": 10.0, "volume": 100},
            {"time": "09:36:10", "price": 10.0, "volume": 100},
            {"time": "09:38:00", "price": 10.0, "volume": 1000},
            {"time": "09:38:10", "price": 10.0, "volume": 1000},
        ]

        signal = evaluate_tick_ignition(ticks)

        self.assertFalse(signal["confirmed"])
        self.assertLess(signal["price_change_10s"], 2.0)

    def test_agent_context_includes_market_cap_and_price_position(self):
        def board_fetcher(board_code, **kwargs):
            return [
                {
                    "symbol": "603956",
                    "name": "威派格",
                    "sector": "AI智能体",
                    "price": 5.3,
                    "percent": 4.2,
                    "change": 0.2,
                    "volume": 100000,
                    "turnover": 53000000,
                    "turnover_rate": 3.2,
                    "pe": 18.5,
                    "amplitude": 5.0,
                    "open": 5.0,
                    "prev_close": 5.1,
                    "float_market_cap": 3020503316,
                    "source": "东方财富 AI智能体(BK0809)",
                }
            ], None

        context = generate_agent_context(board_fetcher=board_fetcher)
        payload = extract_market_payload(context)
        candidate = payload["candidates"][0]

        self.assertEqual(candidate["float_market_cap_cny"], 3020503316)
        self.assertEqual(candidate["volume_hands"], 100000)
        self.assertTrue(candidate["price_position"]["above_open"])
        self.assertTrue(candidate["price_position"]["above_zero_line"])
        self.assertTrue(candidate["price_position"]["above_vwap"])

    def test_agent_context_can_include_tick_ignition_signal(self):
        def board_fetcher(board_code, **kwargs):
            return [
                {
                    "symbol": "603956",
                    "name": "威派格",
                    "sector": "AI智能体",
                    "price": 5.3,
                    "percent": 4.2,
                    "change": 0.2,
                    "volume": 100000,
                    "turnover": 53000000,
                    "turnover_rate": 3.2,
                    "pe": 18.5,
                    "amplitude": 5.0,
                    "open": 5.0,
                    "prev_close": 5.1,
                    "float_market_cap": 3020503316,
                    "source": "东方财富 AI智能体(BK0809)",
                }
            ], None

        def tick_fetcher(symbol):
            return [
                {"time": "09:36:00", "price": 10.0, "volume": 50},
                {"time": "09:36:10", "price": 10.0, "volume": 50},
                {"time": "09:38:00", "price": 10.0, "volume": 100},
                {"time": "09:38:05", "price": 10.12, "volume": 500},
                {"time": "09:38:10", "price": 10.24, "volume": 500},
            ]

        context = generate_agent_context(
            board_fetcher=board_fetcher,
            enable_tick=True,
            tick_fetcher=tick_fetcher,
        )
        payload = extract_market_payload(context)

        self.assertTrue(payload["candidates"][0]["ignition_signal"]["confirmed"])
        self.assertGreaterEqual(payload["candidates"][0]["ignition_signal"]["volume_ratio"], 8.0)

    def test_agent_context_marks_tick_fetch_failure_without_crashing(self):
        def board_fetcher(board_code, **kwargs):
            return [
                {
                    "symbol": "603956",
                    "name": "威派格",
                    "sector": "AI智能体",
                    "price": 5.3,
                    "percent": 4.2,
                    "change": 0.2,
                    "volume": 100000,
                    "turnover": 53000000,
                    "turnover_rate": 3.2,
                    "pe": 18.5,
                    "amplitude": 5.0,
                    "open": 5.0,
                    "prev_close": 5.1,
                    "float_market_cap": 3020503316,
                    "source": "东方财富 AI智能体(BK0809)",
                }
            ], None

        def tick_fetcher(symbol):
            raise TimeoutError("tick timeout")

        context = generate_agent_context(
            board_fetcher=board_fetcher,
            enable_tick=True,
            tick_fetcher=tick_fetcher,
        )
        payload = extract_market_payload(context)
        signal = payload["candidates"][0]["ignition_signal"]

        self.assertFalse(signal["confirmed"])
        self.assertIn("tick timeout", signal["reason"])

    def test_agent_context_limits_tick_fetches(self):
        def board_fetcher(board_code, **kwargs):
            rows = []
            for symbol in ["603956", "603990"]:
                rows.append(
                    {
                        "symbol": symbol,
                        "name": symbol,
                        "sector": "AI智能体",
                        "price": 5.3,
                        "percent": 4.2,
                        "change": 0.2,
                        "volume": 100000,
                        "turnover": 53000000,
                        "turnover_rate": 3.2,
                        "pe": 18.5,
                        "amplitude": 5.0,
                        "open": 5.0,
                        "prev_close": 5.1,
                        "float_market_cap": 3020503316,
                        "source": "东方财富 AI智能体(BK0809)",
                    }
                )
            return rows, None

        seen = []

        def tick_fetcher(symbol):
            seen.append(symbol)
            return [
                {"time": "09:36:00", "price": 10.0, "volume": 50},
                {"time": "09:38:00", "price": 10.0, "volume": 100},
                {"time": "09:38:10", "price": 10.24, "volume": 500},
            ]

        context = generate_agent_context(
            board_fetcher=board_fetcher,
            enable_tick=True,
            tick_fetcher=tick_fetcher,
            tick_limit=1,
        )
        payload = extract_market_payload(context)

        self.assertEqual(seen, ["603956"])
        self.assertIsNotNone(payload["candidates"][0]["ignition_signal"])
        self.assertIsNone(payload["candidates"][1]["ignition_signal"])

    def test_conservative_report_explains_candidate_reasons_and_position(self):
        payload = {
            "board_name": "AI智能体",
            "candidates": [
                {
                    "symbol": "600186",
                    "name": "莲花控股",
                    "change_percent": 10.01,
                    "turnover_rate": 7.81,
                    "turnover_cny": 2100000000,
                    "pe": 48.73,
                    "signal_score": 92,
                    "float_market_cap_cny": 3020503316,
                    "price_position": {"above_open": True, "above_zero_line": True, "above_vwap": True},
                    "ignition_signal": {"confirmed": True, "price_change_10s": 2.4, "volume_ratio": 11.0, "reason": "10秒价量点火确认"},
                    "machine_reasons": ["强势拉升 10.01%，短线热度很高"],
                },
                {
                    "symbol": "600060",
                    "name": "海信视像",
                    "change_percent": 3.86,
                    "turnover_rate": 2.4,
                    "turnover_cny": 530000000,
                    "pe": 13.9,
                    "signal_score": 72,
                    "machine_reasons": ["换手率 2.40%，流动性较好"],
                },
            ],
        }

        report = build_conservative_report(payload, "测试风控")

        self.assertIn("今日情绪：强", report)
        self.assertIn("总仓位框架：8成", report)
        self.assertIn("### 莲花控股 (600186)", report)
        self.assertIn("**入选理由**", report)
        self.assertIn("涨幅 10.01%", report)
        self.assertIn("涨幅已经超过 7% 追高阈值", report)
        self.assertIn("流通市值 30.2 亿", report)
        self.assertIn("价格位置：开盘价上方、0轴上方、均价线上方", report)
        self.assertIn("点火信号：确认", report)
        self.assertIn("10秒涨幅 2.40%", report)
        self.assertIn("单股仓位：不超过1成", report)
        self.assertIn("### 海信视像 (600060)", report)
        self.assertIn("涨幅和换手有启动特征", report)
        self.assertIn("当前版本已尝试接入 10 秒逐笔成交", report)

    def test_agent_context_contains_structured_market_data_not_final_recommendation(self):
        def board_fetcher(board_code, **kwargs):
            return [
                {
                    "symbol": "300130",
                    "name": "新国都",
                    "sector": "AI智能体",
                    "price": 23.69,
                    "percent": 14.28,
                    "change": 2.96,
                    "volume": 100,
                    "turnover": 986014953.57,
                    "turnover_rate": 9.87,
                    "pe": 31.5,
                    "amplitude": 12.3,
                    "source": "东方财富 AI智能体(BK0809)",
                }
            ], None

        context = generate_agent_context(
            now=datetime(2026, 6, 22, 1, 0, tzinfo=timezone.utc),
            board_fetcher=board_fetcher,
            candidate_limit=1,
        )

        self.assertIn("MARKET_DATA_JSON", context)
        self.assertIn('"board_code": "BK0800"', context)
        self.assertIn('"symbol": "300130"', context)
        self.assertIn('"signal_score"', context)
        self.assertNotIn("【推荐 #1】", context)
        self.assertIn("请解释 factor_rank_v1", context)

    def test_generate_ai_report_uses_llm_result(self):
        def board_fetcher(board_code, **kwargs):
            return [
                {
                    "symbol": "300130",
                    "name": "新国都",
                    "sector": "AI智能体",
                    "price": 23.69,
                    "percent": 14.28,
                    "change": 2.96,
                    "volume": 100,
                    "turnover": 986014953.57,
                    "turnover_rate": 9.87,
                    "pe": 31.5,
                    "amplitude": 12.3,
                    "source": "东方财富 AI智能体(BK0809)",
                }
            ], None

        def fake_llm(context, **kwargs):
            self.assertIn("MARKET_DATA_JSON", context)
            self.assertIn("300130", context)
            return "AI最终推荐：新国都（300130），观望或轻仓。"

        report = generate_ai_report(
            now=datetime(2026, 6, 22, 1, 0, tzinfo=timezone.utc),
            board_fetcher=board_fetcher,
            llm_client=fake_llm,
        )

        self.assertIn("AI最终推荐：新国都（300130），观望或轻仓。", report)
        self.assertIn("板块状态", report)
        self.assertIn("推荐股每小时成交与涨跌跟踪", report)
        self.assertIn("涨跌幅 +14.28%", report)
        self.assertIn("成交量 100 手", report)
        self.assertIn("成交额 9.9 亿", report)

    def test_generate_ai_report_falls_back_when_llm_fails(self):
        def broken_llm(context, **kwargs):
            raise TimeoutError("slow model")

        def board_fetcher(board_code, **kwargs):
            return [
                {
                    "symbol": "300130",
                    "name": "新国都",
                    "sector": "AI智能体",
                    "price": 23.69,
                    "percent": 14.28,
                    "change": 2.96,
                    "volume": 100,
                    "turnover": 986014953.57,
                    "turnover_rate": 9.87,
                    "pe": 31.5,
                    "amplitude": 12.3,
                    "source": "东方财富 AI智能体(BK0809)",
                }
            ], None

        report = generate_ai_report(
            now=datetime(2026, 6, 22, 1, 0, tzinfo=timezone.utc),
            llm_client=broken_llm,
            board_fetcher=board_fetcher,
            strategy={
                "id": "strategy-ai-tech",
                "name": "科技 AI",
                "revision": 2,
                "lifecycle": {"stage": "paper"},
                "signal": {"model": "factor_rank_v1", "run_time": "08:00"},
                "portfolio": {
                    "max_positions": 10,
                    "target_weight_pct": 10,
                    "stop_loss_pct": 8,
                    "trailing_activation_pct": 10,
                    "trailing_drawdown_pct": 5,
                },
                "execution": {"t_plus_one": True},
            },
        )

        self.assertIn("AI 解说暂不可用", report)
        self.assertIn("确定性策略入场计划", report)
        self.assertIn("新国都 (300130)", report)
        self.assertIn("最多持有 10 只", report)
        self.assertIn("目标单股仓位 10%", report)
        self.assertIn("止损 8%", report)
        self.assertIn("T+1", report)
        self.assertIn("工作日 08:00", report)
        self.assertNotIn("脚本兜底报告", report)
        self.assertNotIn("股票每日推荐", report)
        self.assertNotIn("slow model", report)

    def test_generate_ai_report_stops_when_market_data_unavailable(self):
        called = False

        def llm_should_not_run(context, **kwargs):
            nonlocal called
            called = True
            return "should not happen"

        report = generate_ai_report(
            now=datetime(2026, 6, 22, 1, 0, tzinfo=timezone.utc),
            board_fetcher=lambda board_code, **kwargs: ([], "remote disconnected"),
            fallback_fetcher=lambda **kwargs: ([], "sina disconnected"),
            llm_client=llm_should_not_run,
        )

        self.assertFalse(called)
        self.assertIn("实时行情不可用", report)
        self.assertIn("今日不生成股票推荐", report)

    def test_generate_ai_report_overrides_risky_heavy_position(self):
        def board_fetcher(board_code, **kwargs):
            return [
                {
                    "symbol": "300130",
                    "name": "新国都",
                    "sector": "AI智能体",
                    "price": 23.69,
                    "percent": 14.28,
                    "change": 2.96,
                    "volume": 100,
                    "turnover": 986014953.57,
                    "turnover_rate": 9.87,
                    "pe": 31.5,
                    "amplitude": 12.3,
                    "source": "东方财富 AI智能体(BK0809)",
                }
            ], None

        def unsafe_llm(context, **kwargs):
            return "推荐新国都，建议重仓试错。"

        report = generate_ai_report(
            now=datetime(2026, 6, 22, 1, 0, tzinfo=timezone.utc),
            board_fetcher=board_fetcher,
            llm_client=unsafe_llm,
        )

        self.assertIn("系统风控修正", report)
        self.assertIn("不建议追高", report)
        self.assertNotIn("重仓试错", report)

    def test_generate_ai_report_rejects_unknown_symbol(self):
        def board_fetcher(board_code, **kwargs):
            return [
                {
                    "symbol": "300130",
                    "name": "新国都",
                    "sector": "AI智能体",
                    "price": 23.69,
                    "percent": 14.28,
                    "change": 2.96,
                    "volume": 100,
                    "turnover": 986014953.57,
                    "turnover_rate": 9.87,
                    "pe": 31.5,
                    "amplitude": 12.3,
                    "source": "东方财富 AI智能体(BK0809)",
                }
            ], None

        def hallucinating_llm(context, **kwargs):
            return "推荐 999999 幻觉股票，建议观望。"

        report = generate_ai_report(
            now=datetime(2026, 6, 22, 1, 0, tzinfo=timezone.utc),
            board_fetcher=board_fetcher,
            llm_client=hallucinating_llm,
        )

        self.assertIn("数据一致性校验失败", report)
        self.assertNotIn("999999", report)

    def test_select_agent_candidates_includes_moderate_alternatives(self):
        rows = [
            {"symbol": "A", "name": "高热1", "price": 1, "percent": 12, "turnover": 2_000_000_000, "turnover_rate": 10, "pe": 20, "source": "x"},
            {"symbol": "B", "name": "高热2", "price": 1, "percent": 9, "turnover": 1_000_000_000, "turnover_rate": 8, "pe": 20, "source": "x"},
            {"symbol": "C", "name": "稳健1", "price": 1, "percent": 3, "turnover": 500_000_000, "turnover_rate": 4, "pe": 20, "source": "x"},
            {"symbol": "D", "name": "稳健2", "price": 1, "percent": 1, "turnover": 300_000_000, "turnover_rate": 3, "pe": 20, "source": "x"},
        ]
        for index, row in enumerate(rows):
            row["signal_features"] = {
                "momentum20": 4 - index,
                "momentum60": 4 - index,
                "trend": 2,
                "volume_ratio": 4 - index / 2,
                "inverse_volatility": index,
                "drawdown": -index / 100,
            }
        analyses = analyze_candidates(rows)
        selected = select_agent_candidates(analyses, limit=3)

        self.assertIn("C", [row["symbol"] for row in selected])
        self.assertIn("A", [row["symbol"] for row in selected])


if __name__ == "__main__":
    unittest.main()
