import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from stock_recommender.admin import _serialize_strategy_performance
from stock_recommender.portfolio_engine.borrow import (
    AVAILABLE,
    BorrowSecurity,
    BorrowSnapshot,
)
from stock_recommender.portfolio_engine.config import (
    default_exposure_policy,
    default_margin_policy,
    default_short_policy,
)
from stock_recommender.portfolio_engine.contracts import (
    AccountSnapshot,
    DecisionBatch,
    MarketSnapshot,
    PerformanceProjectionRequest,
    PerformanceStrategySource,
    PortfolioSnapshot,
    PositionSide,
    PositionSnapshot,
    ProcessRequest,
)
from stock_recommender.portfolio_engine.ledger import JsonLedgerStore
from stock_recommender.portfolio_engine.service import PortfolioEngine
from stock_recommender.portfolio_engine.valuation import value_account
from stock_recommender.reports import (
    append_performance_link,
    format_portfolio_actions,
    format_portfolio_snapshot,
)


NOW = datetime(2026, 8, 3, 10, 0, tzinfo=ZoneInfo("America/New_York"))


def long_short_strategy():
    exposure = default_exposure_policy()
    exposure.update({"mode": "LONG_SHORT", "max_positions": 10})
    return {
        "version": 6,
        "id": "long-short-test",
        "name": "多空测试",
        "revision": 2,
        "market": "us",
        "signal": {"model": "factor_rank_v1"},
        "lifecycle": {"stage": "paper"},
        "exposure_policy": exposure,
        "margin_policy": default_margin_policy(),
        "short_policy": default_short_policy(),
        "portfolio": {"initial_cash": 1_000.0},
    }


def long_short_snapshot():
    positions = (
        PositionSnapshot(
            symbol="MSFT",
            side=PositionSide.LONG,
            quantity=9,
            average_cost=95.0,
            current_price=100.0,
            sellable_quantity=9,
        ),
        PositionSnapshot(
            symbol="PLTR",
            side=PositionSide.SHORT,
            quantity=3,
            average_cost=105.0,
            current_price=100.0,
        ),
    )
    account = AccountSnapshot(
        id="account-long-short-test",
        strategy_id="long-short-test",
        strategy_revision=2,
        occurred_at=NOW,
        available_cash=100.0,
        restricted_short_proceeds=300.0,
        margin_loan=100.0,
        accrued_financing_cost=12.5,
        accrued_borrow_cost=4.5,
        positions=positions,
        snapshot_id="portfolio-long-short-test",
    )
    valuation = value_account(account, {"MSFT": 100.0, "PLTR": 100.0})
    return PortfolioSnapshot(
        account=account,
        metrics=valuation.metrics,
        positions=valuation.positions,
        open_intents=(),
        recent_events=(),
    )


def _market(symbol: str, price: float, snapshot_id: str) -> MarketSnapshot:
    return MarketSnapshot(
        id=snapshot_id,
        occurred_at=NOW + timedelta(minutes=5),
        quotes={
            symbol: {
                "price": price,
                "bar_open": price,
                "bar_high": price,
                "bar_low": price,
                "bar_volume": 100_000,
                "percent": 0.0,
                "volume_ratio": 1.0,
            }
        },
    )


def _run_real_risk(
    path: Path,
    account: AccountSnapshot,
    market: MarketSnapshot,
    borrow: BorrowSnapshot,
) -> tuple[PortfolioEngine, DecisionBatch, PortfolioSnapshot]:
    store = JsonLedgerStore(path)
    store.create_account(account)
    engine = PortfolioEngine(ledger_store=store)
    batch = engine.process_and_commit(
        ProcessRequest(
            run_key=f"risk:{market.id}",
            strategy=long_short_strategy(),
            account=store.load("long-short-test"),
            market=market,
            borrow=borrow,
        )
    )
    return engine, batch, engine.performance("long-short-test", market)


def _margin_call_account() -> AccountSnapshot:
    return AccountSnapshot(
        id="account-long-short-test",
        strategy_id="long-short-test",
        strategy_revision=2,
        occurred_at=NOW,
        available_cash=-750.0,
        positions=(
            PositionSnapshot(
                symbol="MSFT",
                side=PositionSide.LONG,
                quantity=10,
                average_cost=100.0,
                current_price=100.0,
                sellable_quantity=10,
            ),
        ),
        snapshot_id="portfolio-margin-call",
    )


def _cover_only_account() -> AccountSnapshot:
    return AccountSnapshot(
        id="account-long-short-test",
        strategy_id="long-short-test",
        strategy_revision=2,
        occurred_at=NOW,
        available_cash=1_000.0,
        restricted_short_proceeds=1_000.0,
        positions=(
            PositionSnapshot(
                symbol="PLTR",
                side=PositionSide.SHORT,
                quantity=10,
                average_cost=100.0,
                current_price=100.0,
            ),
        ),
        snapshot_id="portfolio-cover-only",
    )


