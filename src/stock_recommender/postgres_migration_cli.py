"""CLI for the one-way live ledger cutover to PostgreSQL."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .portfolio_engine.ledger import portfolio_ledger_path
from .portfolio_engine.postgres_migration import (
    PostgresBootstrapError,
    bootstrap_postgres_from_json,
)
from .portfolio_store import (
    DEFAULT_POSTGRES_SCHEMA,
    LEDGER_DATABASE_URL_ENV,
    LEDGER_SCHEMA_ENV,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-portfolio-bootstrap-postgres",
        description="Freeze JSON history and bootstrap current portfolio state into PostgreSQL.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate without writing")
    mode.add_argument("--apply", action="store_true", help="archive and import accounts")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--database-url")
    parser.add_argument("--schema")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = args.database_url or os.getenv(LEDGER_DATABASE_URL_ENV)
    schema = args.schema or os.getenv(LEDGER_SCHEMA_ENV) or DEFAULT_POSTGRES_SCHEMA
    try:
        report = bootstrap_postgres_from_json(
            args.source or portfolio_ledger_path(),
            archive_path=args.archive,
            apply=args.apply,
            database_url=database_url,
            schema=schema,
        )
    except (PostgresBootstrapError, ValueError, RuntimeError) as exc:
        print(f"PostgreSQL bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
