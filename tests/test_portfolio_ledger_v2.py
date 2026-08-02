from __future__ import annotations

import json
import multiprocessing
import threading
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from stock_recommender.pipeline import StageOutput
from stock_recommender.portfolio_engine import atomic_io, execution
from stock_recommender.portfolio_engine import ledger as ledger_module
from stock_recommender.portfolio_engine import contracts as portfolio_contracts

from stock_recommender.portfolio_engine.contracts import (
    AccrualLifecycle,
    AccountSnapshot,
    CarryAccrualRecord,
    CarryCostType,
    DecisionBatch,
    ExecutionFill,
    ExecutionProgressFill,
    MarketSnapshot,
    OrderExecutionProgress,
    OrderIntent,
    OrderSide,
    PortfolioEvent,
    PositionEffect,
    PositionRiskUpdate,
    PositionSide,
    PositionSnapshot,
    stable_execution_intent_id,
    stable_execution_progress_fill_id,
)
from stock_recommender.portfolio_engine.ledger import (
    InMemoryLedgerStore,
    JsonLedgerStore,
    LedgerError,
    LedgerSchemaError,
    StalePortfolioSnapshotError,
    UnknownPortfolioEventError,
    encode_account_snapshot,
)


NOW = datetime(2026, 7, 31, 14, 30, tzinfo=timezone.utc)
INTENT_CREATED_AT = datetime(2026, 7, 31, 14, 29, tzinfo=timezone.utc)


_OrderIntent = OrderIntent


def OrderIntent(**kwargs):
    kwargs.setdefault("created_market_at", INTENT_CREATED_AT)
    return _OrderIntent(**kwargs)


def account(*, strategy_id: str = "strategy", cash: float = 1_000.0) -> AccountSnapshot:
    return AccountSnapshot(
        id=f"account-{strategy_id}",
        strategy_id=strategy_id,
        strategy_revision=2,
        occurred_at=NOW,
        available_cash=cash,
    )


def batch(
    *,
    strategy_id: str = "strategy",
    run_key: str = "run-1",
    snapshot_id: str = "snapshot-0",
    events: tuple[PortfolioEvent, ...] = (),
) -> DecisionBatch:
    return DecisionBatch(
        run_key=run_key,
        strategy_id=strategy_id,
        strategy_revision=2,
        portfolio_snapshot_id=snapshot_id,
        market_snapshot_id=f"market-{run_key}",
        events=events,
    )


def cash_event(event_id: str, amount: float) -> PortfolioEvent:
    return PortfolioEvent(
        id=event_id,
        type="CASH_ADJUSTED",
        occurred_at=NOW,
        data={"amount": amount},
    )


def commit_from_process(path: str, run_key: str, result_queue: object) -> None:
    try:
        JsonLedgerStore(path).commit(
            batch(
                run_key=run_key,
                events=(cash_event(f"cash-{run_key}", -10.0),),
            )
        )
    except Exception as exc:
        result_queue.put(type(exc).__name__)
    else:
        result_queue.put("OK")


def create_account_from_process(path: str, result_queue: object) -> None:
    bootstrap = AccountSnapshot(
        id="account-bootstrap",
        strategy_id="bootstrap",
        strategy_revision=1,
        occurred_at=NOW,
        available_cash=10_000.0,
        snapshot_id="bootstrap-snapshot-0",
    )
    try:
        JsonLedgerStore(path).create_account(bootstrap)
    except Exception as exc:
        result_queue.put(type(exc).__name__)
    else:
        result_queue.put("OK")


def transition_revision_from_process(
    path: str,
    expected_snapshot_id: str,
    result_queue: object,
) -> None:
    try:
        JsonLedgerStore(path).transition_revision(
            portfolio_contracts.RevisionTransition(
                id="multiprocess-r2-r3",
                strategy_id="multiprocess-revision",
                expected_snapshot_id=expected_snapshot_id,
                from_revision=2,
                to_revision=3,
                occurred_at=NOW + timedelta(minutes=1),
            )
        )
    except Exception as exc:
        result_queue.put(type(exc).__name__)
    else:
        result_queue.put("OK")


