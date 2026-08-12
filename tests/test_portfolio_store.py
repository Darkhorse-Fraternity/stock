from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from stock_recommender.portfolio_engine.ledger import JsonLedgerStore
from stock_recommender.portfolio_engine.archived_store import ArchivedLedgerStore
from stock_recommender.portfolio_store import open_portfolio_store


class PortfolioStoreFactoryTests(unittest.TestCase):
    def test_json_is_explicit_local_default(self):
        store = open_portfolio_store(
            Path("local-ledger.json"),
            environ={},
        )

        self.assertIsInstance(store, JsonLedgerStore)
        self.assertEqual(store.path, Path("local-ledger.json"))

    def test_postgres_requires_database_url(self):
        with self.assertRaisesRegex(ValueError, "STOCK_AGENT_PORTFOLIO_DATABASE_URL"):
            open_portfolio_store(environ={"STOCK_AGENT_LEDGER_BACKEND": "postgres"})

    def test_large_json_runtime_fails_closed(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.json"
            path.write_bytes(b"01234567890")

            with self.assertRaisesRegex(RuntimeError, "Configure PostgreSQL"):
                open_portfolio_store(
                    path,
                    environ={"STOCK_AGENT_JSON_LEDGER_MAX_BYTES": "10"},
                )

    def test_postgres_constructs_independent_adapter(self):
        sentinel = object()
        with patch(
            "stock_recommender.portfolio_engine.postgres_store.PostgresLedgerStore",
            return_value=sentinel,
        ) as constructor:
            store = open_portfolio_store(
                environ={
                    "STOCK_AGENT_LEDGER_BACKEND": "postgres",
                    "STOCK_AGENT_PORTFOLIO_DATABASE_URL": "postgresql://ledger/db",
                    "STOCK_AGENT_PORTFOLIO_SCHEMA": "portfolio_live",
                }
            )

        self.assertIs(store, sentinel)
        constructor.assert_called_once_with(
            "postgresql://ledger/db",
            schema="portfolio_live",
        )

    def test_postgres_can_decorate_live_store_with_frozen_archive(self):
        sentinel = object()
        with patch(
            "stock_recommender.portfolio_engine.postgres_store.PostgresLedgerStore",
            return_value=sentinel,
        ):
            store = open_portfolio_store(
                environ={
                    "STOCK_AGENT_LEDGER_BACKEND": "postgres",
                    "STOCK_AGENT_PORTFOLIO_DATABASE_URL": "postgresql://ledger/db",
                    "STOCK_AGENT_PORTFOLIO_ARCHIVE_PATH": "history/ledger.json",
                }
            )

        self.assertIsInstance(store, ArchivedLedgerStore)
        self.assertIs(store.live, sentinel)
        self.assertEqual(store.archive.path, Path("history/ledger.json"))

    def test_unknown_backend_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "must be 'json' or 'postgres'"):
            open_portfolio_store(environ={"STOCK_AGENT_LEDGER_BACKEND": "sqlite"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