class LongShortNotificationTests(unittest.TestCase):
    def test_real_margin_call_batch_ledger_api_and_message_use_canonical_facts(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "portfolio.json"
            market = _market("MSFT", 100.0, "market-margin-call")
            engine, batch, snapshot = _run_real_risk(
                path,
                _margin_call_account(),
                market,
                BorrowSnapshot.unavailable("long position needs no borrow"),
            )
            ledger_event = next(
                item
                for item in JsonLedgerStore(path).load_performance_view(
                    "long-short-test"
                ).events
                if item.type == "RISK_CHANGED"
            )
            message = format_portfolio_actions(
                long_short_strategy(),
                batch,
                snapshot=snapshot,
                performance_url="https://stock.example/strategies/long-short-test/portfolio",
            )
            source = PerformanceStrategySource(
                id="long-short-test",
                name="多空测试",
                revision=2,
                stage="paper",
                market="us",
                market_label="美股",
                currency="USD",
                currency_symbol="$",
                initial_cash=1_000.0,
                max_positions=10,
                exposure_policy=long_short_strategy()["exposure_policy"],
                margin_policy=long_short_strategy()["margin_policy"],
                short_policy=long_short_strategy()["short_policy"],
            )
            projection = engine.performance_projection(
                PerformanceProjectionRequest(
                    strategy=source,
                    market=market,
                    generated_at=market.occurred_at,
                    valuation_source="test",
                )
            )
            payload = _serialize_strategy_performance(projection)
            public_event = next(
                item for item in payload["events"] if item["id"] == ledger_event.id
            )

        self.assertEqual(batch.events, ())
        self.assertEqual([item.reason for item in batch.intents], ["MARGIN_CALL"])
        self.assertTrue(batch.position_risk_updates)
        self.assertEqual(ledger_event.type, "RISK_CHANGED")
        self.assertIn("策略：多空测试 · v2", message)
        self.assertIn("保证金追缴", message)
        self.assertIn("强制去杠杆", message)
        self.assertIn("保证金率低于维持线", message)
        self.assertIn("https://stock.example/strategies/long-short-test/portfolio", message)
        self.assertEqual(public_event["type"], "RISK_CHANGED")
        self.assertEqual(public_event["data"]["reason"], "MARGIN_CALL")
        self.assertIn("强制去杠杆", public_event["message"])

    def test_real_mode_only_cover_update_is_not_silent_and_commits_risk_changed(self):
        unavailable = BorrowSnapshot(
            id="borrow-cover-only",
            status=AVAILABLE,
            securities={
                "PLTR": BorrowSecurity(
                    symbol="PLTR",
                    shortable=False,
                    easy_to_borrow=False,
                )
            },
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "portfolio.json"
            engine, batch, snapshot = _run_real_risk(
                path,
                _cover_only_account(),
                _market("PLTR", 100.0, "market-cover-only"),
                unavailable,
            )
            message = format_portfolio_actions(
                long_short_strategy(),
                batch,
                snapshot=snapshot,
            )
            events = JsonLedgerStore(path).load_performance_view(
                "long-short-test"
            ).events

        self.assertEqual(batch.intents, ())
        self.assertEqual(batch.events, ())
        self.assertEqual(batch.position_risk_updates[0].position_mode, "COVER_ONLY")
        self.assertTrue(any(item.type == "RISK_CHANGED" for item in events))
        self.assertIn("PLTR", message)
        self.assertIn("仅允许空头回补", message)

    def test_hourly_message_contains_direction_account_metrics_costs_and_link(self):
        message = format_portfolio_snapshot(
            long_short_strategy(),
            long_short_snapshot(),
            performance_url="https://stock.example/strategies/long-short-test/portfolio",
        )

        self.assertIn("策略：多空测试 · v2", message)
        self.assertIn("MSFT 多头", message)
        self.assertIn("PLTR 空头", message)
        self.assertIn("总敞口 133.33%", message)
        self.assertIn("净敞口 66.67%", message)
        self.assertIn("保证金率 75.00%", message)
        self.assertIn("融资成本 12.50", message)
        self.assertIn("借券成本 4.50", message)
        self.assertIn("https://stock.example/strategies/long-short-test/portfolio", message)

    def test_five_minute_risk_message_is_silent_without_actions_or_events(self):
        empty_account = AccountSnapshot(
            id="account-long-short-test",
            strategy_id="long-short-test",
            strategy_revision=2,
            occurred_at=NOW,
            available_cash=1_000.0,
            snapshot_id="portfolio-empty",
        )
        with TemporaryDirectory() as temporary:
            _, empty, snapshot = _run_real_risk(
                Path(temporary) / "portfolio.json",
                empty_account,
                MarketSnapshot(
                    id="market-empty",
                    occurred_at=NOW + timedelta(minutes=5),
                    quotes={},
                ),
                BorrowSnapshot.unavailable("empty account"),
            )

        self.assertEqual(
            format_portfolio_actions(
                long_short_strategy(),
                empty,
                snapshot=snapshot,
                performance_url="https://stock.example/strategies/long-short-test/portfolio",
            ),
            "",
        )

    def test_performance_link_rejects_markdown_injection_and_credentials(self):
        for malicious in (
            "https://token@stock.example/portfolio",
            "https://stock.example/portfolio\nINTERNAL_SECRET",
            "https://stock.example/portfolio\x7fsecret",
            "javascript:alert(1)",
        ):
            with self.subTest(malicious=malicious):
                self.assertEqual(append_performance_link("report", malicious), "report")

    def test_performance_link_accepts_hosts_ports_and_parenthesized_paths_safely(self):
        cases = {
            "http://127.0.0.1:8765/strategies/(alpha)/portfolio": (
                "[查看策略表现](<http://127.0.0.1:8765/strategies/%28alpha%29/portfolio>)"
            ),
            "http://[2001:db8::1]:8765/strategies/(alpha)": (
                "[查看策略表现](<http://[2001:db8::1]:8765/strategies/%28alpha%29>)"
            ),
            "https://stock.example/%7ealpha%2f(beta)?next=%2fportfolio": (
                "[查看策略表现](<https://stock.example/~alpha%2F%28beta%29?next=%2Fportfolio>)"
            ),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertIn(expected, append_performance_link("report", url))


if __name__ == "__main__":
    unittest.main()
