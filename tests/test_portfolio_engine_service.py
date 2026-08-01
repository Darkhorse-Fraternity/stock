from __future__ import annotations

import math
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from stock_recommender.context import (
    CandidateCollection,
    collect_recommendation_plan,
    recommendation_context_payload,
)
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
    MarketSnapshot,
    PlanRequest,
    PortfolioSnapshot,
    ProcessRequest,
    PositionSide,
    SignalCandidate,
)
from stock_recommender.portfolio_engine.ledger import JsonLedgerStore
from stock_recommender.portfolio_engine.risk import COVER_ONLY
from stock_recommender.recommendation import RecommendationPlan
from stock_recommender.reports import render_report
from stock_recommender.tracking import save_daily_selection

from stock_recommender.portfolio_engine import service
from stock_recommender.portfolio_engine import contracts
import stock_recommender.portfolio_engine as public_portfolio_engine


NOW = datetime(2026, 8, 3, 14, 30, tzinfo=timezone.utc)


def strategy(mode: str = "LONG_SHORT") -> dict:
    exposure = default_exposure_policy()
    exposure["mode"] = mode
    return {
        "version": 6,
        "id": "strategy-us",
        "revision": 3,
        "market": "us",
        "signal": {"model": "long-test-v1"},
        "lifecycle": {"stage": "paper"},
        "exposure_policy": exposure,
        "margin_policy": default_margin_policy(),
        "short_policy": {**default_short_policy(), "signal_model": "short-test-v1"},
        "portfolio": {"initial_cash": 100_000.0},
    }


def account(snapshot_id: str = "portfolio-0") -> AccountSnapshot:
    return AccountSnapshot(
        id="account-us",
        strategy_id="strategy-us",
        strategy_revision=3,
        occurred_at=NOW,
        available_cash=100_000.0,
        snapshot_id=snapshot_id,
    )


def market(snapshot_id: str = "market-1", *, occurred_at: datetime = NOW) -> MarketSnapshot:
    return MarketSnapshot(
        id=snapshot_id,
        occurred_at=occurred_at,
        quotes={
            "L": {"price": 100.0, "bar_open": 100.0, "bar_high": 101.0, "bar_low": 99.0, "bar_volume": 100_000},
            "S": {"price": 50.0, "bar_open": 50.0, "bar_high": 51.0, "bar_low": 49.0, "bar_volume": 100_000},
        },
    )


class StaticSignalModel:
    def __init__(self, model_id: str, side: PositionSide, symbol: str):
        self.model_id = model_id
        self.side = side
        self.symbol = symbol
        self.row_objects: list[object] = []

    def evaluate(self, rows, event_calendar):
        self.row_objects.append(rows)
        self.last_calendar = event_calendar
        return (
            SignalCandidate(
                symbol=self.symbol,
                side=self.side,
                score=0.9,
                requested_weight_pct=10.0 if self.side is PositionSide.LONG else 5.0,
                model_id=self.model_id,
                thesis_id=f"{self.model_id}:{self.symbol}:2026-08-01",
            ),
        )


class PoisonLedger:
    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"pure evaluate touched ledger method {name}")

        return fail


class CountingLedger:
    def __init__(self, path: Path):
        self.store = JsonLedgerStore(path)
        self.commit_calls = 0

    def create_account(self, value):
        return self.store.create_account(value)

    def load(self, strategy_id):
        return self.store.load(strategy_id)

    def load_view(self, strategy_id):
        return self.store.load_view(strategy_id)

    def commit(self, batch):
        self.commit_calls += 1
        return self.store.commit(batch)


def recommendation_plan(decision=None) -> RecommendationPlan:
    row = {
        "symbol": "L",
        "name": "Long",
        "price": 100.0,
        "score": 90.0,
        "risk_level": "MEDIUM",
        "reasons": ["test signal"],
        "rating": "关注",
        "rating_emoji": "🟡",
        "percent": 1.0,
        "change": 1.0,
        "turnover_rate": 1.0,
        "turnover": 1_000_000.0,
    }
    return RecommendationPlan(
        generated_at=NOW,
        universe_type="watchlist",
        watchlist_size=1,
        sector_filters=(),
        board_code="NASDAQ100",
        board_name="Nasdaq 100",
        sources=("test",),
        fetch_error=None,
        analyzed_count=1,
        data_quality={"status": "READY"},
        market_regime={"state": "RISK_ON"},
        signal_contract={"model": "long-test-v1"},
        candidates=(row,),
        selected_candidates=(row,),
        market="us",
        portfolio_decision=decision,
    )


