from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from stock_recommender import cli
from stock_recommender.runtime_runs import recent_runtime_runs, record_runtime_run


class RuntimeRunJournalTests(unittest.TestCase):
    def test_journal_is_bounded_and_newest_first(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "runs.sqlite3"
            for index in range(4):
                record_runtime_run(
                    {
                        "started_at": f"2026-08-11T00:0{index}:00+00:00",
                        "status": "succeeded",
                        "mode": "risk",
                        "strategy_id": "strategy",
                        "duration_seconds": float(index),
                    },
                    path=path,
                    max_rows=2,
                )

            rows = recent_runtime_runs(path=path, limit=10)

            self.assertEqual(len(rows), 2)
            self.assertEqual(
                [item["duration_seconds"] for item in rows],
                [3.0, 2.0],
            )

    def test_cli_records_success_and_failure_without_swallowing_error(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "runs.sqlite3"
            environment = {
                "STOCK_AGENT_RUN_LOG_PATH": str(path),
                "STOCK_AGENT_MODE": "risk",
                "STOCK_AGENT_STRATEGY_ID": "strategy",
                "STOCK_AGENT_LEDGER_BACKEND": "postgres",
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(cli, "_run"):
                    cli.main()
                with patch.object(cli, "_run", side_effect=RuntimeError("boom")):
                    with self.assertRaisesRegex(RuntimeError, "boom"):
                        cli.main()

            failed, succeeded = recent_runtime_runs(path=path, limit=10)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error_type"], "RuntimeError")
            self.assertEqual(succeeded["status"], "succeeded")
            self.assertEqual(succeeded["mode"], "risk")
            self.assertEqual(succeeded["ledger_backend"], "postgres")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