class PortfolioLedgerV2Tests(unittest.TestCase):
    def test_fill_replay_clears_financing_lifecycle_when_close_repays_loan(self):
        financed = AccountSnapshot(
            id="account-strategy",
            strategy_id="strategy",
            strategy_revision=2,
            occurred_at=NOW,
            available_cash=0.0,
            margin_loan=150.0,
            financing_lifecycle=AccrualLifecycle(
                "financing-close",
                date(2026, 7, 30),
            ),
            positions=(
                PositionSnapshot(
                    symbol="AAPL",
                    side=PositionSide.LONG,
                    quantity=2,
                    average_cost=100.0,
                    current_price=100.0,
                ),
            ),
        )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["accounts"]["strategy"].update(encode_account_snapshot(financed))
        payload["accounts"]["strategy"]["portfolio_snapshot_id"] = "snapshot-0"
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        intent = OrderIntent(
            id="close-financed-long",
            symbol="AAPL",
            position_side=PositionSide.LONG,
            order_side=OrderSide.SELL,
            position_effect=PositionEffect.CLOSE,
            quantity=2,
            reason="MARGIN_CALL",
            created_snapshot_id="market-0",
        )
        values = {
            "intent_id": intent.id,
            "symbol": intent.symbol,
            "position_side": intent.position_side,
            "order_side": intent.order_side,
            "snapshot_id": "market-1",
            "occurred_at": NOW,
            "quantity": 2,
            "price": 100.0,
            "fees": 0.0,
            "commission": 0.0,
            "status": "FILLED",
        }
        detail = ExecutionProgressFill(
            id=stable_execution_progress_fill_id(**values),
            **values,
        )
        progress = OrderExecutionProgress(
            intent_id=intent.id,
            symbol=intent.symbol,
            position_side=intent.position_side,
            order_side=intent.order_side,
            intent_quantity=intent.quantity,
            execution_policy_fingerprint="policy-close",
            fills=(detail,),
        )

        committed = JsonLedgerStore(self.path).commit(
            DecisionBatch(
                run_key="close-financing",
                strategy_id="strategy",
                strategy_revision=2,
                portfolio_snapshot_id="snapshot-0",
                market_snapshot_id="market-1",
                intents=(intent,),
                fills=(
                    ExecutionFill(
                        intent.id,
                        intent.symbol,
                        2,
                        100.0,
                        0.0,
                        "FILLED",
                    ),
                ),
                execution_progress=(progress,),
            )
        )

        self.assertEqual(committed.margin_loan, 0.0)
        self.assertIsNone(committed.financing_lifecycle)
        self.assertEqual(committed.available_cash, 50.0)
        self.assertEqual(committed.positions, ())

    def test_performance_view_replays_lifecycle_and_marks_unreplayable_runs(self):
        with TemporaryDirectory() as temporary:
            replayable_path = Path(temporary) / "replayable.json"
            replayable = JsonLedgerStore(replayable_path)
            replayable.create_account(replace(account(), snapshot_id="snapshot-0"))
            intent = OrderIntent(
                id=stable_execution_intent_id(
                    symbol="AAPL",
                    position_side=PositionSide.LONG,
                    order_side=OrderSide.BUY,
                    position_effect=PositionEffect.OPEN,
                    quantity=1,
                    reason="performance read model",
                    created_snapshot_id="snapshot-0",
                    created_market_at=INTENT_CREATED_AT,
                ),
                symbol="AAPL",
                position_side=PositionSide.LONG,
                order_side=OrderSide.BUY,
                position_effect=PositionEffect.OPEN,
                quantity=1,
                reason="performance read model",
                created_snapshot_id="snapshot-0",
            )
            replayable.commit(
                replace(
                    batch(snapshot_id="snapshot-0"),
                    request_fingerprint="performance-request",
                    intents=(intent,),
                )
            )

            view = replayable.load_performance_view("strategy")

            self.assertEqual(view.intents, (intent,))
            self.assertEqual(len(view.batches), 1)
            self.assertTrue(view.lifecycle_complete)
            self.assertIsNone(view.lifecycle_reason)

            incomplete_path = Path(temporary) / "incomplete.json"
            incomplete = JsonLedgerStore(incomplete_path)
            incomplete.create_account(replace(account(), snapshot_id="snapshot-0"))
            incomplete.commit(batch(snapshot_id="snapshot-0"))

            incomplete_view = incomplete.load_performance_view("strategy")

            self.assertFalse(incomplete_view.lifecycle_complete)
            self.assertIn("no replayable DecisionBatch", incomplete_view.lifecycle_reason)

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "ledger.json"
        self.path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "accounts": {
                        "strategy": {
                            **encode_account_snapshot(account()),
                            "portfolio_snapshot_id": "snapshot-0",
                            "reserved_cash": 0.0,
                            "open_intents": [],
                            "fills": [],
                            "execution_progress": [],
                            "risk_facts": [],
                            "revision_transitions": [],
                            "events": [],
                            "committed_batches": [],
                            "run_results": [],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_execution_and_ledger_share_exact_decimal_fee_transition(self):
        source = account(cash=999_999.8364)

        execution_account = execution._charge_fee(source, 0.1636)
        replayed_account = ledger_module._charge_fee(source, 0.1636)

        self.assertEqual(execution_account, replayed_account)
        self.assertEqual(execution_account.available_cash, 999_999.6728)

    def test_in_memory_store_reuses_canonical_commit_and_validates_at_boundary(self):
        store = InMemoryLedgerStore(self.path.parent / "memory-ledger.lock")
        opened = replace(
            account(strategy_id="memory"),
            snapshot_id="memory-snapshot-0",
        )
        store.create_account(opened)

        committed = store.commit(
            batch(
                strategy_id="memory",
                snapshot_id=opened.snapshot_id,
                events=(cash_event("memory-cash", -10.0),),
            )
        )

        self.assertIs(InMemoryLedgerStore.commit, JsonLedgerStore.commit)
        self.assertEqual(store.load("memory"), committed)
        self.assertEqual(committed.available_cash, 990.0)
        store.validate_integrity()

    def test_revision_transition_is_typed_atomic_idempotent_and_auditable(self):
        path = self.path.parent / "revision-transition.json"
        opened = AccountSnapshot(
            id="account-revisioned",
            strategy_id="revisioned",
            strategy_revision=3,
            occurred_at=NOW,
            available_cash=900.0,
            restricted_short_proceeds=100.0,
            margin_loan=50.0,
            positions=(
                PositionSnapshot(
                    symbol="S",
                    side=PositionSide.SHORT,
                    quantity=1,
                    average_cost=100.0,
                    current_price=100.0,
                    borrow_lifecycle=AccrualLifecycle(
                        id="borrow-s",
                        started_on=NOW.date(),
                    ),
                ),
            ),
            financing_lifecycle=AccrualLifecycle(
                id="financing-account-revisioned",
                started_on=NOW.date(),
            ),
            snapshot_id="revision-snapshot-r3",
        )
        store = JsonLedgerStore(path)
        store.create_account(opened)
        old_intent = OrderIntent(
            id="revision-old-increase",
            symbol="S",
            position_side=PositionSide.SHORT,
            order_side=OrderSide.SELL,
            position_effect=PositionEffect.INCREASE,
            quantity=1,
            reason="OLD_REVISION",
            created_snapshot_id="old-market",
            created_market_at=INTENT_CREATED_AT,
        )
        planned = store.commit(
            DecisionBatch(
                run_key="revision-r3-plan",
                strategy_id="revisioned",
                strategy_revision=3,
                portfolio_snapshot_id=opened.snapshot_id,
                market_snapshot_id="old-market",
                intents=(old_intent,),
            )
        )
        transition = portfolio_contracts.RevisionTransition(
            id="revisioned-r3-r4",
            strategy_id="revisioned",
            expected_snapshot_id=planned.snapshot_id,
            from_revision=3,
            to_revision=4,
            occurred_at=NOW + timedelta(minutes=1),
        )

        transitioned = store.transition_revision(transition)
        repeated = store.transition_revision(transition)

        self.assertEqual(repeated, transitioned)
        self.assertEqual(transitioned.strategy_revision, 4)
        self.assertNotEqual(transitioned.snapshot_id, planned.snapshot_id)
        self.assertEqual(transitioned.available_cash, planned.available_cash)
        self.assertEqual(
            transitioned.restricted_short_proceeds,
            planned.restricted_short_proceeds,
        )
        self.assertEqual(transitioned.positions, planned.positions)
        self.assertEqual(transitioned.carry_accruals, planned.carry_accruals)
        self.assertEqual(
            transitioned.financing_lifecycle,
            planned.financing_lifecycle,
        )
        view = store.load_view("revisioned")
        self.assertEqual(view.open_intents, ())
        self.assertEqual(view.execution_progress, ())
        self.assertEqual(view.recent_events[-1].type, "REVISION_TRANSITIONED")
        payload = json.loads(path.read_text(encoding="utf-8"))["accounts"][
            "revisioned"
        ]
        self.assertEqual(len(payload["revision_transitions"]), 1)
        self.assertEqual(
            payload["revision_transitions"][0]["cancelled_intent_ids"],
            [old_intent.id],
        )
        collision = replace(
            transition,
            expected_snapshot_id="different-source-snapshot",
        )
        with self.assertRaisesRegex(LedgerError, "collision|different"):
            store.transition_revision(collision)
        changed_time = replace(
            transition,
            occurred_at=transition.occurred_at + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(LedgerError, "collision|different"):
            store.transition_revision(changed_time)

    def test_revision_transition_rejects_downgrade_and_wrong_strategy(self):
        transaction_type = portfolio_contracts.RevisionTransition
        with self.assertRaisesRegex(ValueError, "increase|downgrade"):
            transaction_type(
                id="bad-downgrade",
                strategy_id="strategy",
                expected_snapshot_id="snapshot-0",
                from_revision=3,
                to_revision=2,
                occurred_at=NOW,
            )
        store = JsonLedgerStore(self.path)
        with self.assertRaisesRegex((KeyError, LedgerError), "other"):
            store.transition_revision(
                transaction_type(
                    id="wrong-strategy",
                    strategy_id="other",
                    expected_snapshot_id="snapshot-0",
                    from_revision=2,
                    to_revision=3,
                    occurred_at=NOW,
                )
            )

    def test_revision_transition_is_multiprocess_safe_and_exactly_once(self):
        path = self.path.parent / "multiprocess-revision.json"
        opened = JsonLedgerStore(path).create_account(
            AccountSnapshot(
                id="account-multiprocess-revision",
                strategy_id="multiprocess-revision",
                strategy_revision=2,
                occurred_at=NOW,
                available_cash=10_000.0,
                snapshot_id="multiprocess-revision-snapshot",
            )
        )
        context = multiprocessing.get_context("spawn")
        results = context.Queue()
        processes = [
            context.Process(
                target=transition_revision_from_process,
                args=(str(path), opened.snapshot_id, results),
            )
            for _ in range(2)
        ]

        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)

        self.assertEqual(
            sorted(results.get(timeout=2) for _ in processes),
            ["OK", "OK"],
        )
        persisted = json.loads(path.read_text(encoding="utf-8"))["accounts"][
            "multiprocess-revision"
        ]
        self.assertEqual(persisted["strategy_revision"], 3)
        self.assertEqual(len(persisted["revision_transitions"]), 1)
        self.assertEqual(
            [
                event["type"]
                for event in persisted["events"]
                if event["type"] == "REVISION_TRANSITIONED"
            ],
            ["REVISION_TRANSITIONED"],
        )

    def test_revision_transition_tampering_fails_closed(self):
        path = self.path.parent / "tamper-revision.json"
        store = JsonLedgerStore(path)
        opened = store.create_account(
            AccountSnapshot(
                id="account-tamper-revision",
                strategy_id="tamper-revision",
                strategy_revision=2,
                occurred_at=NOW,
                available_cash=10_000.0,
                snapshot_id="tamper-revision-snapshot",
            )
        )
        store.transition_revision(
            portfolio_contracts.RevisionTransition(
                id="tamper-r2-r3",
                strategy_id="tamper-revision",
                expected_snapshot_id=opened.snapshot_id,
                from_revision=2,
                to_revision=3,
                occurred_at=NOW + timedelta(minutes=1),
            )
        )
        valid = json.loads(path.read_text(encoding="utf-8"))
        mutations = (
            lambda payload: payload["accounts"]["tamper-revision"][
                "revision_transitions"
            ][0].__setitem__("result_snapshot_id", "forged-snapshot"),
            lambda payload: payload["accounts"]["tamper-revision"]["events"][-1][
                "data"
            ].__setitem__("to_revision", 4),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                forged = json.loads(json.dumps(valid))
                mutate(forged)
                path.write_text(json.dumps(forged), encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaises(LedgerSchemaError):
                    store.load("tamper-revision")
                self.assertEqual(path.read_bytes(), before)

    def test_committed_request_result_is_lossless_and_tamper_evident(self):
        store = JsonLedgerStore(self.path)
        result = DecisionBatch(
            run_key="request-result-run",
            strategy_id="strategy",
            strategy_revision=2,
            portfolio_snapshot_id="snapshot-0",
            market_snapshot_id="request-result-market",
            request_fingerprint="request-fingerprint-1",
            diagnostics=({"code": "TEST", "nested": {"values": (1, 2)}},),
            stage_outputs=(
                StageOutput(
                    stage="audit",
                    component_version="1.0.0",
                    facts=({"kind": "typed", "items": ()},),
                ),
            ),
        )
        store.commit(result)

        self.assertEqual(
            store.load_committed_batch(
                "strategy",
                result.run_key,
                result.request_fingerprint,
            ),
            result,
        )
        with self.assertRaisesRegex(LedgerError, "different request"):
            store.load_committed_batch(
                "strategy",
                result.run_key,
                "different-request-fingerprint",
            )

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["accounts"]["strategy"]["run_results"][0][
            "request_fingerprint"
        ] = "forged-request-fingerprint"
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(LedgerSchemaError):
            store.load("strategy")
        self.assertEqual(self.path.read_bytes(), before)

    def _persist_two_risk_updates(self) -> tuple[Path, dict[str, object]]:
        path = self.path.parent / "risk-history.json"
        opened = AccountSnapshot(
            id="account-risk-history",
            strategy_id="risk-history",
            strategy_revision=2,
            occurred_at=NOW,
            available_cash=10_000.0,
            positions=(
                PositionSnapshot(
                    symbol="AAPL",
                    side=PositionSide.LONG,
                    quantity=1,
                    average_cost=100.0,
                    current_price=100.0,
                    peak_price=100.0,
                ),
            ),
            snapshot_id="risk-snapshot-0",
        )
        store = JsonLedgerStore(path)
        store.create_account(opened)
        update = PositionRiskUpdate(
            symbol="AAPL",
            side=PositionSide.LONG,
            peak_price=110.0,
            trough_price=None,
            trailing_active=True,
            position_mode="NORMAL",
        )
        first = store.commit(
            DecisionBatch(
                run_key="risk-run-1",
                strategy_id="risk-history",
                strategy_revision=2,
                portfolio_snapshot_id="risk-snapshot-0",
                market_snapshot_id="risk-market-1",
                position_risk_updates=(update,),
            )
        )
        store.commit(
            DecisionBatch(
                run_key="risk-run-2",
                strategy_id="risk-history",
                strategy_revision=2,
                portfolio_snapshot_id=first.snapshot_id,
                market_snapshot_id="risk-market-2",
                position_risk_updates=(update,),
            )
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        return path, payload

    def _assert_corrupt_risk_payload_is_read_only(
        self,
        path: Path,
        payload: dict[str, object],
    ) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaisesRegex(LedgerSchemaError, "risk|RISK"):
            JsonLedgerStore(path).load("risk-history")
        self.assertEqual(path.read_bytes(), before)

    def test_decision_batch_commits_once_and_returns_account_snapshot(self):
        store = JsonLedgerStore(self.path)
        first = store.commit(batch(events=(cash_event("cash-1", -10.0),)))
        repeated = store.commit(batch(events=(cash_event("cash-1", -10.0),)))

        self.assertIs(type(first), AccountSnapshot)
        self.assertEqual(first.available_cash, 990.0)
        self.assertEqual(repeated, first)
        self.assertEqual(store.load("strategy"), first)
        self.assertEqual(store.list_accounts(), (first,))

    def test_create_account_is_atomic_idempotent_and_records_opening_event(self):
        path = self.path.parent / "bootstrap.json"
        bootstrap = AccountSnapshot(
            id="account-bootstrap",
            strategy_id="bootstrap",
            strategy_revision=1,
            occurred_at=NOW,
            available_cash=10_000.0,
            snapshot_id="bootstrap-snapshot-0",
        )
        store = JsonLedgerStore(path)

        created = store.create_account(bootstrap)
        first_bytes = path.read_bytes()
        repeated = store.create_account(bootstrap)

        self.assertEqual(created, bootstrap)
        self.assertEqual(repeated, bootstrap)
        self.assertEqual(path.read_bytes(), first_bytes)
        persisted = json.loads(path.read_text(encoding="utf-8"))["accounts"][
            "bootstrap"
        ]
        self.assertEqual(persisted["risk_facts"], [])
        self.assertEqual(len(persisted["events"]), 1)
        opened = persisted["events"][0]
        self.assertEqual(opened["type"], "ACCOUNT_OPENED")
        self.assertEqual(
            opened["data"],
            {
                "account_id": bootstrap.id,
                "strategy_id": bootstrap.strategy_id,
                "strategy_revision": bootstrap.strategy_revision,
                "portfolio_snapshot_id": bootstrap.snapshot_id,
                "available_cash": bootstrap.available_cash,
            },
        )

        with self.assertRaisesRegex(LedgerError, "collision|different"):
            store.create_account(replace(bootstrap, available_cash=9_999.0))
        self.assertEqual(path.read_bytes(), first_bytes)

    def test_create_account_requires_explicit_snapshot_without_creating_target(self):
        path = self.path.parent / "missing-snapshot.json"
        bootstrap = AccountSnapshot(
            id="account-bootstrap",
            strategy_id="bootstrap",
            strategy_revision=1,
            occurred_at=NOW,
            available_cash=10_000.0,
        )

        with self.assertRaisesRegex(LedgerError, "snapshot"):
            JsonLedgerStore(path).create_account(bootstrap)

        self.assertFalse(path.exists())

    def test_concurrent_processes_create_one_account_without_truncation(self):
        path = self.path.parent / "concurrent-bootstrap.json"
        context = multiprocessing.get_context("spawn")
        results = context.Queue()
        processes = [
            context.Process(
                target=create_account_from_process,
                args=(str(path), results),
            )
            for _ in range(2)
        ]

        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)

        self.assertEqual(sorted(results.get(timeout=2) for _ in processes), ["OK", "OK"])
        accounts = JsonLedgerStore(path).list_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].strategy_id, "bootstrap")

    def test_different_run_rejects_stale_snapshot(self):
        store = JsonLedgerStore(self.path)
        committed = store.commit(batch(events=(cash_event("cash-1", -10.0),)))

        with self.assertRaises(StalePortfolioSnapshotError):
            store.commit(
                batch(
                    run_key="run-2",
                    snapshot_id="snapshot-0",
                    events=(cash_event("cash-2", -10.0),),
                )
            )
        self.assertEqual(store.load("strategy"), committed)

    def test_unknown_event_fails_closed_and_preserves_original_bytes(self):
        before = self.path.read_bytes()
        unknown = PortfolioEvent(
            id="unknown-1",
            type="FUTURE_EVENT",
            occurred_at=NOW,
            data={},
        )

        with self.assertRaises(UnknownPortfolioEventError):
            JsonLedgerStore(self.path).commit(batch(events=(unknown,)))

        self.assertEqual(self.path.read_bytes(), before)

    def test_unpaired_state_event_fails_closed_and_preserves_original_bytes(self):
        before = self.path.read_bytes()
        spoofed = PortfolioEvent(
            id="spoofed-fill",
            type="ORDER_FILLED",
            occurred_at=NOW,
            data={},
        )

        with self.assertRaisesRegex(LedgerError, "ORDER_FILLED|canonical|event"):
            JsonLedgerStore(self.path).commit(batch(events=(spoofed,)))

        self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_events_inside_one_batch_are_rejected_without_writing(self):
        before = self.path.read_bytes()
        duplicated = cash_event("duplicate-cash", -10.0)

        with self.assertRaisesRegex(LedgerError, "duplicate event"):
            JsonLedgerStore(self.path).commit(
                batch(events=(duplicated, duplicated))
            )

        self.assertEqual(self.path.read_bytes(), before)

    def test_atomic_replace_failure_preserves_original_bytes(self):
        before = self.path.read_bytes()

        with patch.object(
            atomic_io,
            "replace_path",
            side_effect=OSError("injected replace failure"),
        ):
            with self.assertRaises(OSError):
                JsonLedgerStore(self.path).commit(
                    batch(events=(cash_event("cash-replace", -10.0),))
                )

        self.assertEqual(self.path.read_bytes(), before)

    def test_directory_fsync_failure_rolls_back_original_and_cleans_temps(self):
        before = self.path.read_bytes()
        real_fsync = atomic_io.os.fsync
        call_count = 0

        def fail_first_directory_fsync(file_descriptor: int) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("injected directory fsync failure")
            real_fsync(file_descriptor)

        with patch.object(
            atomic_io.os,
            "fsync",
            side_effect=fail_first_directory_fsync,
        ):
            with self.assertRaises(OSError):
                JsonLedgerStore(self.path).commit(
                    batch(events=(cash_event("cash-fsync", -10.0),))
                )

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(tuple(self.path.parent.glob(f".{self.path.name}.*")), ())

    def test_directory_fsync_failure_restores_nonexistent_target(self):
        missing_path = self.path.parent / "missing-ledger.json"
        real_fsync = atomic_io.os.fsync
        call_count = 0

        def fail_first_directory_fsync(file_descriptor: int) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("injected directory fsync failure")
            real_fsync(file_descriptor)

        with patch.object(
            atomic_io.os,
            "fsync",
            side_effect=fail_first_directory_fsync,
        ):
            with self.assertRaises(OSError):
                ledger_module._atomic_write(
                    missing_path,
                    {"version": 2, "accounts": {}},
                )

        self.assertFalse(missing_path.exists())
        self.assertEqual(tuple(missing_path.parent.glob(f".{missing_path.name}.*")), ())

    def test_persisted_duplicate_intents_and_unknown_events_are_rejected(self):
        intent = {
            "id": "duplicate",
            "symbol": "AAPL",
            "position_side": "LONG",
            "order_side": "BUY",
            "position_effect": "OPEN",
            "quantity": 1,
            "reason": "test",
            "created_snapshot_id": "market-0",
        }
        original = json.loads(self.path.read_text(encoding="utf-8"))
        corruptions = (
            {"open_intents": [intent, intent]},
            {
                "events": [
                    {
                        "id": "future",
                        "type": "FUTURE_EVENT",
                        "occurred_at": NOW.isoformat(),
                        "data": {},
                    }
                ]
            },
        )
        for corruption in corruptions:
            with self.subTest(corruption=tuple(corruption)):
                payload = json.loads(json.dumps(original))
                payload["accounts"]["strategy"].update(corruption)
                self.path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(LedgerSchemaError):
                    JsonLedgerStore(self.path).load("strategy")

    def test_schema_v2_requires_exact_root_account_and_position_fields(self):
        original = json.loads(self.path.read_text(encoding="utf-8"))
        required_account_fields = (
            "reserved_cash",
            "restricted_short_proceeds",
            "margin_loan",
            "accrued_financing_cost",
            "accrued_borrow_cost",
            "positions",
            "carry_accruals",
            "financing_lifecycle",
            "portfolio_snapshot_id",
            "open_intents",
            "fills",
            "execution_progress",
            "risk_facts",
            "revision_transitions",
            "events",
            "committed_batches",
            "run_results",
        )
        for field_name in required_account_fields:
            with self.subTest(missing=field_name):
                payload = json.loads(json.dumps(original))
                del payload["accounts"]["strategy"][field_name]
                self.path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(LedgerSchemaError):
                    JsonLedgerStore(self.path).load("strategy")

        for target in ("root", "account"):
            with self.subTest(extra=target):
                payload = json.loads(json.dumps(original))
                if target == "root":
                    payload["unexpected"] = True
                else:
                    payload["accounts"]["strategy"]["unexpected"] = True
                self.path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(LedgerSchemaError):
                    JsonLedgerStore(self.path).load("strategy")

        payload = json.loads(json.dumps(original))
        payload["accounts"]["strategy"]["positions"] = [
            {
                "symbol": "AAPL",
                "side": "LONG",
                "quantity": 1,
                "average_cost": 100.0,
                "current_price": None,
                "peak_price": None,
                "trough_price": None,
                "position_mode": "NORMAL",
                "sellable_quantity": None,
                "sellable_on": None,
                "borrow_lifecycle": None,
            }
        ]
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(LedgerSchemaError):
            JsonLedgerStore(self.path).load("strategy")

    def test_persisted_nonstandard_json_numbers_are_rejected(self):
        original = json.loads(self.path.read_text(encoding="utf-8"))
        for number in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(number=number):
                payload = json.loads(json.dumps(original))
                payload["accounts"]["strategy"]["events"] = [
                    {
                        "id": "pipeline-event",
                        "type": "PIPELINE_COMPLETED",
                        "occurred_at": NOW.isoformat(),
                        "data": {"score": number},
                    }
                ]
                self.path.write_text(json.dumps(payload), encoding="utf-8")
                before = self.path.read_bytes()
                with self.assertRaises(LedgerSchemaError):
                    JsonLedgerStore(self.path).load("strategy")
                self.assertEqual(self.path.read_bytes(), before)

    def test_batch_observable_data_must_be_recursively_json_safe_and_finite(self):
        invalid_batches = (
            DecisionBatch(
                run_key="event-nan",
                strategy_id="strategy",
                strategy_revision=2,
                portfolio_snapshot_id="snapshot-0",
                market_snapshot_id="market-1",
                events=(
                    PortfolioEvent(
                        id="pipeline-nan",
                        type="PIPELINE_COMPLETED",
                        occurred_at=NOW,
                        data={"nested": {"score": float("nan")}},
                    ),
                ),
            ),
            DecisionBatch(
                run_key="diagnostic-inf",
                strategy_id="strategy",
                strategy_revision=2,
                portfolio_snapshot_id="snapshot-0",
                market_snapshot_id="market-1",
                diagnostics=({"score": float("inf")},),
            ),
            DecisionBatch(
                run_key="stage-inf",
                strategy_id="strategy",
                strategy_revision=2,
                portfolio_snapshot_id="snapshot-0",
                market_snapshot_id="market-1",
                stage_outputs=(
                    StageOutput(
                        stage="audit",
                        component_version="1.0.0",
                        facts=({"kind": "audit", "score": float("-inf")},),
                    ),
                ),
            ),
        )
        before = self.path.read_bytes()
        for invalid in invalid_batches:
            with self.subTest(run_key=invalid.run_key):
                with self.assertRaises((LedgerError, ValueError)):
                    JsonLedgerStore(self.path).commit(invalid)
                self.assertEqual(self.path.read_bytes(), before)

    def test_runtime_rejects_v1_malformed_and_truncated_json(self):
        for raw in (
            json.dumps({"version": 1, "accounts": {}}),
            json.dumps({"version": 2, "accounts": []}),
            '{"version": 2, "accounts":',
        ):
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                with self.assertRaises(LedgerSchemaError):
                    JsonLedgerStore(self.path).list_accounts()

    def test_concurrent_different_runs_cannot_overwrite_each_other(self):
        store = JsonLedgerStore(self.path)
        errors: list[Exception] = []

        def commit_run(run_key: str) -> None:
            try:
                store.commit(
                    batch(
                        run_key=run_key,
                        events=(cash_event(f"cash-{run_key}", -10.0),),
                    )
                )
            except Exception as exc:  # one contender must observe stale state
                errors.append(exc)

        threads = [threading.Thread(target=commit_run, args=(f"run-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(JsonLedgerStore(self.path).load("strategy").available_cash, 990.0)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StalePortfolioSnapshotError)

    def test_process_lock_serializes_stale_writers(self):
        context = multiprocessing.get_context("spawn")
        results = context.Queue()
        processes = [
            context.Process(
                target=commit_from_process,
                args=(str(self.path), f"process-{index}", results),
            )
            for index in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)

        outcomes = sorted(results.get(timeout=2) for _ in processes)
        self.assertEqual(outcomes, ["OK", "StalePortfolioSnapshotError"])
        self.assertEqual(JsonLedgerStore(self.path).load("strategy").available_cash, 990.0)

    def test_canonical_progress_risk_and_carry_are_applied_once(self):
        intent = OrderIntent(
            id="intent-1",
            symbol="AAPL",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            quantity=2,
            reason="TARGET",
            created_snapshot_id="market-0",
        )
        fill_values = {
            "intent_id": intent.id,
            "symbol": intent.symbol,
            "position_side": intent.position_side,
            "order_side": intent.order_side,
            "snapshot_id": "market-1",
            "occurred_at": NOW,
            "quantity": 2,
            "price": 100.0,
            "fees": 1.0,
            "commission": 1.0,
            "status": "FILLED",
        }
        progress_fill = ExecutionProgressFill(
            id=stable_execution_progress_fill_id(**fill_values), **fill_values
        )
        progress = OrderExecutionProgress(
            intent_id=intent.id,
            symbol=intent.symbol,
            position_side=intent.position_side,
            order_side=intent.order_side,
            intent_quantity=2,
            execution_policy_fingerprint="policy-1",
            fills=(progress_fill,),
        )
        risk_update = PositionRiskUpdate(
            symbol="AAPL",
            side=PositionSide.LONG,
            peak_price=110.0,
            trough_price=None,
            trailing_active=True,
            position_mode="NORMAL",
        )
        decision = DecisionBatch(
            run_key="run-fill",
            strategy_id="strategy",
            strategy_revision=2,
            portfolio_snapshot_id="snapshot-0",
            market_snapshot_id="market-1",
            intents=(intent,),
            fills=(ExecutionFill(intent.id, "AAPL", 2, 100.0, 1.0, "FILLED"),),
            execution_progress=(progress,),
            position_risk_updates=(risk_update,),
        )

        committed = JsonLedgerStore(self.path).commit(decision)
        position = committed.positions[0]
        self.assertEqual(committed.available_cash, 799.0)
        self.assertEqual((position.symbol, position.quantity, position.average_cost), ("AAPL", 2, 100.0))
        self.assertEqual(position.peak_price, 110.0)
        self.assertTrue(position.trailing_active)
        persisted = json.loads(self.path.read_text(encoding="utf-8"))["accounts"][
            "strategy"
        ]
        self.assertEqual(
            [event["type"] for event in persisted["events"]],
            ["ORDER_FILLED", "RISK_CHANGED"],
        )
        before_repeat = self.path.read_bytes()
        repeated = JsonLedgerStore(self.path).commit(decision)
        self.assertEqual(repeated, committed)
        self.assertEqual(self.path.read_bytes(), before_repeat)

    def test_same_risk_update_in_different_runs_persists_distinct_canonical_facts(self):
        path, payload = self._persist_two_risk_updates()
        persisted = payload["accounts"]["risk-history"]

        facts = persisted["risk_facts"]
        self.assertEqual(len(facts), 2)
        self.assertEqual(len({fact["fact_id"] for fact in facts}), 2)
        self.assertEqual(
            [fact["run_key"] for fact in facts],
            ["risk-run-1", "risk-run-2"],
        )
        self.assertEqual(facts[0]["update"], facts[1]["update"])
        self.assertEqual(
            facts[0]["update"],
            {
                "symbol": "AAPL",
                "side": "LONG",
                "peak_price": 110.0,
                "trough_price": None,
                "trailing_active": True,
                "position_mode": "NORMAL",
            },
        )
        self.assertEqual(facts[0]["strategy_id"], "risk-history")
        self.assertEqual(facts[0]["strategy_revision"], 2)
        self.assertEqual(facts[0]["portfolio_snapshot_id"], "risk-snapshot-0")
        self.assertEqual(facts[0]["market_snapshot_id"], "risk-market-1")
        events = [event for event in persisted["events"] if event["type"] == "RISK_CHANGED"]
        self.assertEqual(len(events), 2)
        self.assertEqual(
            {event["data"]["risk_fact_id"] for event in events},
            {fact["fact_id"] for fact in facts},
        )
        committed_fact_ids = {
            fact_id
            for committed in persisted["committed_batches"]
            for fact_id in committed["risk_fact_ids"]
        }
        self.assertEqual(committed_fact_ids, {fact["fact_id"] for fact in facts})
        self.assertEqual(
            JsonLedgerStore(path).load("risk-history").positions[0].peak_price,
            110.0,
        )

    def test_copied_risk_event_with_a_new_id_is_rejected_without_writing(self):
        path, payload = self._persist_two_risk_updates()
        persisted = payload["accounts"]["risk-history"]
        copied = next(
            event for event in persisted["events"] if event["type"] == "RISK_CHANGED"
        ).copy()
        copied["id"] = "forged-risk-event-id"
        persisted["events"].append(copied)

        self._assert_corrupt_risk_payload_is_read_only(path, payload)

    def test_copied_risk_fact_is_rejected_without_writing(self):
        path, original = self._persist_two_risk_updates()
        for copied_as in ("same-id", "new-id"):
            with self.subTest(copied_as=copied_as):
                payload = json.loads(json.dumps(original))
                persisted = payload["accounts"]["risk-history"]
                copied = json.loads(json.dumps(persisted["risk_facts"][0]))
                if copied_as == "new-id":
                    copied["fact_id"] = "risk-fact-forged-copy"
                persisted["risk_facts"].append(copied)
                self._assert_corrupt_risk_payload_is_read_only(path, payload)

    def test_missing_risk_event_or_fact_is_rejected_without_writing(self):
        path, original = self._persist_two_risk_updates()
        for missing in ("event", "fact", "event-and-fact", "batch-fact-ids"):
            with self.subTest(missing=missing):
                payload = json.loads(json.dumps(original))
                persisted = payload["accounts"]["risk-history"]
                if missing == "event":
                    index = next(
                        index
                        for index, event in enumerate(persisted["events"])
                        if event["type"] == "RISK_CHANGED"
                    )
                    del persisted["events"][index]
                elif missing == "fact":
                    del persisted["risk_facts"][0]
                elif missing == "event-and-fact":
                    del persisted["risk_facts"][0]
                    index = next(
                        index
                        for index, event in enumerate(persisted["events"])
                        if event["type"] == "RISK_CHANGED"
                    )
                    del persisted["events"][index]
                else:
                    del persisted["committed_batches"][0]["risk_fact_ids"]
                self._assert_corrupt_risk_payload_is_read_only(path, payload)

    def test_tampered_risk_fact_or_event_content_is_rejected_without_writing(self):
        path, original = self._persist_two_risk_updates()
        for target in ("fact", "event", "batch", "append-order"):
            with self.subTest(target=target):
                payload = json.loads(json.dumps(original))
                persisted = payload["accounts"]["risk-history"]
                if target == "fact":
                    persisted["risk_facts"][0]["update"]["peak_price"] = 111.0
                elif target == "event":
                    event = next(
                        event
                        for event in persisted["events"]
                        if event["type"] == "RISK_CHANGED"
                    )
                    event["data"]["peak_price"] = 111.0
                elif target == "batch":
                    persisted["risk_facts"][0]["run_key"] = "risk-run-forged"
                else:
                    persisted["risk_facts"].reverse()
                self._assert_corrupt_risk_payload_is_read_only(path, payload)

    def test_execution_account_fact_must_match_replayed_progress(self):
        intent = OrderIntent(
            id="intent-account-proof",
            symbol="AAPL",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            quantity=1,
            reason="TARGET",
            created_snapshot_id="market-0",
        )
        fill_values = {
            "intent_id": intent.id,
            "symbol": intent.symbol,
            "position_side": intent.position_side,
            "order_side": intent.order_side,
            "snapshot_id": "market-1",
            "occurred_at": NOW,
            "quantity": 1,
            "price": 100.0,
            "fees": 0.0,
            "commission": 0.0,
            "status": "FILLED",
        }
        progress_fill = ExecutionProgressFill(
            id=stable_execution_progress_fill_id(**fill_values), **fill_values
        )
        progress = OrderExecutionProgress(
            intent.id,
            intent.symbol,
            intent.position_side,
            intent.order_side,
            1,
            "policy-1",
            (progress_fill,),
        )
        forged = account(cash=999.0)
        output = StageOutput(
            stage="execution_simulation",
            component_version="2.0.0",
            facts=(
                {"kind": "execution_account", "account": forged},
                {"kind": "execution_progress", "items": (progress,)},
            ),
        )
        before = self.path.read_bytes()

        with self.assertRaisesRegex(LedgerError, "execution_account"):
            JsonLedgerStore(self.path).commit(
                DecisionBatch(
                    run_key="account-proof",
                    strategy_id="strategy",
                    strategy_revision=2,
                    portfolio_snapshot_id="snapshot-0",
                    market_snapshot_id="market-1",
                    intents=(intent,),
                    fills=(
                        ExecutionFill(
                            intent.id,
                            "AAPL",
                            1,
                            100.0,
                            0.0,
                            "FILLED",
                        ),
                    ),
                    stage_outputs=(output,),
                )
            )

        self.assertEqual(self.path.read_bytes(), before)

    def test_real_execution_output_is_adopted_as_canonical_account(self):
        store = JsonLedgerStore(self.path)
        source = store.load("strategy")
        intent_values = {
            "symbol": "AAPL",
            "position_side": PositionSide.LONG,
            "order_side": OrderSide.BUY,
            "position_effect": PositionEffect.OPEN,
            "quantity": 2,
            "reason": "TARGET",
            "created_snapshot_id": "market-0",
            "created_market_at": INTENT_CREATED_AT,
        }
        intent = OrderIntent(
            id=stable_execution_intent_id(**intent_values),
            **intent_values,
        )
        market = MarketSnapshot(
            id="market-1",
            occurred_at=NOW,
            quotes={"AAPL": {"price": 100.0, "bar_volume": 10_000}},
        )
        policy = execution.ExecutionPolicy(
            market="us",
            lot_size=1,
            same_day_sell=True,
            commission_rate_pct=0.0,
            minimum_commission=0.0,
            stamp_duty_rate_pct=0.0,
            transfer_fee_rate_pct=0.0,
            slippage_bps=0.0,
            max_bar_participation_pct=100.0,
        )
        simulated = execution.execute_intents(source, (intent,), market, policy)
        forged_position = replace(
            simulated.account.positions[0],
            current_price=999.0,
            sellable_quantity=0,
            peak_price=999.0,
            trailing_active=True,
            position_mode="COVER_ONLY",
        )
        forged_account = replace(simulated.account, positions=(forged_position,))
        forged_output = StageOutput(
            stage="execution_simulation",
            component_version="2.0.0",
            facts=(
                {"kind": "execution_fills", "items": simulated.fills},
                {"kind": "execution_account", "account": forged_account},
                {"kind": "execution_progress", "items": simulated.progress},
                {
                    "kind": "position_settlement_updates",
                    "items": simulated.settlement_updates,
                },
            ),
        )
        before = self.path.read_bytes()
        with self.assertRaisesRegex(LedgerError, "execution_account"):
            store.commit(
                DecisionBatch(
                    run_key="forged-output",
                    strategy_id="strategy",
                    strategy_revision=2,
                    portfolio_snapshot_id="snapshot-0",
                    market_snapshot_id="market-1",
                    intents=(intent,),
                    stage_outputs=(forged_output,),
                )
            )
        self.assertEqual(self.path.read_bytes(), before)

        output = StageOutput(
            stage="execution_simulation",
            component_version="2.0.0",
            facts=(
                {"kind": "execution_fills", "items": simulated.fills},
                {"kind": "execution_account", "account": simulated.account},
                {"kind": "execution_progress", "items": simulated.progress},
                {
                    "kind": "position_settlement_updates",
                    "items": simulated.settlement_updates,
                },
            ),
        )

        committed = store.commit(
            DecisionBatch(
                run_key="real-output",
                strategy_id="strategy",
                strategy_revision=2,
                portfolio_snapshot_id="snapshot-0",
                market_snapshot_id="market-1",
                intents=(intent,),
                stage_outputs=(output,),
            )
        )

        self.assertEqual(committed.available_cash, simulated.account.available_cash)
        self.assertEqual(committed.positions, simulated.account.positions)
        self.assertNotEqual(committed.snapshot_id, simulated.account.snapshot_id)

    def test_execution_fill_summary_must_match_progress(self):
        intent = OrderIntent(
            id="intent-fill-proof",
            symbol="AAPL",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            quantity=2,
            reason="TARGET",
            created_snapshot_id="market-0",
        )
        fill_values = {
            "intent_id": intent.id,
            "symbol": intent.symbol,
            "position_side": intent.position_side,
            "order_side": intent.order_side,
            "snapshot_id": "market-1",
            "occurred_at": NOW,
            "quantity": 2,
            "price": 100.0,
            "fees": 1.0,
            "commission": 1.0,
            "status": "FILLED",
        }
        detail = ExecutionProgressFill(
            id=stable_execution_progress_fill_id(**fill_values), **fill_values
        )
        progress = OrderExecutionProgress(
            intent.id,
            intent.symbol,
            intent.position_side,
            intent.order_side,
            2,
            "policy-1",
            (detail,),
        )
        before = self.path.read_bytes()

        with self.assertRaisesRegex(LedgerError, "fill summary"):
            JsonLedgerStore(self.path).commit(
                DecisionBatch(
                    run_key="fill-proof",
                    strategy_id="strategy",
                    strategy_revision=2,
                    portfolio_snapshot_id="snapshot-0",
                    market_snapshot_id="market-1",
                    intents=(intent,),
                    fills=(ExecutionFill(intent.id, "AAPL", 1, 100.0, 1.0, "FILLED"),),
                    execution_progress=(progress,),
                )
            )

        self.assertEqual(self.path.read_bytes(), before)

    def test_progress_fill_without_summary_is_rejected_without_writing(self):
        intent = OrderIntent(
            id="intent-missing-summary",
            symbol="AAPL",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            quantity=1,
            reason="TARGET",
            created_snapshot_id="market-0",
        )
        fill_values = {
            "intent_id": intent.id,
            "symbol": intent.symbol,
            "position_side": intent.position_side,
            "order_side": intent.order_side,
            "snapshot_id": "market-1",
            "occurred_at": NOW,
            "quantity": 1,
            "price": 100.0,
            "fees": 0.0,
            "commission": 0.0,
            "status": "FILLED",
        }
        detail = ExecutionProgressFill(
            id=stable_execution_progress_fill_id(**fill_values), **fill_values
        )
        progress = OrderExecutionProgress(
            intent.id,
            intent.symbol,
            intent.position_side,
            intent.order_side,
            1,
            "policy-1",
            (detail,),
        )
        before = self.path.read_bytes()

        with self.assertRaisesRegex(LedgerError, "fill summary"):
            JsonLedgerStore(self.path).commit(
                DecisionBatch(
                    run_key="missing-fill-summary",
                    strategy_id="strategy",
                    strategy_revision=2,
                    portfolio_snapshot_id="snapshot-0",
                    market_snapshot_id="market-1",
                    intents=(intent,),
                    execution_progress=(progress,),
                )
            )

        self.assertEqual(self.path.read_bytes(), before)

    def test_append_only_partial_progress_replays_only_new_fill(self):
        intent = OrderIntent(
            id="intent-partial",
            symbol="AAPL",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            quantity=4,
            reason="TARGET",
            created_snapshot_id="market-0",
        )

        def progress_fill(snapshot: str, occurred_at: datetime, status: str):
            values = {
                "intent_id": intent.id,
                "symbol": intent.symbol,
                "position_side": intent.position_side,
                "order_side": intent.order_side,
                "snapshot_id": snapshot,
                "occurred_at": occurred_at,
                "quantity": 2,
                "price": 100.0,
                "fees": 0.0,
                "commission": 0.0,
                "status": status,
            }
            return ExecutionProgressFill(
                id=stable_execution_progress_fill_id(**values), **values
            )

        first_fill = progress_fill("market-1", NOW, "PARTIAL")
        first_progress = OrderExecutionProgress(
            intent.id,
            intent.symbol,
            intent.position_side,
            intent.order_side,
            4,
            "policy-1",
            (first_fill,),
        )
        store = JsonLedgerStore(self.path)
        first = store.commit(
            DecisionBatch(
                run_key="partial-1",
                strategy_id="strategy",
                strategy_revision=2,
                portfolio_snapshot_id="snapshot-0",
                market_snapshot_id="market-1",
                intents=(intent,),
                fills=(ExecutionFill(intent.id, "AAPL", 2, 100.0, 0.0, "PARTIAL"),),
                execution_progress=(first_progress,),
            )
        )
        second_fill = progress_fill("market-2", NOW + timedelta(minutes=5), "FILLED")
        second_progress = OrderExecutionProgress(
            intent.id,
            intent.symbol,
            intent.position_side,
            intent.order_side,
            4,
            "policy-1",
            (first_fill, second_fill),
        )
        second = store.commit(
            DecisionBatch(
                run_key="partial-2",
                strategy_id="strategy",
                strategy_revision=2,
                portfolio_snapshot_id=first.snapshot_id,
                market_snapshot_id="market-2",
                intents=(intent,),
                fills=(ExecutionFill(intent.id, "AAPL", 2, 100.0, 0.0, "FILLED"),),
                execution_progress=(second_progress,),
            )
        )
        self.assertEqual(second.positions[0].quantity, 4)
        self.assertEqual(second.available_cash, 600.0)

    def test_identical_fill_summaries_use_progress_fill_identity_across_snapshots(self):
        intent = OrderIntent(
            id="intent-identical-fills",
            symbol="AAPL",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            quantity=3,
            reason="TARGET",
            created_snapshot_id="market-0",
        )

        def detail(snapshot: str, occurred_at: datetime) -> ExecutionProgressFill:
            values = {
                "intent_id": intent.id,
                "symbol": intent.symbol,
                "position_side": intent.position_side,
                "order_side": intent.order_side,
                "snapshot_id": snapshot,
                "occurred_at": occurred_at,
                "quantity": 1,
                "price": 100.0,
                "fees": 0.0,
                "commission": 0.0,
                "status": "PARTIAL",
            }
            return ExecutionProgressFill(
                id=stable_execution_progress_fill_id(**values), **values
            )

        first_detail = detail("market-1", NOW)
        first_progress = OrderExecutionProgress(
            intent.id,
            intent.symbol,
            intent.position_side,
            intent.order_side,
            3,
            "policy-1",
            (first_detail,),
        )
        summary = ExecutionFill(intent.id, "AAPL", 1, 100.0, 0.0, "PARTIAL")
        store = JsonLedgerStore(self.path)
        first = store.commit(
            DecisionBatch(
                run_key="identical-fill-1",
                strategy_id="strategy",
                strategy_revision=2,
                portfolio_snapshot_id="snapshot-0",
                market_snapshot_id="market-1",
                intents=(intent,),
                fills=(summary,),
                execution_progress=(first_progress,),
            )
        )
        second_detail = detail("market-2", NOW + timedelta(minutes=5))
        second_progress = replace(
            first_progress,
            fills=(first_detail, second_detail),
        )
        store.commit(
            DecisionBatch(
                run_key="identical-fill-2",
                strategy_id="strategy",
                strategy_revision=2,
                portfolio_snapshot_id=first.snapshot_id,
                market_snapshot_id="market-2",
                intents=(intent,),
                fills=(summary,),
                execution_progress=(second_progress,),
            )
        )

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        persisted = payload["accounts"]["strategy"]
        self.assertEqual(len(persisted["fills"]), 2)
        self.assertEqual(
            {item["progress_fill_id"] for item in persisted["fills"]},
            {first_detail.id, second_detail.id},
        )
        self.assertEqual(store.load("strategy").positions[0].quantity, 2)

        persisted["fills"].pop()
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(LedgerSchemaError, "fill.*progress|one-to-one"):
            store.load("strategy")

    def test_carry_record_is_accounted_once_and_requires_matching_event_type(self):
        financed = AccountSnapshot(
            id="account-strategy",
            strategy_id="strategy",
            strategy_revision=2,
            occurred_at=NOW,
            available_cash=100.0,
            margin_loan=500.0,
            financing_lifecycle=AccrualLifecycle("financing-1", date(2026, 7, 30)),
        )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["accounts"]["strategy"].update(encode_account_snapshot(financed))
        payload["accounts"]["strategy"]["portfolio_snapshot_id"] = "snapshot-0"
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        record = CarryAccrualRecord(
            account_id=financed.id,
            cost_type=CarryCostType.FINANCING,
            accrual_date=date(2026, 7, 31),
            elapsed_days=1,
            amount=2.0,
            lifecycle_id="financing-1",
        )
        event = PortfolioEvent(
            id="carry-1",
            type="FINANCING_COST_ACCRUED",
            occurred_at=NOW,
            data={
                "account_id": record.account_id,
                "cost_type": record.cost_type.value,
                "lifecycle_id": record.lifecycle_id,
                "symbol": record.symbol,
                "accrual_date": record.accrual_date.isoformat(),
                "elapsed_days": record.elapsed_days,
                "amount": record.amount,
            },
        )
        decision = DecisionBatch(
            run_key="carry-1",
            strategy_id="strategy",
            strategy_revision=2,
            portfolio_snapshot_id="snapshot-0",
            market_snapshot_id="market-1",
            carry_accruals=(record,),
            events=(event,),
        )
        committed = JsonLedgerStore(self.path).commit(decision)
        self.assertEqual(committed.available_cash, 98.0)
        self.assertEqual(committed.accrued_financing_cost, 2.0)
        self.assertEqual(committed.carry_accruals, (record,))

    def test_fill_without_canonical_progress_fails_without_writing(self):
        before = self.path.read_bytes()
        decision = DecisionBatch(
            run_key="fill-only",
            strategy_id="strategy",
            strategy_revision=2,
            portfolio_snapshot_id="snapshot-0",
            market_snapshot_id="market-1",
            fills=(ExecutionFill("missing", "AAPL", 1, 100.0, 0.0, "FILLED"),),
        )
        with self.assertRaisesRegex(ValueError, "canonical execution progress"):
            JsonLedgerStore(self.path).commit(decision)
        self.assertEqual(self.path.read_bytes(), before)

    def test_same_run_key_with_different_facts_is_rejected(self):
        store = JsonLedgerStore(self.path)
        store.commit(batch(events=(cash_event("cash-1", -10.0),)))
        before = self.path.read_bytes()

        with self.assertRaisesRegex(ValueError, "run_key.*different"):
            store.commit(batch(events=(cash_event("cash-forged", -20.0),)))

        self.assertEqual(self.path.read_bytes(), before)

    def test_same_run_key_fingerprint_covers_stage_outputs_and_diagnostics(self):
        store = JsonLedgerStore(self.path)
        original = DecisionBatch(
            run_key="observable-fingerprint",
            strategy_id="strategy",
            strategy_revision=2,
            portfolio_snapshot_id="snapshot-0",
            market_snapshot_id="market-1",
            diagnostics=({"code": "BASE", "score": 1.0},),
            stage_outputs=(
                StageOutput(
                    stage="audit",
                    component_version="1.0.0",
                    facts=({"kind": "noncanonical_audit", "value": 1},),
                    diagnostics=({"code": "STAGE_BASE"},),
                ),
            ),
        )
        committed = store.commit(original)

        self.assertEqual(store.commit(original), committed)
        for changed in (
            DecisionBatch(
                run_key=original.run_key,
                strategy_id="strategy",
                strategy_revision=2,
                portfolio_snapshot_id="snapshot-0",
                market_snapshot_id="market-1",
                diagnostics=({"code": "CHANGED", "score": 1.0},),
                stage_outputs=original.stage_outputs,
            ),
            DecisionBatch(
                run_key=original.run_key,
                strategy_id="strategy",
                strategy_revision=2,
                portfolio_snapshot_id="snapshot-0",
                market_snapshot_id="market-1",
                diagnostics=original.diagnostics,
                stage_outputs=(
                    StageOutput(
                        stage="audit",
                        component_version="1.0.0",
                        facts=({"kind": "noncanonical_audit", "value": 2},),
                        diagnostics=({"code": "STAGE_BASE"},),
                    ),
                ),
            ),
        ):
            with self.subTest(changed=changed):
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "run_key.*different"):
                    store.commit(changed)
                self.assertEqual(self.path.read_bytes(), before)

    def test_carry_record_and_event_must_be_one_to_one(self):
        financed = AccountSnapshot(
            id="account-strategy",
            strategy_id="strategy",
            strategy_revision=2,
            occurred_at=NOW,
            available_cash=100.0,
            margin_loan=500.0,
            financing_lifecycle=AccrualLifecycle("financing-1", date(2026, 7, 30)),
        )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["accounts"]["strategy"].update(encode_account_snapshot(financed))
        payload["accounts"]["strategy"]["portfolio_snapshot_id"] = "snapshot-0"
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        record = CarryAccrualRecord(
            account_id=financed.id,
            cost_type=CarryCostType.FINANCING,
            accrual_date=date(2026, 7, 31),
            elapsed_days=1,
            amount=2.0,
            lifecycle_id="financing-1",
        )
        before = self.path.read_bytes()

        with self.assertRaisesRegex(ValueError, "carry.*event"):
            JsonLedgerStore(self.path).commit(
                DecisionBatch(
                    run_key="carry-unpaired",
                    strategy_id="strategy",
                    strategy_revision=2,
                    portfolio_snapshot_id="snapshot-0",
                    market_snapshot_id="market-1",
                    carry_accruals=(record,),
                )
            )

        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
