from __future__ import annotations

import json
import math
import os
import queue
import tempfile
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


CACHE_SCHEMA_VERSION = 1
DEFAULT_CACHE_TTL_SECONDS = 6 * 60 * 60
DEFAULT_FETCH_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 1.0
_REFRESH_LOCK = threading.Lock()
_REFRESHING_SYMBOLS: set[str] = set()
_REFRESH_QUEUE: queue.Queue[tuple] = queue.Queue()
_REFRESH_WORKERS_STARTED = False


class DailyHistoryUnavailableError(RuntimeError):
    pass


def market_history_cache_dir(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    configured = os.getenv("STOCK_AGENT_MARKET_HISTORY_CACHE_DIR", "data/market_history_cache").strip()
    return Path(configured or "data/market_history_cache").expanduser()


def _validated_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip()
    if len(normalized) != 6 or not normalized.isdigit():
        raise ValueError(f"无效股票代码：{symbol}")
    return normalized


def _cache_path(symbol: str, cache_dir: str | Path | None = None) -> Path:
    return market_history_cache_dir(cache_dir) / f"{_validated_symbol(symbol)}.json"


def _aware_utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def normalize_daily_history(rows: Iterable[dict]) -> list[dict]:
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = {str(key): _json_value(value) for key, value in row.items()}
        if item.get("date"):
            item["date"] = str(item["date"])[:10]
        normalized.append(item)
    return normalized


def _read_cache(symbol: str, cache_dir: str | Path | None = None) -> tuple[datetime, list[dict]] | None:
    target = _cache_path(symbol, cache_dir)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(str(payload.get("fetched_at") or ""))
        rows = payload.get("rows")
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if payload.get("schema_version") != CACHE_SCHEMA_VERSION or payload.get("symbol") != symbol:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    if not isinstance(rows, list):
        return None
    normalized = normalize_daily_history(item for item in rows if isinstance(item, dict))
    return fetched_at.astimezone(timezone.utc), normalized


def save_daily_history_cache(
    symbol: str,
    rows: Iterable[dict],
    *,
    cache_dir: str | Path | None = None,
    now: datetime | None = None,
) -> list[dict]:
    normalized_symbol = _validated_symbol(symbol)
    normalized_rows = normalize_daily_history(rows)
    if not normalized_rows:
        raise DailyHistoryUnavailableError(f"{normalized_symbol} 未返回可缓存日线")
    target = _cache_path(normalized_symbol, cache_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "symbol": normalized_symbol,
        "fetched_at": _aware_utc(now).isoformat(timespec="seconds"),
        "rows": normalized_rows,
    }
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
    return normalized_rows


def _background_refresh_worker() -> None:
    while True:
        (
            symbol,
            loader,
            cache_dir,
            attempts,
            backoff_seconds,
            now,
        ) = _REFRESH_QUEUE.get()
        try:
            maximum_attempts = max(1, int(attempts))
            delay = max(0.0, float(backoff_seconds))
            for attempt in range(maximum_attempts):
                try:
                    save_daily_history_cache(
                        symbol,
                        loader(),
                        cache_dir=cache_dir,
                        now=now,
                    )
                    return
                except Exception:
                    if attempt + 1 < maximum_attempts and delay > 0:
                        time.sleep(delay * (2**attempt))
        finally:
            with _REFRESH_LOCK:
                _REFRESHING_SYMBOLS.discard(symbol)
            _REFRESH_QUEUE.task_done()


def _ensure_background_refresh_workers() -> None:
    global _REFRESH_WORKERS_STARTED
    with _REFRESH_LOCK:
        if _REFRESH_WORKERS_STARTED:
            return
        worker_count = max(1, int(os.getenv("STOCK_AGENT_HISTORY_BACKGROUND_WORKERS", "4")))
        for index in range(worker_count):
            threading.Thread(
                target=_background_refresh_worker,
                name=f"stock-history-refresh-{index + 1}",
                daemon=True,
            ).start()
        _REFRESH_WORKERS_STARTED = True


def _schedule_background_refresh(
    symbol: str,
    loader: Callable[[], Iterable[dict]],
    *,
    cache_dir: str | Path | None,
    attempts: int,
    backoff_seconds: float,
    now: datetime,
) -> bool:
    _ensure_background_refresh_workers()
    with _REFRESH_LOCK:
        if symbol in _REFRESHING_SYMBOLS:
            return False
        _REFRESHING_SYMBOLS.add(symbol)
    _REFRESH_QUEUE.put((symbol, loader, cache_dir, attempts, backoff_seconds, now))
    return True


def fetch_daily_history_with_cache(
    symbol: str,
    loader: Callable[[], Iterable[dict]],
    *,
    cache_dir: str | Path | None = None,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    attempts: int = DEFAULT_FETCH_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
    background_refresh: bool = False,
    force_refresh: bool = False,
) -> list[dict]:
    normalized_symbol = _validated_symbol(symbol)
    current = _aware_utc(now)
    cached = _read_cache(normalized_symbol, cache_dir)
    ttl = max(0.0, float(cache_ttl_seconds))
    if cached and not force_refresh and (current - cached[0]).total_seconds() <= ttl:
        return cached[1]
    if cached and not force_refresh:
        if background_refresh:
            _schedule_background_refresh(
                normalized_symbol,
                loader,
                cache_dir=cache_dir,
                attempts=attempts,
                backoff_seconds=backoff_seconds,
                now=current,
            )
        return cached[1]

    maximum_attempts = max(1, int(attempts))
    delay = max(0.0, float(backoff_seconds))
    last_error: Exception | None = None
    for attempt in range(maximum_attempts):
        try:
            return save_daily_history_cache(
                normalized_symbol,
                loader(),
                cache_dir=cache_dir,
                now=current,
            )
        except Exception as exc:
            last_error = exc
            if attempt + 1 < maximum_attempts and delay > 0:
                sleep(delay * (2**attempt))

    if cached:
        return cached[1]
    message = f"{normalized_symbol} 日线获取失败，已重试 {maximum_attempts} 次"
    raise DailyHistoryUnavailableError(message) from last_error
