from __future__ import annotations

import json
import multiprocessing
import os
import stat
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from stock_recommender.portfolio_engine.config import (
    default_exposure_policy,
    default_margin_policy,
    default_short_policy,
)
from stock_recommender.portfolio_engine.ledger import JsonLedgerStore, LedgerSchemaError
from stock_recommender.portfolio_engine import migration as migration_module
from stock_recommender.portfolio_engine.migration import (
    MigrationError,
    migrate_portfolio_store,
    migrate_stores,
    migrate_strategy_store,
)
from stock_recommender.portfolio_migration_cli import main as migration_main
from stock_recommender.parameters import (
    StrategyLifecycleError,
    load_strategy_store,
)


FIXED_NOW = datetime(2026, 8, 1, 12, 34, 56, tzinfo=timezone.utc)


def tree_snapshot(root: Path) -> tuple[tuple[str, str, bytes | str | None], ...]:
    entries = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            entries.append((relative, "symlink", os.readlink(path)))
        elif path.is_dir():
            entries.append((relative, "directory", None))
        else:
            entries.append((relative, "file", path.read_bytes()))
    return tuple(entries)


def write_two_sources_under_exclusive_locks(
    strategy_path: str,
    strategy_bytes: bytes,
    portfolio_path: str,
    portfolio_bytes: bytes,
    first_written: multiprocessing.synchronize.Event,
    finish: multiprocessing.synchronize.Event,
) -> None:
    import fcntl

    paths = tuple(sorted((Path(strategy_path), Path(portfolio_path))))
    handles = {path: path.open("r+b") for path in paths}
    try:
        for path in paths:
            fcntl.flock(handles[path].fileno(), fcntl.LOCK_EX)
        strategy_handle = handles[Path(strategy_path)]
        strategy_handle.seek(0)
        strategy_handle.write(strategy_bytes)
        strategy_handle.truncate()
        strategy_handle.flush()
        os.fsync(strategy_handle.fileno())
        first_written.set()
        if not finish.wait(timeout=10):
            raise RuntimeError("timed out waiting to finish concurrent source write")
        portfolio_handle = handles[Path(portfolio_path)]
        portfolio_handle.seek(0)
        portfolio_handle.write(portfolio_bytes)
        portfolio_handle.truncate()
        portfolio_handle.flush()
        os.fsync(portfolio_handle.fileno())
    finally:
        for path in reversed(paths):
            try:
                fcntl.flock(handles[path].fileno(), fcntl.LOCK_UN)
            finally:
                handles[path].close()


def crash_combined_migration_after_first_replace(
    strategy_path: str,
    portfolio_path: str,
) -> None:
    from stock_recommender.portfolio_engine import atomic_io

    actual_replace = atomic_io.replace_path
    calls = 0

    def replace_then_crash(source: Path, target: Path) -> None:
        nonlocal calls
        actual_replace(source, target)
        calls += 1
        if calls == 1:
            os._exit(86)

    with patch.object(atomic_io, "replace_path", side_effect=replace_then_crash):
        migrate_stores(
            strategy_path,
            portfolio_path,
            apply=True,
            now=FIXED_NOW,
        )


def crash_atomic_replace_with_missing_original(root: str) -> None:
    from stock_recommender.portfolio_engine import atomic_io

    directory = Path(root)
    missing = directory / "first" / "missing.json"
    existing = directory / "second" / "existing.json"
    backup = directory / "second" / "existing.backup"
    actual_replace = atomic_io.replace_path
    calls = 0

    def replace_then_crash(source: Path, target: Path) -> None:
        nonlocal calls
        actual_replace(source, target)
        calls += 1
        if calls == 1:
            os._exit(87)

    with patch.object(atomic_io, "replace_path", side_effect=replace_then_crash):
        atomic_io.atomic_replace_many(
            {missing: b"new missing", existing: b"new existing"},
            originals={
                missing: atomic_io.OriginalFile(False, None),
                existing: atomic_io.OriginalFile(True, b"old existing"),
            },
            recovery_backups={missing: None, existing: backup},
            durable=True,
        )


