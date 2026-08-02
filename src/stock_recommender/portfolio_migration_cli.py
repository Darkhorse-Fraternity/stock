"""Command-line entry point for the one-way portfolio storage migration."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .portfolio_engine.ledger import portfolio_ledger_path
from .portfolio_engine.migration import MigrationError, migrate_stores


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-portfolio-migrate",
        description="Preflight or atomically apply strategy-v6 and ledger-v2 migrations.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate without writing")
    mode.add_argument("--apply", action="store_true", help="back up and migrate both stores")
    parser.add_argument("--strategy-path", type=Path)
    parser.add_argument("--portfolio-path", type=Path)
    return parser


def _json_report(report: object) -> str:
    return json.dumps(asdict(report), ensure_ascii=False, default=str, sort_keys=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.strategy_path is None:
        from .parameters import strategy_config_path

        strategy_path = strategy_config_path()
    else:
        strategy_path = args.strategy_path
    if args.portfolio_path is None:
        portfolio_path = portfolio_ledger_path()
    else:
        portfolio_path = args.portfolio_path
    try:
        report = migrate_stores(
            strategy_path,
            portfolio_path,
            apply=args.apply,
        )
    except MigrationError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    print(_json_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
