from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .runtime_runs import recent_runtime_runs


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock-runtime-runs")
    parser.add_argument("--path", type=Path)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    for item in recent_runtime_runs(path=args.path, limit=args.limit):
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