def crash_combined_migration_after_commit_marker(
    strategy_path: str,
    portfolio_path: str,
) -> None:
    from stock_recommender.portfolio_engine import atomic_io

    def crash_before_cleanup(paths: object) -> None:
        del paths
        os._exit(88)

    with patch.object(atomic_io, "_remove_paths", side_effect=crash_before_cleanup):
        migrate_stores(
            strategy_path,
            portfolio_path,
            apply=True,
            now=FIXED_NOW,
        )


def v1_long_only_store() -> dict:
    return {
        "version": 1,
        "accounts": {
            "tech": {
                "id": "account-tech",
                "strategy_id": "tech",
                "strategy_revision": 7,
                "updated_at": "2026-07-31T16:00:00+00:00",
                "cash": 1_000.0,
                "reserved_cash": 100.0,
                "latest_nav": 1_120.0,
                "positions": {
                    "600001": {
                        "symbol": "600001",
                        "quantity": 10,
                        "average_cost": 10.0,
                        "current_price": 12.0,
                        "peak_price": 13.0,
                        "trailing_active": True,
                        "sellable_quantity": 8,
                        "sellable_on": "2026-08-01",
                    }
                },
                "orders": [
                    {
                        "id": "buy-open",
                        "symbol": "600002",
                        "side": "BUY",
                        "quantity": 2,
                        "filled_quantity": 0,
                        "status": "INTENDED",
                        "reason": "new",
                    },
                    {
                        "id": "buy-increase",
                        "symbol": "600001",
                        "side": "BUY",
                        "quantity": 2,
                        "filled_quantity": 0,
                        "status": "ACCEPTED",
                        "reason": "add",
                    },
                    {
                        "id": "sell-reduce",
                        "symbol": "600001",
                        "side": "SELL",
                        "quantity": 4,
                        "filled_quantity": 0,
                        "status": "PARTIAL",
                        "reason": "trim",
                    },
                    {
                        "id": "finished-order",
                        "symbol": "600001",
                        "side": "SELL",
                        "quantity": 10,
                        "filled_quantity": 10,
                        "status": "FILLED",
                        "reason": "historical",
                    },
                ],
                "events": [{"id": "legacy-event", "type": "ORDER_FILLED"}],
                "committed_run_keys": ["daily:2026-07-31"],
            }
        },
    }


def v5_strategy_store() -> dict:
    strategy = {
        "version": 5,
        "id": "active-tech",
        "revision": 11,
        "parent_revision": 10,
        "name": "US AI",
        "description": "keep me",
        "created_at": "2026-07-01T08:00:00+08:00",
        "updated_at": "2026-07-31T08:00:00+08:00",
        "lifecycle": {"stage": "paper", "approved_by": "owner"},
        "delivery": {"enabled": True, "channel": "feishu", "target": "group"},
        "signal": {"model": "factor-rank-v2", "run_time": "09:30"},
        "allocation": {"model": "risk-parity", "maximum_positions": 10},
        "validation": {"minimum_trading_days": 20},
        "portfolio": {"initial_cash": 100_000.0, "max_positions": 10},
        "parameters": {
            "market": {"enabled": True, "value": "us"},
            "watchlist": {"enabled": True, "value": ["NVDA", "MSFT"]},
        },
        "custom_extension": {"must": ["survive", 5]},
    }
    return {"version": 5, "active_strategy_id": "active-tech", "strategies": [strategy]}


class PortfolioMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        root = Path(self.temporary.name)
        self.portfolio_path = root / "strategy_portfolios.json"
        self.strategy_path = root / "strategy_config.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_dry_run_does_not_write_and_preserves_nav(self) -> None:
        self._write_json(self.portfolio_path, v1_long_only_store())
        before = self.portfolio_path.read_bytes()

        report = migrate_portfolio_store(self.portfolio_path, apply=False, now=FIXED_NOW)

        self.assertEqual(self.portfolio_path.read_bytes(), before)
        self.assertTrue(report.changed)
        self.assertFalse(report.applied)
        self.assertTrue(report.nav_parity)
        self.assertIsNone(report.backup_path)

    def test_single_store_dry_runs_leave_directory_entries_and_bytes_unchanged(self) -> None:
        cases = (
            ("strategy", v5_strategy_store(), migrate_strategy_store),
            ("portfolio", v1_long_only_store(), migrate_portfolio_store),
        )
        for name, payload, migrate in cases:
            with self.subTest(name=name):
                root = Path(self.temporary.name) / f"single-{name}"
                root.mkdir()
                target = root / f"{name}.json"
                self._write_json(target, payload)
                before = tree_snapshot(root)

                report = migrate(target, apply=False, now=FIXED_NOW)

                self.assertTrue(report.changed)
                self.assertEqual(tree_snapshot(root), before)

    def test_combined_dry_run_across_directories_is_byte_for_byte_zero_write(self) -> None:
        root = Path(self.temporary.name) / "combined-check"
        strategy_path = root / "strategy" / "strategy.json"
        portfolio_path = root / "portfolio" / "portfolio.json"
        strategy_path.parent.mkdir(parents=True)
        portfolio_path.parent.mkdir(parents=True)
        self._write_json(strategy_path, v5_strategy_store())
        self._write_json(portfolio_path, v1_long_only_store())
        before = tree_snapshot(root)

        report = migrate_stores(
            strategy_path,
            portfolio_path,
            apply=False,
            now=FIXED_NOW,
        )

        self.assertTrue(report.strategy.changed)
        self.assertTrue(report.portfolio.changed)
        self.assertEqual(tree_snapshot(root), before)

    def test_dry_run_missing_and_symlink_sources_fail_without_any_write(self) -> None:
        root = Path(self.temporary.name) / "invalid-check"
        root.mkdir()
        missing = root / "missing.json"
        before_missing = tree_snapshot(root)
        with self.assertRaises(MigrationError):
            migrate_portfolio_store(missing, apply=False, now=FIXED_NOW)
        self.assertEqual(tree_snapshot(root), before_missing)

        real = root / "real.json"
        link = root / "link.json"
        self._write_json(real, v1_long_only_store())
        link.symlink_to(real)
        before_link = tree_snapshot(root)
        with self.assertRaises(MigrationError):
            migrate_portfolio_store(link, apply=False, now=FIXED_NOW)
        self.assertEqual(tree_snapshot(root), before_link)

    def test_combined_dry_run_never_observes_cooperative_writer_mixed_generation(self) -> None:
        root = Path(self.temporary.name) / "concurrent-check"
        strategy_path = root / "a-strategy" / "strategy.json"
        portfolio_path = root / "z-portfolio" / "portfolio.json"
        strategy_path.parent.mkdir(parents=True)
        portfolio_path.parent.mkdir(parents=True)
        old_strategy = v5_strategy_store()
        old_strategy["strategies"][0]["description"] = "old-generation"
        new_strategy = deepcopy(old_strategy)
        new_strategy["strategies"][0]["description"] = "new-generation"
        old_portfolio = v1_long_only_store()
        old_portfolio["accounts"]["tech"]["updated_at"] = "2026-07-30T16:00:00+00:00"
        new_portfolio = deepcopy(old_portfolio)
        new_portfolio["accounts"]["tech"]["updated_at"] = "2026-07-31T16:00:00+00:00"
        self._write_json(strategy_path, old_strategy)
        self._write_json(portfolio_path, old_portfolio)
        context = multiprocessing.get_context("spawn")
        first_written = context.Event()
        finish = context.Event()
        writer = context.Process(
            target=write_two_sources_under_exclusive_locks,
            args=(
                str(strategy_path),
                json.dumps(new_strategy).encode("utf-8"),
                str(portfolio_path),
                json.dumps(new_portfolio).encode("utf-8"),
                first_written,
                finish,
            ),
        )
        writer.start()
        self.assertTrue(first_written.wait(timeout=5))
        observed: list[str] = []
        original_strategy_prepare = migration_module._prepare_strategy
        original_portfolio_prepare = migration_module._prepare_portfolio
        strategy_prepared = threading.Event()
        outcome: list[BaseException | object] = []

        def prepare_strategy(path: Path, raw: bytes):
            observed.append(json.loads(raw)["strategies"][0]["description"])
            strategy_prepared.set()
            return original_strategy_prepare(path, raw)

        def prepare_portfolio(path: Path, raw: bytes, now: datetime):
            observed.append(json.loads(raw)["accounts"]["tech"]["updated_at"])
            return original_portfolio_prepare(path, raw, now)

        def check() -> None:
            try:
                outcome.append(
                    migrate_stores(
                        strategy_path,
                        portfolio_path,
                        apply=False,
                        now=FIXED_NOW,
                    )
                )
            except BaseException as exc:
                outcome.append(exc)

        with patch.object(migration_module, "_prepare_strategy", side_effect=prepare_strategy), patch.object(
            migration_module,
            "_prepare_portfolio",
            side_effect=prepare_portfolio,
        ):
            checker = threading.Thread(target=check)
            checker.start()
            strategy_prepared.wait(timeout=0.25)
            finish.set()
            checker.join(timeout=10)
        writer.join(timeout=10)

        self.assertFalse(checker.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(writer.exitcode, 0)
        self.assertEqual(len(outcome), 1)
        if isinstance(outcome[0], BaseException):
            self.assertIsInstance(outcome[0], MigrationError)
            self.assertRegex(str(outcome[0]), "changed|retry|consistent")
        else:
            self.assertIn(
                tuple(observed),
                {
                    ("old-generation", "2026-07-30T16:00:00+00:00"),
                    ("new-generation", "2026-07-31T16:00:00+00:00"),
                },
            )

    def test_nav_mismatch_fails_preflight_without_writing(self) -> None:
        store = v1_long_only_store()
        store["accounts"]["tech"]["latest_nav"] = 9_999.0
        self._write_json(self.portfolio_path, store)
        before = self.portfolio_path.read_bytes()

        with self.assertRaisesRegex(MigrationError, "NAV parity"):
            migrate_portfolio_store(self.portfolio_path, apply=True, now=FIXED_NOW)

        self.assertEqual(self.portfolio_path.read_bytes(), before)
        self.assertEqual(list(self.portfolio_path.parent.glob("*.bak.*")), [])

    def test_nonstandard_nan_json_is_rejected_without_writing(self) -> None:
        self.portfolio_path.write_text(
            '{"version": 1, "accounts": {}, "unexpected": NaN}',
            encoding="utf-8",
        )
        before = self.portfolio_path.read_bytes()

        with self.assertRaises(MigrationError):
            migrate_portfolio_store(self.portfolio_path, apply=False, now=FIXED_NOW)

        self.assertEqual(self.portfolio_path.read_bytes(), before)

    def test_apply_creates_backup_and_explicit_long_state(self) -> None:
        self._write_json(self.portfolio_path, v1_long_only_store())

        report = migrate_portfolio_store(self.portfolio_path, apply=True, now=FIXED_NOW)
        migrated = json.loads(self.portfolio_path.read_text(encoding="utf-8"))
        account = migrated["accounts"]["tech"]

        self.assertEqual(migrated["version"], 2)
        self.assertEqual(set(migrated), {"version", "accounts"})
        self.assertEqual(
            set(account),
            {
                "id",
                "strategy_id",
                "strategy_revision",
                "occurred_at",
                "available_cash",
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
                "events",
                "committed_batches",
            },
        )
        self.assertEqual(account["positions"]["600001"]["side"], "LONG")
        self.assertEqual(account["positions"]["600001"]["quantity"], 10)
        self.assertEqual(account["available_cash"], 1_000.0)
        self.assertEqual(account["reserved_cash"], 100.0)
        self.assertEqual(account["restricted_short_proceeds"], 0.0)
        self.assertEqual(account["margin_loan"], 0.0)
        self.assertEqual(account["risk_facts"], [])
        self.assertTrue(
            all(
                committed["risk_fact_ids"] == []
                for committed in account["committed_batches"]
            )
        )
        intents = {item["id"]: item for item in account["open_intents"]}
        self.assertEqual(intents["buy-open"]["position_effect"], "OPEN")
        self.assertEqual(intents["buy-increase"]["position_effect"], "INCREASE")
        self.assertEqual(intents["sell-reduce"]["position_effect"], "REDUCE")
        self.assertTrue(all(item["position_side"] == "LONG" for item in intents.values()))
        self.assertTrue(report.backup_path.is_file())
        self.assertEqual(JsonLedgerStore(self.portfolio_path).load("tech").strategy_revision, 7)

    def test_sell_of_entire_long_position_becomes_close(self) -> None:
        store = v1_long_only_store()
        store["accounts"]["tech"]["orders"] = [
            {
                "id": "sell-close",
                "symbol": "600001",
                "side": "SELL",
                "quantity": 12,
                "filled_quantity": 2,
                "status": "PARTIAL",
                "reason": "exit",
            }
        ]
        self._write_json(self.portfolio_path, store)

        migrate_portfolio_store(self.portfolio_path, apply=True, now=FIXED_NOW)
        migrated = json.loads(self.portfolio_path.read_text(encoding="utf-8"))
        intent = migrated["accounts"]["tech"]["open_intents"][0]

        self.assertEqual(intent["quantity"], 10)
        self.assertEqual(intent["position_effect"], "CLOSE")

    def test_aggregate_open_sell_orders_cannot_exceed_position(self) -> None:
        store = v1_long_only_store()
        store["accounts"]["tech"]["orders"] = [
            {
                "id": "sell-a",
                "symbol": "600001",
                "side": "SELL",
                "quantity": 6,
                "filled_quantity": 0,
                "status": "INTENDED",
                "reason": "trim a",
            },
            {
                "id": "sell-b",
                "symbol": "600001",
                "side": "SELL",
                "quantity": 6,
                "filled_quantity": 0,
                "status": "ACCEPTED",
                "reason": "trim b",
            },
        ]
        self._write_json(self.portfolio_path, store)
        before = self.portfolio_path.read_bytes()

        with self.assertRaisesRegex(MigrationError, "aggregate SELL"):
            migrate_portfolio_store(self.portfolio_path, apply=True, now=FIXED_NOW)

        self.assertEqual(self.portfolio_path.read_bytes(), before)

    def test_strategy_migration_preserves_business_state_and_writes_complete_policies(self) -> None:
        original = v5_strategy_store()
        expected = deepcopy(original["strategies"][0])
        self._write_json(self.strategy_path, original)

        report = migrate_strategy_store(self.strategy_path, apply=True, now=FIXED_NOW)
        migrated = json.loads(self.strategy_path.read_text(encoding="utf-8"))
        strategy = migrated["strategies"][0]

        self.assertEqual(migrated["version"], 6)
        self.assertEqual(migrated["active_strategy_id"], "active-tech")
        for key in (
            "id",
            "revision",
            "parent_revision",
            "lifecycle",
            "delivery",
            "signal",
            "allocation",
            "parameters",
            "custom_extension",
        ):
            self.assertEqual(strategy[key], expected[key])
        self.assertEqual(strategy["version"], 6)
        self.assertEqual(strategy["exposure_policy"], default_exposure_policy())
        self.assertEqual(strategy["margin_policy"], default_margin_policy())
        self.assertEqual(strategy["short_policy"], default_short_policy())
        self.assertEqual(strategy["exposure_policy"]["mode"], "LONG_ONLY")
        self.assertTrue(report.backup_path.is_file())

    def test_combined_preflight_failure_writes_neither_store(self) -> None:
        self._write_json(self.strategy_path, v5_strategy_store())
        self.portfolio_path.write_text("{truncated", encoding="utf-8")
        strategy_before = self.strategy_path.read_bytes()
        portfolio_before = self.portfolio_path.read_bytes()

        with self.assertRaises(MigrationError):
            migrate_stores(
                self.strategy_path,
                self.portfolio_path,
                apply=True,
                now=FIXED_NOW,
            )

        self.assertEqual(self.strategy_path.read_bytes(), strategy_before)
        self.assertEqual(self.portfolio_path.read_bytes(), portfolio_before)
        self.assertEqual(list(self.strategy_path.parent.glob("*.bak")), [])

    def test_combined_apply_uses_timestamped_sibling_backups(self) -> None:
        self._write_json(self.strategy_path, v5_strategy_store())
        self._write_json(self.portfolio_path, v1_long_only_store())

        report = migrate_stores(
            self.strategy_path,
            self.portfolio_path,
            apply=True,
            now=FIXED_NOW,
        )

        self.assertTrue(report.strategy.backup_path.is_file())
        self.assertTrue(report.portfolio.backup_path.is_file())
        strategy_stamp = report.strategy.backup_path.name.split(".bak.")[-1]
        portfolio_stamp = report.portfolio.backup_path.name.split(".bak.")[-1]
        self.assertEqual(strategy_stamp, portfolio_stamp)

    def test_second_replace_failure_rolls_back_both_and_retains_backups(self) -> None:
        self._write_json(self.strategy_path, v5_strategy_store())
        self._write_json(self.portfolio_path, v1_long_only_store())
        strategy_before = self.strategy_path.read_bytes()
        portfolio_before = self.portfolio_path.read_bytes()
        from stock_recommender.portfolio_engine import atomic_io

        actual_replace = atomic_io.replace_path
        calls = 0

        def fail_second(source: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second replacement failure")
            actual_replace(source, target)

        with patch.object(atomic_io, "replace_path", side_effect=fail_second):
            with self.assertRaises(MigrationError):
                migrate_stores(
                    self.strategy_path,
                    self.portfolio_path,
                    apply=True,
                    now=FIXED_NOW,
                )

        self.assertEqual(self.strategy_path.read_bytes(), strategy_before)
        self.assertEqual(self.portfolio_path.read_bytes(), portfolio_before)
        self.assertEqual(len(list(self.strategy_path.parent.glob("*.bak.*"))), 2)

    def test_directory_fsync_failure_rolls_back_both_and_cleans_temps(self) -> None:
        self._write_json(self.strategy_path, v5_strategy_store())
        self._write_json(self.portfolio_path, v1_long_only_store())
        strategy_before = self.strategy_path.read_bytes()
        portfolio_before = self.portfolio_path.read_bytes()
        from stock_recommender.portfolio_engine import atomic_io

        actual_fsync_directory = atomic_io.fsync_directory
        calls = 0

        def fail_commit_fsync(directory: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected commit directory fsync failure")
            actual_fsync_directory(directory)

        with patch.object(
            atomic_io,
            "fsync_directory",
            side_effect=fail_commit_fsync,
        ):
            with self.assertRaises(MigrationError):
                migrate_stores(
                    self.strategy_path,
                    self.portfolio_path,
                    apply=True,
                    now=FIXED_NOW,
                )

        self.assertEqual(self.strategy_path.read_bytes(), strategy_before)
        self.assertEqual(self.portfolio_path.read_bytes(), portfolio_before)
        self.assertEqual(tuple(self.strategy_path.parent.glob(".*.tmp")), ())

    def test_runtime_loaders_recover_crash_after_first_participant_replace(self) -> None:
        triggers = ("strategy", "portfolio")
        for trigger in triggers:
            with self.subTest(trigger=trigger):
                root = Path(self.temporary.name) / trigger
                strategy_path = root / "strategy" / "strategy_config.json"
                portfolio_path = root / "portfolio" / "strategy_portfolios.json"
                strategy_path.parent.mkdir(parents=True)
                portfolio_path.parent.mkdir(parents=True)
                self._write_json(strategy_path, v5_strategy_store())
                self._write_json(portfolio_path, v1_long_only_store())
                strategy_before = strategy_path.read_bytes()
                portfolio_before = portfolio_path.read_bytes()
                process = multiprocessing.get_context("spawn").Process(
                    target=crash_combined_migration_after_first_replace,
                    args=(str(strategy_path), str(portfolio_path)),
                )

                process.start()
                process.join(timeout=10)

                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 86)
                if trigger == "strategy":
                    with self.assertRaises(StrategyLifecycleError):
                        load_strategy_store(strategy_path)
                    with self.assertRaises(StrategyLifecycleError):
                        load_strategy_store(strategy_path)
                else:
                    with self.assertRaises(LedgerSchemaError):
                        JsonLedgerStore(portfolio_path).list_accounts()
                    with self.assertRaises(LedgerSchemaError):
                        JsonLedgerStore(portfolio_path).list_accounts()
                self.assertEqual(strategy_path.read_bytes(), strategy_before)
                self.assertEqual(portfolio_path.read_bytes(), portfolio_before)
                self.assertEqual(tuple(root.rglob(".stock-portfolio-txn-*")), ())
                self.assertEqual(tuple(root.rglob(".*.tmp")), ())

    def test_commit_marker_recovery_finishes_new_generation(self) -> None:
        root = Path(self.temporary.name) / "committed-crash"
        strategy_path = root / "strategy" / "strategy_config.json"
        portfolio_path = root / "portfolio" / "strategy_portfolios.json"
        strategy_path.parent.mkdir(parents=True)
        portfolio_path.parent.mkdir(parents=True)
        self._write_json(strategy_path, v5_strategy_store())
        self._write_json(portfolio_path, v1_long_only_store())
        process = multiprocessing.get_context("spawn").Process(
            target=crash_combined_migration_after_commit_marker,
            args=(str(strategy_path), str(portfolio_path)),
        )

        process.start()
        process.join(timeout=10)

        self.assertEqual(process.exitcode, 88)
        self.assertEqual(load_strategy_store(strategy_path)["version"], 6)
        self.assertEqual(
            JsonLedgerStore(portfolio_path).list_accounts()[0].strategy_id,
            "tech",
        )
        self.assertEqual(tuple(root.rglob(".stock-portfolio-txn-*")), ())
        self.assertEqual(tuple(root.rglob(".*.tmp")), ())

    def test_durable_recovery_restores_nonexistent_original_across_directories(self) -> None:
        from stock_recommender.portfolio_engine import atomic_io

        root = Path(self.temporary.name) / "missing-original"
        (root / "first").mkdir(parents=True)
        (root / "second").mkdir(parents=True)
        existing = root / "second" / "existing.json"
        backup = root / "second" / "existing.backup"
        existing.write_bytes(b"old existing")
        backup.write_bytes(b"old existing")
        process = multiprocessing.get_context("spawn").Process(
            target=crash_atomic_replace_with_missing_original,
            args=(str(root),),
        )

        process.start()
        process.join(timeout=10)

        self.assertEqual(process.exitcode, 87)
        atomic_io.recover_pending_transactions(root / "first" / "missing.json")
        atomic_io.recover_pending_transactions(existing)
        self.assertFalse((root / "first" / "missing.json").exists())
        self.assertEqual(existing.read_bytes(), b"old existing")
        self.assertEqual(tuple(root.rglob(".stock-portfolio-txn-*")), ())
        self.assertEqual(tuple(root.rglob(".*.tmp")), ())

    def test_backup_copies_source_permissions_under_permissive_umask(self) -> None:
        self._write_json(self.portfolio_path, v1_long_only_store())
        self.portfolio_path.chmod(0o600)
        previous_umask = os.umask(0o022)
        try:
            report = migrate_portfolio_store(
                self.portfolio_path,
                apply=True,
                now=FIXED_NOW,
            )
        finally:
            os.umask(previous_umask)

        self.assertIsNotNone(report.backup_path)
        self.assertEqual(
            stat.S_IMODE(report.backup_path.stat().st_mode),
            0o600,
        )

    def test_symlink_source_and_backup_are_rejected_without_writing(self) -> None:
        real_source = self.portfolio_path.with_name("real-portfolio.json")
        self._write_json(real_source, v1_long_only_store())
        self.portfolio_path.symlink_to(real_source)
        source_before = real_source.read_bytes()

        with self.assertRaises(MigrationError):
            migrate_portfolio_store(self.portfolio_path, apply=True, now=FIXED_NOW)
        self.assertEqual(real_source.read_bytes(), source_before)

        self.portfolio_path.unlink()
        self._write_json(self.portfolio_path, v1_long_only_store())
        backup = self.portfolio_path.with_name(
            self.portfolio_path.name + ".bak.20260801T123456000000Z"
        )
        victim = self.portfolio_path.with_name("backup-victim")
        victim.write_bytes(b"do not follow")
        backup.symlink_to(victim)
        before = self.portfolio_path.read_bytes()

        with self.assertRaises(MigrationError):
            migrate_portfolio_store(self.portfolio_path, apply=True, now=FIXED_NOW)
        self.assertEqual(self.portfolio_path.read_bytes(), before)
        self.assertEqual(victim.read_bytes(), b"do not follow")

    def test_current_schema_rerun_is_explicit_and_does_not_write(self) -> None:
        self._write_json(self.strategy_path, v5_strategy_store())
        self._write_json(self.portfolio_path, v1_long_only_store())
        migrate_stores(
            self.strategy_path,
            self.portfolio_path,
            apply=True,
            now=FIXED_NOW,
        )
        strategy_before = self.strategy_path.read_bytes()
        portfolio_before = self.portfolio_path.read_bytes()
        backups_before = set(self.strategy_path.parent.glob("*.bak.*"))

        report = migrate_stores(
            self.strategy_path,
            self.portfolio_path,
            apply=True,
            now=FIXED_NOW,
        )

        self.assertFalse(report.strategy.changed)
        self.assertFalse(report.portfolio.changed)
        self.assertFalse(report.strategy.applied)
        self.assertFalse(report.portfolio.applied)
        self.assertEqual(self.strategy_path.read_bytes(), strategy_before)
        self.assertEqual(self.portfolio_path.read_bytes(), portfolio_before)
        self.assertEqual(set(self.strategy_path.parent.glob("*.bak.*")), backups_before)

    def test_cli_check_and_apply_use_explicit_paths(self) -> None:
        self._write_json(self.strategy_path, v5_strategy_store())
        self._write_json(self.portfolio_path, v1_long_only_store())
        before = self.portfolio_path.read_bytes()

        check_status = migration_main(
            [
                "--strategy-path",
                str(self.strategy_path),
                "--portfolio-path",
                str(self.portfolio_path),
                "--check",
            ]
        )
        self.assertEqual(check_status, 0)
        self.assertEqual(self.portfolio_path.read_bytes(), before)

        apply_status = migration_main(
            [
                "--strategy-path",
                str(self.strategy_path),
                "--portfolio-path",
                str(self.portfolio_path),
                "--apply",
            ]
        )
        self.assertEqual(apply_status, 0)
        self.assertEqual(json.loads(self.portfolio_path.read_text())["version"], 2)

    def test_cli_requires_exactly_one_mode(self) -> None:
        with self.assertRaises(SystemExit):
            migration_main([])
        with self.assertRaises(SystemExit):
            migration_main(["--check", "--apply"])


if __name__ == "__main__":
    unittest.main()
