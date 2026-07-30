import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from stock_recommender.context import (
    collect_recommendation_plan,
    enrich_recommendation_plan_with_ticks,
)
from stock_recommender.data_sources import fetch_nasdaq100_quotes, fetch_sina_us_quotes
from stock_recommender.delivery import portfolio_delivery_crons
from stock_recommender.enrichment import _download_sina_us_daily_history, fetch_daily_history
from stock_recommender.market_adapters import (
    AShareMarketAdapter,
    MarketAdapter,
    UsStockMarketAdapter,
    get_market_adapter,
)
from stock_recommender.markets import US_MARKET, is_market_open, order_session_date
from stock_recommender.parameters import (
    catalog_payload,
    convert_strategy_text,
    default_strategy_config,
)
from stock_recommender.portfolio import (
    create_portfolio_account,
    plan_daily_candidates,
    process_market_snapshot,
)
from stock_recommender.schedule import should_publish_now
from stock_recommender.tracking import load_daily_selection_state
from stock_recommender.universe import normalize_stock_symbol, normalize_watchlist
from stock_recommender.universe_provider import (
    BoardUniverseSnapshotStore,
    Nasdaq100UniverseProvider,
    UniverseQuoteBatch,
)
from stock_recommender.us_data_providers import (
    AlpacaMarketDataClient,
    FailoverUsMarketDataProvider,
    us_market_data_status,
)

from recommendation_fixtures import FULL_EXPOSURE_MARKET_REGIME


SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


def us_strategy() -> dict:
    strategy = default_strategy_config()
    strategy.update({"id": "us-tech", "name": "US Tech", "revision": 2})
    strategy["lifecycle"]["stage"] = "paper"
    strategy["parameters"]["market"] = {"enabled": True, "value": "us"}
    strategy["parameters"]["board_code"] = {"enabled": True, "value": "NASDAQ100"}
    strategy["parameters"]["board_name"] = {"enabled": True, "value": "纳斯达克100"}
    strategy["allocation"]["minimum_universe_size"] = 1
    return strategy


def signal_history(symbol: str) -> list[dict]:
    offset = sum(ord(character) for character in symbol) / 100_000
    start = date(2026, 3, 1)
    return [
        {
            "date": start + timedelta(days=index),
            "open": 20 + index * (0.08 + offset),
            "close": 20 + index * (0.08 + offset),
            "high": 20.2 + index * (0.08 + offset),
            "low": 19.8 + index * (0.08 + offset),
            "volume": 1_000_000 + index * 1_000,
        }
        for index in range(120)
    ]