class PortfolioEngineServiceTests(unittest.TestCase):
    def test_public_service_exposes_portfolio_engine(self):
        self.assertTrue(
            hasattr(service, "PortfolioEngine"),
            "Task 10 requires the PortfolioEngine transaction orchestrator",
        )

    def test_service_request_and_snapshot_contracts_are_public(self):
        self.assertTrue(hasattr(contracts, "PlanRequest"))
        self.assertTrue(hasattr(contracts, "ProcessRequest"))
        self.assertTrue(hasattr(contracts, "PortfolioSnapshot"))
        self.assertTrue(hasattr(contracts, "PortfolioLedgerView"))
        self.assertEqual(
            set(public_portfolio_engine.__all__),
            {"PortfolioEngine", "PlanRequest", "ProcessRequest", "PortfolioSnapshot"},
        )

    def test_recommendation_plan_accepts_only_exact_decision_batch(self):
        decision = service.PortfolioEngine(
            signal_registry={
                "long-test-v1": StaticSignalModel(
                    "long-test-v1", PositionSide.LONG, "L"
                )
            }
        ).evaluate(
            PlanRequest(
                run_key="plan:recommendation",
                strategy=strategy("LONG_ONLY"),
                account=account(),
                analyzed_rows=({"symbol": "L"},),
                market=market("recommendation-market"),
                borrow=BorrowSnapshot.unavailable(),
                event_calendar={},
            )
        )
        self.assertIs(recommendation_plan(decision).portfolio_decision, decision)
        with self.assertRaises(TypeError):
            recommendation_plan({"run_key": decision.run_key})

    def test_tracking_commits_the_exact_recommendation_decision(self):
        decision = service.PortfolioEngine(
            signal_registry={
                "long-test-v1": StaticSignalModel(
                    "long-test-v1", PositionSide.LONG, "L"
                )
            }
        ).evaluate(
            PlanRequest(
                run_key="plan:tracking-exact",
                strategy=strategy("LONG_ONLY"),
                account=account(),
                analyzed_rows=({"symbol": "L"},),
                market=market("tracking-market"),
                borrow=BorrowSnapshot.unavailable(),
                event_calendar={},
            )
        )

        class CommitSpy:
            def __init__(self):
                self.batches = []

            def commit(self, batch):
                self.batches.append(batch)
                return account("committed")

        spy = CommitSpy()
        with TemporaryDirectory() as temporary:
            save_daily_selection(
                Path(temporary) / "daily.json",
                recommendation_plan(decision),
                strategy=strategy("LONG_ONLY"),
                portfolio_engine=spy,
            )
        self.assertEqual(spy.batches, [decision])
        self.assertIs(spy.batches[0], decision)

    def test_tracking_identity_or_commit_failure_writes_no_daily_state(self):
        decision = service.PortfolioEngine(
            signal_registry={
                "long-test-v1": StaticSignalModel(
                    "long-test-v1", PositionSide.LONG, "L"
                )
            }
        ).evaluate(
            PlanRequest(
                run_key="plan:tracking-zero-write",
                strategy=strategy("LONG_ONLY"),
                account=account(),
                analyzed_rows=({"symbol": "L"},),
                market=market("tracking-zero-write-market"),
                borrow=BorrowSnapshot.unavailable(),
                event_calendar={},
            )
        )

        class CommitSpy:
            def __init__(self, error: Exception | None = None):
                self.error = error
                self.batches = []

            def commit(self, batch):
                self.batches.append(batch)
                if self.error is not None:
                    raise self.error
                return account("committed")

        with TemporaryDirectory() as temporary:
            daily = Path(temporary) / "daily.json"
            mismatch_spy = CommitSpy()
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                save_daily_selection(
                    daily,
                    recommendation_plan(replace(decision, strategy_id="other")),
                    strategy=strategy("LONG_ONLY"),
                    portfolio_engine=mismatch_spy,
                )
            self.assertFalse(daily.exists())
            self.assertEqual(mismatch_spy.batches, [])

            failure_spy = CommitSpy(RuntimeError("commit failed"))
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                save_daily_selection(
                    daily,
                    recommendation_plan(decision),
                    strategy=strategy("LONG_ONLY"),
                    portfolio_engine=failure_spy,
                )
            self.assertFalse(daily.exists())
            self.assertEqual(failure_spy.batches, [decision])

    def test_context_evaluates_one_immutable_analyzed_universe(self):
        long_model = StaticSignalModel("long-test-v1", PositionSide.LONG, "L")
        ledger = PoisonLedger()
        engine = service.PortfolioEngine(
            signal_registry={long_model.model_id: long_model},
            ledger_store=ledger,
        )
        analyzed = (
            {
                "symbol": "L",
                "name": "Long",
                "price": 100.0,
                "score": 0.9,
                "signal_features": {
                    "momentum20": 0.05,
                    "momentum60": 0.08,
                    "trend": 2,
                },
            },
        )
        collection = CandidateCollection(
            generated_at=NOW,
            analyses=analyzed,
            market_analyses=analyzed,
            fetch_error=None,
            data_quality={"status": "READY", "reason": "test"},
        )
        with patch(
            "stock_recommender.context.collect_analyzed_candidates",
            return_value=collection,
        ):
            plan = collect_recommendation_plan(
                strategy=strategy("LONG_ONLY"),
                portfolio_engine=engine,
                portfolio_account=account(),
                portfolio_market=market("context-market"),
                portfolio_borrow=BorrowSnapshot.unavailable(),
                portfolio_event_calendar={"L": None},
            )
        self.assertIsNotNone(plan.portfolio_decision)
        self.assertEqual(plan.portfolio_decision.run_key, "recommendation:strategy-us:context-market")
        self.assertEqual(len(long_model.row_objects), 1)
        self.assertEqual(ledger.calls, [])

    def test_prepare_plan_request_uses_injected_snapshot_ports_once(self):
        calls = []

        class QuotePort:
            def snapshot(self, symbols, occurred_at):
                calls.append(("quote", symbols, occurred_at))
                return market("prepared-market", occurred_at=occurred_at)

        class BorrowPort:
            def snapshot(self, symbols, occurred_at):
                calls.append(("borrow", symbols, occurred_at))
                return BorrowSnapshot.unavailable("prepared-borrow")

        class CalendarPort:
            def sessions_until_events(self, symbols, occurred_at):
                calls.append(("calendar", symbols, occurred_at))
                return {symbol: None for symbol in symbols}

        engine = service.PortfolioEngine(
            quote_provider=QuotePort(),
            borrow_provider=BorrowPort(),
            calendar_provider=CalendarPort(),
        )
        rows = [{"symbol": "L", "nested": [1]}, {"symbol": "L"}]
        request = engine.prepare_plan_request(
            run_key="prepared:1",
            strategy=strategy("LONG_ONLY"),
            account=account(),
            analyzed_rows=rows,
            occurred_at=NOW,
        )
        rows[0]["nested"][0] = 999
        self.assertEqual(request.analyzed_rows[0]["nested"], (1,))
        self.assertEqual(
            calls,
            [
                ("quote", ("L",), NOW),
                ("borrow", ("L",), NOW),
                ("calendar", ("L",), NOW),
            ],
        )

    def test_reports_render_signed_signal_facts_from_exact_decision(self):
        decision = service.PortfolioEngine(
            signal_registry={
                "long-test-v1": StaticSignalModel(
                    "long-test-v1", PositionSide.LONG, "L"
                ),
                "short-test-v1": StaticSignalModel(
                    "short-test-v1", PositionSide.SHORT, "S"
                ),
            }
        ).evaluate(
            PlanRequest(
                run_key="plan:signed-report",
                strategy=strategy(),
                account=account(),
                analyzed_rows=({"symbol": "L"}, {"symbol": "S"}),
                market=market("signed-report-market"),
                borrow=BorrowSnapshot.unavailable(),
                event_calendar={},
            )
        )
        plan = recommendation_plan(decision)
        payload = recommendation_context_payload(plan)
        self.assertEqual(
            [(item["symbol"], item["side"]) for item in payload["portfolio_signals"]],
            [("L", "LONG"), ("S", "SHORT")],
        )
        report = render_report(plan, strategy=strategy())
        self.assertIn("做多 L", report)
        self.assertIn("做空 S", report)

    def test_plan_request_is_strict_deeply_immutable_and_finite(self):
        rows = [{"symbol": "L", "score": 0.9, "nested": [1.0]}]
        request = PlanRequest(
            run_key="plan:1",
            strategy=strategy(),
            account=account(),
            analyzed_rows=rows,
            market=market(),
            borrow=BorrowSnapshot.unavailable(),
            event_calendar={"L": 3, "S": None},
        )
        rows[0]["nested"][0] = 999.0
        self.assertEqual(request.analyzed_rows[0]["nested"], (1.0,))
        with self.assertRaisesRegex(ValueError, "finite"):
            PlanRequest(
                run_key="plan:nan",
                strategy=strategy(),
                account=account(),
                analyzed_rows=({"symbol": "L", "score": math.nan},),
                market=market(),
                borrow=BorrowSnapshot.unavailable(),
                event_calendar={},
            )

    def test_evaluate_is_pure_deterministic_and_borrow_failure_is_local(self):
        long_model = StaticSignalModel("long-test-v1", PositionSide.LONG, "L")
        short_model = StaticSignalModel("short-test-v1", PositionSide.SHORT, "S")
        ledger = PoisonLedger()
        engine = service.PortfolioEngine(
            signal_registry={long_model.model_id: long_model, short_model.model_id: short_model},
            ledger_store=ledger,
        )
        request = PlanRequest(
            run_key="plan:local-borrow",
            strategy=strategy(),
            account=account(),
            analyzed_rows=({"symbol": "L", "score": 0.9}, {"symbol": "S", "score": 0.9}),
            market=market(),
            borrow=BorrowSnapshot.unavailable(),
            event_calendar={"L": None, "S": None},
        )

        first = engine.evaluate(request)
        second = engine.evaluate(request)

        self.assertEqual(first, second)
        self.assertEqual(ledger.calls, [])
        self.assertIs(long_model.row_objects[0], short_model.row_objects[0])
        self.assertEqual([intent.symbol for intent in first.intents], ["L"])
        self.assertIn("BORROW_DATA_MISSING", first.diagnostic_codes)
        self.assertEqual(
            [output.stage for output in first.stage_outputs],
            [
                "long_signal",
                "short_signal",
                "target_netting",
                "exposure_budget",
                "borrow_admission",
                "portfolio_risk",
                "rebalance_intent",
                "margin_admission",
            ],
        )

    def test_plan_and_process_commit_once_and_never_fill_same_snapshot(self):
        long_model = StaticSignalModel("long-test-v1", PositionSide.LONG, "L")
        with TemporaryDirectory() as temporary:
            ledger = CountingLedger(Path(temporary) / "portfolio-v2.json")
            ledger.create_account(account())
            engine = service.PortfolioEngine(
                signal_registry={long_model.model_id: long_model},
                ledger_store=ledger,
            )
            request = PlanRequest(
                run_key="plan:commit-once",
                strategy=strategy("LONG_ONLY"),
                account=ledger.load("strategy-us"),
                analyzed_rows=({"symbol": "L", "score": 0.9},),
                market=market("market-1"),
                borrow=BorrowSnapshot.unavailable(),
                event_calendar={"L": None},
            )
            expected = engine.evaluate(request)
            planned = engine.plan_and_commit(request)
            self.assertEqual(planned, expected)
            self.assertEqual(ledger.commit_calls, 1)
            self.assertEqual(len(ledger.load_view("strategy-us").open_intents), 1)

            same = engine.process_and_commit(
                ProcessRequest(
                    run_key="process:same",
                    strategy=strategy("LONG_ONLY"),
                    account=ledger.load("strategy-us"),
                    market=market("market-1"),
                    borrow=BorrowSnapshot.unavailable(),
                )
            )
            self.assertEqual(same.fills, ())
            self.assertEqual(ledger.commit_calls, 2)

            later = engine.process_and_commit(
                ProcessRequest(
                    run_key="process:later",
                    strategy=strategy("LONG_ONLY"),
                    account=ledger.load("strategy-us"),
                    market=market("market-2", occurred_at=NOW + timedelta(minutes=5)),
                    borrow=BorrowSnapshot.unavailable(),
                )
            )
            self.assertEqual(len(later.fills), 1)
            self.assertEqual(ledger.commit_calls, 3)
            self.assertEqual(ledger.load_view("strategy-us").open_intents, ())

    def test_process_requires_strictly_later_market_time_not_only_new_id(self):
        long_model = StaticSignalModel("long-test-v1", PositionSide.LONG, "L")
        with TemporaryDirectory() as temporary:
            ledger = CountingLedger(Path(temporary) / "portfolio-v2.json")
            ledger.create_account(account())
            engine = service.PortfolioEngine(
                signal_registry={long_model.model_id: long_model},
                ledger_store=ledger,
            )
            engine.plan_and_commit(
                PlanRequest(
                    run_key="plan:market-time",
                    strategy=strategy("LONG_ONLY"),
                    account=ledger.load("strategy-us"),
                    analyzed_rows=({"symbol": "L"},),
                    market=market("created-id", occurred_at=NOW),
                    borrow=BorrowSnapshot.unavailable(),
                    event_calendar={},
                )
            )
            for run_key, snapshot_id, occurred_at in (
                ("same-time", "different-id", NOW),
                ("earlier-time", "earlier-id", NOW - timedelta(minutes=1)),
            ):
                result = engine.process_and_commit(
                    ProcessRequest(
                        run_key=f"process:{run_key}",
                        strategy=strategy("LONG_ONLY"),
                        account=ledger.load("strategy-us"),
                        market=market(snapshot_id, occurred_at=occurred_at),
                        borrow=BorrowSnapshot.unavailable(),
                    )
                )
                self.assertEqual(result.fills, ())
            later = engine.process_and_commit(
                ProcessRequest(
                    run_key="process:later-time",
                    strategy=strategy("LONG_ONLY"),
                    account=ledger.load("strategy-us"),
                    market=market("later-id", occurred_at=NOW + timedelta(minutes=1)),
                    borrow=BorrowSnapshot.unavailable(),
                )
            )
            self.assertEqual(len(later.fills), 1)

    def test_process_stale_account_fails_without_commit_and_performance_is_read_only(self):
        long_model = StaticSignalModel("long-test-v1", PositionSide.LONG, "L")
        with TemporaryDirectory() as temporary:
            ledger = CountingLedger(Path(temporary) / "portfolio-v2.json")
            ledger.create_account(account())
            engine = service.PortfolioEngine(
                signal_registry={long_model.model_id: long_model},
                ledger_store=ledger,
            )
            with self.assertRaisesRegex(ValueError, "stale"):
                engine.process_and_commit(
                    ProcessRequest(
                        run_key="process:stale",
                        strategy=strategy("LONG_ONLY"),
                        account=account("forged-stale"),
                        market=market("market-2"),
                        borrow=BorrowSnapshot.unavailable(),
                    )
                )
            self.assertEqual(ledger.commit_calls, 0)

            before = ledger.load("strategy-us")
            snapshot = engine.performance("strategy-us", market("market-read"))
            self.assertIsInstance(snapshot, PortfolioSnapshot)
            self.assertEqual(snapshot.account, before)
            self.assertEqual(ledger.load("strategy-us"), before)
            self.assertEqual(ledger.commit_calls, 0)

    def test_partial_fill_progresses_across_distinct_market_snapshots(self):
        long_model = StaticSignalModel("long-test-v1", PositionSide.LONG, "L")
        with TemporaryDirectory() as temporary:
            ledger = CountingLedger(Path(temporary) / "portfolio-v2.json")
            ledger.create_account(account())
            engine = service.PortfolioEngine(
                signal_registry={long_model.model_id: long_model},
                ledger_store=ledger,
            )
            engine.plan_and_commit(
                PlanRequest(
                    run_key="plan:partial",
                    strategy=strategy("LONG_ONLY"),
                    account=ledger.load("strategy-us"),
                    analyzed_rows=({"symbol": "L"},),
                    market=market("partial-plan"),
                    borrow=BorrowSnapshot.unavailable(),
                    event_calendar={},
                )
            )

            def thin_market(snapshot_id: str, minute: int) -> MarketSnapshot:
                return MarketSnapshot(
                    id=snapshot_id,
                    occurred_at=NOW + timedelta(minutes=minute),
                    quotes={
                        "L": {
                            "price": 100.0,
                            "bar_open": 100.0,
                            "bar_high": 101.0,
                            "bar_low": 99.0,
                            "bar_volume": 100,
                        }
                    },
                )

            first = engine.process_and_commit(
                ProcessRequest(
                    run_key="process:partial:1",
                    strategy=strategy("LONG_ONLY"),
                    account=ledger.load("strategy-us"),
                    market=thin_market("partial-1", 5),
                    borrow=BorrowSnapshot.unavailable(),
                )
            )
            second = engine.process_and_commit(
                ProcessRequest(
                    run_key="process:partial:2",
                    strategy=strategy("LONG_ONLY"),
                    account=ledger.load("strategy-us"),
                    market=thin_market("partial-2", 10),
                    borrow=BorrowSnapshot.unavailable(),
                )
            )
            self.assertEqual(first.fills[0].quantity, 5)
            self.assertEqual(second.fills[0].quantity, 5)
            progress = ledger.load_view("strategy-us").execution_progress
            self.assertEqual(progress[0].filled_quantity, 10)

    def test_process_rechecks_stale_intent_against_current_hard_caps(self):
        long_model = StaticSignalModel("long-test-v1", PositionSide.LONG, "L")
        with TemporaryDirectory() as temporary:
            ledger = CountingLedger(Path(temporary) / "portfolio-v2.json")
            ledger.create_account(account())
            engine = service.PortfolioEngine(
                signal_registry={long_model.model_id: long_model},
                ledger_store=ledger,
            )
            engine.plan_and_commit(
                PlanRequest(
                    run_key="plan:gap",
                    strategy=strategy("LONG_ONLY"),
                    account=ledger.load("strategy-us"),
                    analyzed_rows=({"symbol": "L"},),
                    market=market("gap-plan"),
                    borrow=BorrowSnapshot.unavailable(),
                    event_calendar={},
                )
            )
            gap_market = MarketSnapshot(
                id="gap-current",
                occurred_at=NOW + timedelta(minutes=5),
                quotes={
                    "L": {
                        "price": 2_000.0,
                        "bar_open": 2_000.0,
                        "bar_high": 2_000.0,
                        "bar_low": 2_000.0,
                        "bar_volume": 1_000_000,
                    }
                },
            )
            processed = engine.process_and_commit(
                ProcessRequest(
                    run_key="process:gap",
                    strategy=strategy("LONG_ONLY"),
                    account=ledger.load("strategy-us"),
                    market=gap_market,
                    borrow=BorrowSnapshot.unavailable(),
                )
            )
            self.assertEqual(processed.fills, ())
            self.assertIn("GROSS_EXPOSURE_CAP", processed.diagnostic_codes)
            self.assertEqual(
                [item.stage for item in processed.stage_outputs[:2]],
                ["pre_execution_admission", "execution_simulation"],
            )
            self.assertEqual(ledger.load("strategy-us").positions, ())

    def test_process_rechecks_current_borrow_and_blocks_only_new_short_risk(self):
        long_model = StaticSignalModel("long-test-v1", PositionSide.LONG, "L")
        short_model = StaticSignalModel("short-test-v1", PositionSide.SHORT, "S")
        planned_borrow = BorrowSnapshot(
            id="borrow-planned",
            status=AVAILABLE,
            securities={
                "S": BorrowSecurity(
                    symbol="S",
                    shortable=True,
                    easy_to_borrow=True,
                    borrow_apr_pct=2.0,
                )
            },
        )
        with TemporaryDirectory() as temporary:
            ledger = CountingLedger(Path(temporary) / "portfolio-v2.json")
            ledger.create_account(account())
            engine = service.PortfolioEngine(
                signal_registry={
                    long_model.model_id: long_model,
                    short_model.model_id: short_model,
                },
                ledger_store=ledger,
            )
            planned = engine.plan_and_commit(
                PlanRequest(
                    run_key="plan:borrow-recheck",
                    strategy=strategy("LONG_SHORT"),
                    account=ledger.load("strategy-us"),
                    analyzed_rows=({"symbol": "L"}, {"symbol": "S"}),
                    market=market("borrow-recheck-plan"),
                    borrow=planned_borrow,
                    event_calendar={},
                )
            )
            self.assertEqual([item.symbol for item in planned.intents], ["L", "S"])

            processed = engine.process_and_commit(
                ProcessRequest(
                    run_key="process:borrow-recheck",
                    strategy=strategy("LONG_SHORT"),
                    account=ledger.load("strategy-us"),
                    market=market(
                        "borrow-recheck-current",
                        occurred_at=NOW + timedelta(minutes=1),
                    ),
                    borrow=BorrowSnapshot.unavailable("borrow-current-missing"),
                )
            )
            self.assertEqual([item.symbol for item in processed.fills], ["L"])
            self.assertIn("BORROW_DATA_MISSING", processed.diagnostic_codes)
            positions = ledger.load("strategy-us").positions
            self.assertEqual(
                [(item.symbol, item.side) for item in positions],
                [("L", PositionSide.LONG)],
            )
            self.assertEqual(ledger.commit_calls, 2)

    def test_partial_short_cannot_resume_when_borrow_becomes_unavailable(self):
        long_model = StaticSignalModel("long-test-v1", PositionSide.LONG, "L")
        short_model = StaticSignalModel("short-test-v1", PositionSide.SHORT, "S")
        available_borrow = BorrowSnapshot(
            id="borrow-partial-available",
            status=AVAILABLE,
            securities={
                "S": BorrowSecurity(
                    symbol="S",
                    shortable=True,
                    easy_to_borrow=True,
                    borrow_apr_pct=2.0,
                    available_quantity=100,
                )
            },
        )
        with TemporaryDirectory() as temporary:
            ledger = CountingLedger(Path(temporary) / "portfolio-v2.json")
            ledger.create_account(account())
            engine = service.PortfolioEngine(
                signal_registry={
                    long_model.model_id: long_model,
                    short_model.model_id: short_model,
                },
                ledger_store=ledger,
            )
            planned = engine.plan_and_commit(
                PlanRequest(
                    run_key="plan:partial-short-borrow",
                    strategy=strategy("LONG_SHORT"),
                    account=ledger.load("strategy-us"),
                    analyzed_rows=({"symbol": "L"}, {"symbol": "S"}),
                    market=market("partial-short-plan"),
                    borrow=available_borrow,
                    event_calendar={},
                )
            )
            short_intent = next(
                item for item in planned.intents if item.symbol == "S"
            )
            partial_market = MarketSnapshot(
                id="partial-short-first",
                occurred_at=NOW + timedelta(minutes=1),
                quotes={
                    "L": {
                        "price": 100.0,
                        "bar_open": 100.0,
                        "bar_high": 101.0,
                        "bar_low": 99.0,
                        "bar_volume": 100_000,
                    },
                    "S": {
                        "price": 50.0,
                        "bar_open": 50.0,
                        "bar_high": 51.0,
                        "bar_low": 49.0,
                        "bar_volume": 100,
                    },
                },
            )
            first = engine.process_and_commit(
                ProcessRequest(
                    run_key="process:partial-short:first",
                    strategy=strategy("LONG_SHORT"),
                    account=ledger.load("strategy-us"),
                    market=partial_market,
                    borrow=available_borrow,
                )
            )
            short_fill = next(item for item in first.fills if item.symbol == "S")
            self.assertGreater(short_fill.quantity, 0)
            self.assertLess(short_fill.quantity, short_intent.quantity)

            second = engine.process_and_commit(
                ProcessRequest(
                    run_key="process:partial-short:unavailable",
                    strategy=strategy("LONG_SHORT"),
                    account=ledger.load("strategy-us"),
                    market=market(
                        "partial-short-unavailable",
                        occurred_at=NOW + timedelta(minutes=2),
                    ),
                    borrow=BorrowSnapshot.unavailable("borrow-runtime-failed"),
                )
            )
            self.assertEqual(second.fills, ())
            self.assertIn("BORROW_DATA_MISSING", second.diagnostic_codes)
            short_position = next(
                item
                for item in ledger.load("strategy-us").positions
                if item.symbol == "S"
            )
            self.assertEqual(short_position.quantity, short_fill.quantity)
            self.assertEqual(short_position.position_mode, COVER_ONLY)
            self.assertEqual(ledger.commit_calls, 3)

    def test_post_execution_hard_cap_failure_writes_nothing_to_ledger(self):
        long_model = StaticSignalModel("long-test-v1", PositionSide.LONG, "L")
        with TemporaryDirectory() as temporary:
            ledger = CountingLedger(Path(temporary) / "portfolio-v2.json")
            ledger.create_account(account())
            engine = service.PortfolioEngine(
                signal_registry={long_model.model_id: long_model},
                ledger_store=ledger,
            )
            engine.plan_and_commit(
                PlanRequest(
                    run_key="plan:post-cap",
                    strategy=strategy("LONG_ONLY"),
                    account=ledger.load("strategy-us"),
                    analyzed_rows=({"symbol": "L"},),
                    market=market("post-cap-plan"),
                    borrow=BorrowSnapshot.unavailable(),
                    event_calendar={},
                )
            )
            before = ledger.load_view("strategy-us")
            commit_calls_before = ledger.commit_calls
            with patch.object(
                service,
                "hard_cap_breaches",
                return_value=("GROSS_EXPOSURE_CAP",),
            ):
                with self.assertRaisesRegex(RuntimeError, "post-execution hard-cap"):
                    engine.process_and_commit(
                        ProcessRequest(
                            run_key="process:post-cap",
                            strategy=strategy("LONG_ONLY"),
                            account=before.account,
                            market=market(
                                "post-cap-current",
                                occurred_at=NOW + timedelta(minutes=1),
                            ),
                            borrow=BorrowSnapshot.unavailable(),
                        )
                    )
            self.assertEqual(ledger.commit_calls, commit_calls_before)
            self.assertEqual(ledger.load_view("strategy-us"), before)

    def test_process_accrues_short_carry_once_per_market_day(self):
        long_model = StaticSignalModel("long-test-v1", PositionSide.LONG, "L")
        short_model = StaticSignalModel("short-test-v1", PositionSide.SHORT, "S")
        borrow = BorrowSnapshot(
            id="borrow-ready",
            status=AVAILABLE,
            securities={
                "S": BorrowSecurity(
                    symbol="S",
                    shortable=True,
                    easy_to_borrow=True,
                    borrow_apr_pct=12.0,
                )
            },
        )
        with TemporaryDirectory() as temporary:
            ledger = CountingLedger(Path(temporary) / "portfolio-v2.json")
            ledger.create_account(account())
            engine = service.PortfolioEngine(
                signal_registry={
                    long_model.model_id: long_model,
                    short_model.model_id: short_model,
                },
                ledger_store=ledger,
            )
            engine.plan_and_commit(
                PlanRequest(
                    run_key="plan:carry",
                    strategy=strategy(),
                    account=ledger.load("strategy-us"),
                    analyzed_rows=({"symbol": "L"}, {"symbol": "S"}),
                    market=market("carry-plan"),
                    borrow=borrow,
                    event_calendar={},
                )
            )
            engine.process_and_commit(
                ProcessRequest(
                    run_key="process:carry:fill",
                    strategy=strategy(),
                    account=ledger.load("strategy-us"),
                    market=market("carry-fill", occurred_at=NOW + timedelta(minutes=5)),
                    borrow=borrow,
                )
            )
            first = engine.process_and_commit(
                ProcessRequest(
                    run_key="process:carry:day2:first",
                    strategy=strategy(),
                    account=ledger.load("strategy-us"),
                    market=market("carry-day2-first", occurred_at=NOW + timedelta(days=1)),
                    borrow=borrow,
                )
            )
            cost_after_first = ledger.load("strategy-us").accrued_borrow_cost
            second = engine.process_and_commit(
                ProcessRequest(
                    run_key="process:carry:day2:second",
                    strategy=strategy(),
                    account=ledger.load("strategy-us"),
                    market=market(
                        "carry-day2-second",
                        occurred_at=NOW + timedelta(days=1, minutes=5),
                    ),
                    borrow=borrow,
                )
            )
            self.assertEqual(len(first.carry_accruals), 1)
            self.assertEqual(second.carry_accruals, ())
            self.assertGreater(cost_after_first, 0)
            self.assertEqual(
                ledger.load("strategy-us").accrued_borrow_cost,
                cost_after_first,
            )


if __name__ == "__main__":
    unittest.main()
