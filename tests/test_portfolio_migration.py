from __future__ import annotations

import json
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
from stock_recommender.portfolio_engine.ledger import JsonLedgerStore
from stock_recommender.portfolio_engine.migration import (
    MigrationError,
    migrate_portfolio_store,
    migrate_stores,
    migrate_strategy_store,
)
from stock_recommender.portfolio_migration_cli import main as migration_main


FIXED_NOW = datetime(2026, 8, 1, 12, 34, 56, tzinfo=timezone.utc)


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
        self.assertEqual(account["positions"]["600001"]["side"], "LONG")
        self.assertEqual(account["positions"]["600001"]["quantity"], 10)
        self.assertEqual(account["available_cash"], 1_000.0)
        self.assertEqual(account["reserved_cash"], 100.0)
        self.assertEqual(account["restricted_short_proceeds"], 0.0)
        self.assertEqual(account["margin_loan"], 0.0)
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
        from stock_recommender.portfolio_engine import migration

        actual_replace = migration._replace_path
        calls = 0

        def fail_second(source: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second replacement failure")
            actual_replace(source, target)

        with patch.object(migration, "_replace_path", side_effect=fail_second):
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
