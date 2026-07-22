#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from stock_recommender.parameters import load_strategy_store, strategy_config_path
from stock_recommender.performance import (
    recommendation_history_path,
    reconcile_recommendation_history_strategies,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile orphaned recommendation history strategy IDs")
    parser.add_argument("--config", default=str(strategy_config_path()))
    parser.add_argument("--history", default=str(recommendation_history_path()))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="OLD_ID=NEW_ID",
        help="explicitly map an orphaned strategy ID to a current strategy ID",
    )
    args = parser.parse_args()
    explicit_mapping = {}
    for item in args.map:
        old_id, separator, new_id = item.partition("=")
        if not separator or not old_id.strip() or not new_id.strip():
            parser.error(f"invalid --map value: {item}")
        explicit_mapping[old_id.strip()] = new_id.strip()
    result = reconcile_recommendation_history_strategies(
        load_strategy_store(args.config),
        path=args.history,
        dry_run=args.dry_run,
        explicit_mapping=explicit_mapping,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["unresolved_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
