from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .config import DEFAULT_BOARD_CODE, DEFAULT_BOARD_NAME
from .enrichment import fetch_daily_history
from .data_sources import akshare_symbol
from .market_history import save_daily_history_cache
from .parameters import load_strategy_config, parameter_value
from .signal_engine import extract_signal_features, normalize_signal_history
from .universe_provider import BoardUniverseProvider
from .utils import beijing_now


def _download_sina_warmup_history(symbol: str, *, now: datetime) -> list[dict]:
    import akshare as ak

    frame = ak.stock_zh_a_daily(
        symbol=akshare_symbol(symbol),
        start_date=(now.date() - timedelta(days=550)).strftime("%Y%m%d"),
        end_date=(now.date() + timedelta(days=1)).strftime("%Y%m%d"),
        adjust="qfq",
    )
    return [
        {
            "date": row.get("date"),
            "open": row.get("open"),
            "close": row.get("close"),
            "high": row.get("high"),
            "low": row.get("low"),
            "volume": row.get("volume"),
            "turnover": row.get("amount"),
        }
        for _, row in frame.iterrows()
    ]


def warmup_status_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    return Path(
        os.getenv("STOCK_AGENT_HISTORY_WARMUP_STATUS_PATH", "data/history_warmup_status.json")
    ).expanduser()


def save_warmup_status(payload: dict, path: str | Path | None = None) -> None:
    target = warmup_status_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(target)


def warm_board_history_cache(
    *,
    board_code: str,
    board_name: str,
    provider: BoardUniverseProvider | None = None,
    history_fetcher: Callable | None = None,
    secondary_history_fetcher: Callable | None = None,
    workers: int | None = None,
    minimum_history_rows: int = 61,
    now: datetime | None = None,
    cache_dir: str | Path | None = None,
    force_refresh: bool = True,
) -> dict:
    current = beijing_now(now)
    universe = (provider or BoardUniverseProvider()).fetch(
        board_code,
        board_name=board_name,
        now=current,
    )
    result = {
        "schema_version": 1,
        "started_at": current.isoformat(timespec="seconds"),
        "completed_at": None,
        "status": "RUNNING",
        "board_code": str(board_code),
        "board_name": str(board_name),
        "source_mode": universe.mode,
        "universe_count": len(universe.rows),
        "ready_count": 0,
        "failed_count": 0,
        "minimum_history_rows": max(61, int(minimum_history_rows)),
        "errors": [],
    }
    if not universe.rows:
        result.update(
            {
                "status": "BLOCKED",
                "completed_at": beijing_now().isoformat(timespec="seconds"),
                "failed_count": 0,
                "errors": [universe.error or "板块股票池不可用"],
            }
        )
        return result

    fetcher = history_fetcher or fetch_daily_history
    secondary_fetcher = (
        secondary_history_fetcher
        if secondary_history_fetcher is not None
        else (_download_sina_warmup_history if history_fetcher is None else None)
    )
    worker_count = max(
        1,
        int(workers if workers is not None else os.getenv("STOCK_AGENT_HISTORY_WARMUP_WORKERS", "8")),
    )

    def warm(row: dict) -> tuple[str, str | None]:
        symbol = str(row.get("symbol") or "")
        try:
            try:
                history = fetcher(
                    symbol,
                    attempts=1,
                    force_refresh=force_refresh,
                    now=current,
                )
            except Exception as primary_error:
                if secondary_fetcher is None:
                    raise
                try:
                    history = secondary_fetcher(symbol, now=current)
                except Exception as secondary_error:
                    raise RuntimeError(
                        f"主源失败：{primary_error}；新浪源失败：{secondary_error}"
                    ) from secondary_error
            history = save_daily_history_cache(
                symbol,
                history,
                cache_dir=cache_dir,
                now=current,
            )
            normalized = normalize_signal_history(history, cutoff=current.date())
            features = extract_signal_features(
                normalized,
                minimum_rows=result["minimum_history_rows"],
            )
            if features is None:
                return symbol, f"有效历史不足 {result['minimum_history_rows']} 行"
            return symbol, None
        except Exception as exc:
            return symbol, str(exc)

    errors: list[str] = []
    ready_count = 0
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="stock-history-warmup") as executor:
        futures = [executor.submit(warm, dict(row)) for row in universe.rows]
        for future in as_completed(futures):
            symbol, error = future.result()
            if error:
                if len(errors) < 20:
                    errors.append(f"{symbol}: {error}")
            else:
                ready_count += 1

    result.update(
        {
            "status": "COMPLETED",
            "completed_at": beijing_now().isoformat(timespec="seconds"),
            "ready_count": ready_count,
            "failed_count": len(universe.rows) - ready_count,
            "errors": errors,
        }
    )
    return result


def main() -> None:
    strategy = load_strategy_config()
    board_code = os.getenv("STOCK_AGENT_BOARD_CODE") or str(
        parameter_value(strategy, "board_code", DEFAULT_BOARD_CODE)
    )
    board_name = os.getenv("STOCK_AGENT_BOARD_NAME") or str(
        parameter_value(strategy, "board_name", DEFAULT_BOARD_NAME)
    )
    result = warm_board_history_cache(
        board_code=board_code,
        board_name=board_name,
        minimum_history_rows=int(strategy.get("signal", {}).get("minimum_history_rows", 61)),
        force_refresh=os.getenv("STOCK_AGENT_HISTORY_WARMUP_FORCE_REFRESH", "1") == "1",
    )
    save_warmup_status(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "COMPLETED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