class UsMarketAdapterTests(unittest.TestCase):
    def test_registry_exposes_one_contract_for_both_markets(self):
        cn = get_market_adapter("cn")
        us = get_market_adapter("us")

        self.assertIsInstance(cn, MarketAdapter)
        self.assertIsInstance(us, MarketAdapter)
        self.assertIsInstance(cn, AShareMarketAdapter)
        self.assertIsInstance(us, UsStockMarketAdapter)
        self.assertEqual(cn.profile.lot_size, 100)
        self.assertEqual(us.profile.lot_size, 1)
        self.assertTrue(us.profile.same_day_sell)
        self.assertEqual(us.resolve_universe(None), ("NASDAQ100", "纳斯达克100"))
        self.assertEqual(us.resolve_universe(us_strategy()), ("NASDAQ100", "纳斯达克100"))

    def test_us_symbols_and_catalog_are_market_aware(self):
        self.assertEqual(normalize_stock_symbol("brk.b", "us"), "BRK.B")
        self.assertEqual(
            [item["symbol"] for item in normalize_watchlist("aapl, msft, nvda", market="us")],
            ["AAPL", "MSFT", "NVDA"],
        )
        strategy = us_strategy()
        payload = catalog_payload(strategy)
        parameters = {item["id"]: item for item in payload["parameters"]}

        self.assertEqual(payload["market"]["currency"], "USD")
        self.assertEqual(parameters["price_min"]["unit"], "$")
        self.assertFalse(parameters["stock_prefixes"]["applicable"])
        self.assertFalse(parameters["exclude_st"]["applicable"])
        self.assertFalse(parameters["turnover_rate_min"]["applicable"])
        self.assertFalse(parameters["float_market_cap_min"]["applicable"])
        self.assertFalse(parameters["ignition_price_10s_min"]["applicable"])
        self.assertFalse(parameters["roe_min"]["applicable"])
        self.assertFalse(parameters["roe_min"]["effective"])
        updates = {
            item["id"]: item["value"]
            for item in convert_strategy_text("使用美股纳斯达克科技策略")["updates"]
        }
        self.assertEqual(updates["market"], "us")

    def test_nasdaq_membership_parser_preserves_downward_sign(self):
        payload = {
            "data": {
                "data": {
                    "rows": [
                        {
                            "symbol": "AAPL",
                            "companyName": "Apple Inc.",
                            "lastSalePrice": "$210.00",
                            "percentageChange": "0.56%",
                            "netChange": "$1.19",
                            "deltaIndicator": "down",
                            "marketCap": "$3,000,000,000,000",
                        }
                    ]
                }
            }
        }

        rows, error = fetch_nasdaq100_quotes(
            urlopen_func=lambda request, timeout: FakeResponse(json.dumps(payload).encode()),
        )

        self.assertIsNone(error)
        self.assertEqual(rows[0]["symbol"], "AAPL")
        self.assertEqual(rows[0]["percent"], -0.56)
        self.assertEqual(rows[0]["change"], -1.19)
        self.assertEqual(rows[0]["currency"], "USD")

    def test_sina_us_quote_parser_returns_shares_and_usd_turnover(self):
        fields = [""] * 31
        fields[0] = "Apple Inc."
        fields[1] = "210.5"
        fields[2] = "1.25"
        fields[3] = "2026-07-29 16:00:00"
        fields[4] = "2.6"
        fields[5] = "208"
        fields[6] = "212"
        fields[7] = "207"
        fields[10] = "9999999"
        fields[12] = "3000000000000"
        fields[14] = "31.5"
        fields[26] = "207.9"
        fields[27] = "1234567"
        fields[30] = "260000000"
        raw = f'var hq_str_gb_aapl="{",".join(fields)}";'.encode("gb18030")

        rows, error = fetch_sina_us_quotes(
            symbols=[{"symbol": "AAPL", "sector": "AI"}],
            urlopen_func=lambda request, timeout: FakeResponse(raw),
        )

        self.assertIsNone(error)
        self.assertEqual(rows[0]["volume"], 1_234_567)
        self.assertEqual(rows[0]["turnover"], 260_000_000)
        self.assertEqual(rows[0]["sector"], "AI")
        self.assertEqual(rows[0]["market"], "us")

    def test_us_history_uses_isolated_cache_namespace(self):
        rows = [
            {
                "date": date(2026, 7, 29),
                "open": 200,
                "close": 210,
                "high": 212,
                "low": 198,
                "volume": 1_000_000,
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = fetch_daily_history(
                "AAPL",
                market="us",
                cache_dir=directory,
                downloader=lambda symbol: rows,
                attempts=1,
            )
            target = Path(directory) / "us" / "AAPL.json"

            self.assertEqual(result[0]["close"], 210)
            self.assertTrue(target.exists())

    def test_us_history_jsonp_parser_is_bounded_and_normalized(self):
        payload = [
            {
                "d": "2026-07-29",
                "o": "200",
                "c": "210",
                "h": "212",
                "l": "198",
                "v": "1000000",
                "a": "208000000",
            }
        ]
        raw = f"var _AAPL=({json.dumps(payload)});".encode()
        with mock.patch(
            "stock_recommender.enrichment.urllib.request.urlopen",
            return_value=FakeResponse(raw),
        ) as opener:
            rows = _download_sina_us_daily_history("AAPL")

        self.assertEqual(rows[0]["date"], "2026-07-29")
        self.assertEqual(rows[0]["close"], "210")
        self.assertEqual(rows[0]["source"], "新浪财经美股日线（降级源）")
        self.assertEqual(opener.call_args.kwargs["timeout"], 8.0)

    def test_alpaca_snapshot_parser_maps_realtime_price_and_daily_volume(self):
        captured = {}
        payload = {
            "snapshots": {
                "AAPL": {
                    "latestTrade": {
                        "p": 213.25,
                        "t": "2026-07-30T15:45:00Z",
                    },
                    "minuteBar": {"c": 213.2, "t": "2026-07-30T15:45:00Z"},
                    "dailyBar": {
                        "o": 210,
                        "h": 214,
                        "l": 209,
                        "c": 213.2,
                        "v": 12_345_678,
                        "vw": 212.5,
                    },
                    "prevDailyBar": {"c": 208.5},
                }
            }
        }

        def opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(json.dumps(payload).encode())

        client = AlpacaMarketDataClient(
            api_key="test-key",
            api_secret="test-secret",
            urlopen_func=opener,
            now_func=lambda: datetime(
                2026,
                7,
                30,
                15,
                50,
                tzinfo=ZoneInfo("UTC"),
            ),
        )
        rows = client.fetch_quotes(
            symbols=[{"symbol": "AAPL", "name": "Apple", "sector": "AI"}],
        )

        headers = {
            key.lower(): value for key, value in captured["request"].header_items()
        }
        self.assertEqual(headers["apca-api-key-id"], "test-key")
        self.assertEqual(headers["apca-api-secret-key"], "test-secret")
        self.assertIn("feed=iex", captured["request"].full_url)
        self.assertEqual(captured["timeout"], 8.0)
        self.assertEqual(rows[0]["price"], 213.25)
        self.assertEqual(rows[0]["prev_close"], 208.5)
        self.assertEqual(rows[0]["volume"], 12_345_678)
        self.assertEqual(rows[0]["turnover"], 2_623_456_575)
        self.assertEqual(rows[0]["source"], "Alpaca IEX 美股行情")

    def test_alpaca_rejects_stale_intraday_snapshot(self):
        payload = {
            "snapshots": {
                "AAPL": {
                    "latestTrade": {
                        "p": 213.25,
                        "t": "2026-07-30T14:00:00Z",
                    },
                    "dailyBar": {"c": 213.25, "v": 1_000_000},
                    "prevDailyBar": {"c": 208.5},
                }
            }
        }
        client = AlpacaMarketDataClient(
            api_key="test-key",
            api_secret="test-secret",
            urlopen_func=lambda request, timeout: FakeResponse(
                json.dumps(payload).encode()
            ),
            now_func=lambda: datetime(
                2026,
                7,
                30,
                16,
                0,
                tzinfo=ZoneInfo("UTC"),
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "时间戳过期"):
            client.fetch_quotes(symbols=["AAPL"])

    def test_alpaca_history_parser_returns_adjusted_daily_rows(self):
        payload = {
            "bars": [
                {
                    "t": "2026-07-29T04:00:00Z",
                    "o": 205,
                    "h": 211,
                    "l": 204,
                    "c": 210,
                    "v": 1_000_000,
                    "vw": 208,
                }
            ],
            "next_page_token": None,
        }
        client = AlpacaMarketDataClient(
            api_key="test-key",
            api_secret="test-secret",
            urlopen_func=lambda request, timeout: FakeResponse(
                json.dumps(payload).encode()
            ),
        )

        rows = client.fetch_daily_history("AAPL")

        self.assertEqual(rows[0]["date"], "2026-07-29")
        self.assertEqual(rows[0]["close"], 210)
        self.assertEqual(rows[0]["turnover"], 208_000_000)
        self.assertEqual(rows[0]["source"], "Alpaca IEX 美股日线")

    def test_alpaca_failover_uses_sina_only_when_primary_is_unavailable(self):
        primary = mock.Mock()
        fallback = mock.Mock()
        primary.provider_id = "alpaca"
        fallback.provider_id = "sina"
        primary.fetch_quotes.return_value = ([], "Alpaca timeout")
        fallback.fetch_quotes.return_value = (
            [
                {
                    "symbol": "AAPL",
                    "price": 210,
                    "source": "新浪财经美股实时行情（降级源）",
                }
            ],
            None,
        )
        primary.fetch_daily_history.side_effect = RuntimeError("Alpaca timeout")
        fallback.fetch_daily_history.return_value = signal_history("AAPL")
        provider = FailoverUsMarketDataProvider(primary, fallback)

        quotes, error = provider.fetch_quotes(symbols=["AAPL"])
        history = provider.fetch_daily_history("AAPL")

        self.assertEqual(quotes[0]["source"], "新浪财经美股实时行情（降级源）")
        self.assertIn("已降级至 新浪", error)
        self.assertEqual(len(history), 120)
        fallback.fetch_quotes.assert_called_once()
        fallback.fetch_daily_history.assert_called_once_with("AAPL")

    def test_market_data_status_exposes_configuration_without_secrets(self):
        with mock.patch.dict(
            "os.environ",
            {
                "STOCK_AGENT_ALPACA_API_KEY_ID": "key",
                "STOCK_AGENT_ALPACA_API_SECRET_KEY": "secret",
                "STOCK_AGENT_ALPACA_FEED": "iex",
            },
            clear=False,
        ):
            status = us_market_data_status()

        self.assertEqual(status["primary"], "alpaca")
        self.assertEqual(status["mode"], "primary_ready")
        self.assertTrue(status["alpaca_configured"])
        self.assertNotIn("key", status)
        self.assertNotIn("secret", status)

    def test_nasdaq_provider_rejects_partial_membership_and_uses_fresh_snapshot(self):
        membership = [
            {"symbol": f"T{chr(65 + index // 26)}{chr(65 + index % 26)}", "name": f"Stock {index}"}
            for index in range(30)
        ]

        def quotes(*, symbols, **kwargs):
            return (
                [
                    {
                        **item,
                        "price": 100,
                        "volume": 1_000_000,
                        "turnover": 100_000_000,
                    }
                    for item in symbols
                ],
                None,
            )

        with tempfile.TemporaryDirectory() as directory:
            store = BoardUniverseSnapshotStore(directory)
            provider = Nasdaq100UniverseProvider(
                primary_fetcher=lambda *args, **kwargs: (membership, None),
                quote_fetcher=quotes,
                snapshot_store=store,
                universe_limit=30,
            )
            first = provider.fetch(
                now=datetime(2026, 7, 30, tzinfo=ZoneInfo("UTC")),
            )
            provider.primary_fetcher = lambda *args, **kwargs: (
                membership[:5],
                None,
            )
            fallback = provider.fetch(
                now=datetime(2026, 7, 31, tzinfo=ZoneInfo("UTC")),
            )

        self.assertEqual(first.mode, "primary")
        self.assertEqual(len(first.rows), 30)
        self.assertEqual(fallback.mode, "snapshot_realtime")
        self.assertEqual(len(fallback.rows), 30)
        self.assertIn("覆盖不足", fallback.primary_error)

    def test_pipeline_consumes_adapter_without_vendor_specific_branching(self):
        history_calls = []

        class DeterministicUsAdapter(UsStockMarketAdapter):
            def fetch_universe(self, strategy, **kwargs):
                symbols = tuple(
                    f"T{chr(65 + index // 26)}{chr(65 + index % 26)}"
                    for index in range(30)
                )
                rows = tuple(
                    {
                        "symbol": symbol,
                        "name": symbol,
                        "sector": "AI",
                        "sectors": ["AI"],
                        "price": 100 + index,
                        "percent": 1 + index / 10,
                        "change": 1,
                        "volume": 2_000_000,
                        "turnover": 200_000_000,
                        "source": "adapter-fixture",
                    }
                    for index, symbol in enumerate(symbols)
                )
                return UniverseQuoteBatch(
                    rows=rows,
                    mode="adapter_fixture",
                    board_code="NASDAQ100",
                    board_name="纳斯达克100",
                    snapshot_count=len(rows),
                    market="us",
                )

            def fetch_history(self, symbol, **kwargs):
                history_calls.append(symbol)
                return [
                    *signal_history(symbol),
                    {
                        "date": date(2026, 7, 30),
                        "open": 999,
                        "close": 1_000,
                        "high": 1_001,
                        "low": 998,
                        "volume": 99_000_000,
                    },
                ]

        strategy = us_strategy()
        strategy["parameters"]["roe_min"] = {"enabled": True, "value": 5}
        with mock.patch.dict(
            "stock_recommender.market_adapters._ADAPTERS",
            {"us": DeterministicUsAdapter()},
        ):
            plan = collect_recommendation_plan(
                now=datetime(2026, 7, 31, 2, 0, tzinfo=SHANGHAI),
                strategy=strategy,
                candidate_limit=3,
                selection_limit=3,
            )

        self.assertEqual(plan.market, "us")
        self.assertEqual(plan.board_code, "NASDAQ100")
        self.assertEqual(plan.data_quality["source_mode"], "adapter_fixture")
        self.assertGreater(len(plan.candidates), 0)
        self.assertTrue(all(item["symbol"].isalpha() for item in plan.candidates))
        self.assertEqual(len(history_calls), 30)
        self.assertTrue(
            all(
                item["signal_features"]["history_latest_date"] != "2026-07-30"
                for item in plan.candidates
            )
        )
        unchanged = enrich_recommendation_plan_with_ticks(
            plan,
            tick_fetcher=lambda symbol: self.fail(
                f"美股不应调用 A 股逐笔数据源：{symbol}"
            ),
        )
        self.assertIs(unchanged, plan)

    def test_us_portfolio_uses_whole_shares_zero_commission_and_same_day_sellable(self):
        strategy = us_strategy()
        signal_time = datetime(2026, 7, 30, 8, 0, tzinfo=SHANGHAI)
        open_time = datetime(2026, 7, 30, 9, 35, tzinfo=ZoneInfo("America/New_York"))
        account = create_portfolio_account(strategy, now=signal_time)
        account, _ = plan_daily_candidates(
            strategy,
            [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "price": 200,
                    "score": 0.9,
                    "signal_features": {"momentum20": 0.1, "trend": 2},
                }
            ],
            now=signal_time,
            account=account,
            market_regime=FULL_EXPOSURE_MARKET_REGIME,
        )
        account, events = process_market_snapshot(
            strategy,
            [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "price": 201,
                    "volume": 10_000_000,
                    "bar_volume": 10_000_000,
                    "bar_open": 201,
                    "bar_high": 202,
                    "bar_low": 200,
                }
            ],
            now=open_time,
            account=account,
        )
        position = account["positions"]["AAPL"]
        fill = next(event for event in events if event["type"] == "ORDER_FILLED")

        self.assertEqual(account["market"], "us")
        self.assertEqual(account["currency"], "USD")
        self.assertGreater(position["quantity"], 0)
        self.assertEqual(position["sellable_quantity"], position["quantity"])
        self.assertEqual(fill["data"]["fees"], 0)

    def test_us_session_guards_cover_dst_and_delivery_window(self):
        summer_open = datetime(2026, 7, 30, 22, 0, tzinfo=SHANGHAI)
        summer_closed = datetime(2026, 7, 31, 4, 1, tzinfo=SHANGHAI)
        after_close = datetime(
            2026,
            7,
            29,
            17,
            0,
            tzinfo=ZoneInfo("America/New_York"),
        )

        self.assertTrue(is_market_open(summer_open, US_MARKET))
        self.assertFalse(is_market_open(summer_closed, US_MARKET))
        self.assertEqual(order_session_date(after_close, US_MARKET), date(2026, 7, 30))
        self.assertTrue(should_publish_now(summer_open, market=US_MARKET))
        self.assertEqual(
            portfolio_delivery_crons(us_strategy()),
            ("0 13-21 * * 1-5", "*/5 13-21 * * 1-5"),
        )

    def test_us_daily_selection_survives_beijing_midnight_during_session(self):
        payload = {
            "trade_date": "2026-07-30",
            "market": "us",
            "symbols": ["AAPL"],
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "selection.json"
            target.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_daily_selection_state(
                target,
                now=datetime(2026, 7, 31, 2, 0, tzinfo=SHANGHAI),
            )

        self.assertEqual(loaded["symbols"], ["AAPL"])


if __name__ == "__main__":
    unittest.main()
