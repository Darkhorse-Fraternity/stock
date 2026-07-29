from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .data_sources import fetch_board_quotes, fetch_sina_fallback_quotes
from .universe import normalize_stock_symbol


SNAPSHOT_SCHEMA_VERSION = 1


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def board_universe_cache_dir(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    configured = os.getenv(
        "STOCK_AGENT_BOARD_UNIVERSE_CACHE_DIR",
        "data/board_universe_cache",
    ).strip()
    return Path(configured or "data/board_universe_cache").expanduser()


def _snapshot_rows(rows: Iterable[dict], board_name: str) -> list[dict]:
    normalized: list[dict] = []
    used: set[str] = set()
    for raw in rows:
        try:
            symbol = normalize_stock_symbol(raw.get("symbol"))
        except (AttributeError, ValueError):
            continue
        if symbol in used:
            continue
        used.add(symbol)
        sector = str(raw.get("sector") or board_name)
        sectors = [
            str(item).strip()
            for item in (raw.get("sectors") or ([sector] if sector else []))
            if str(item).strip()
        ]
        normalized.append(
            {
                "symbol": symbol,
                "name": str(raw.get("name") or symbol),
                "sector": sector,
                "sectors": sectors,
            }
        )
    normalized.sort(key=lambda item: item["symbol"])
    return normalized


@dataclass(frozen=True, slots=True)
class BoardUniverseSnapshot:
    board_code: str
    board_name: str
    fetched_at: datetime
    rows: tuple[dict, ...]


class BoardUniverseSnapshotStore:
    def __init__(self, cache_dir: str | Path | None = None):
        self.cache_dir = board_universe_cache_dir(cache_dir)

    def _path(self, board_code: str) -> Path:
        safe_code = "".join(character for character in str(board_code) if character.isalnum() or character in "-_")
        if not safe_code:
            raise ValueError("板块代码不能为空")
        return self.cache_dir / f"{safe_code}.json"

    def save(
        self,
        board_code: str,
        board_name: str,
        rows: Iterable[dict],
        *,
        now: datetime | None = None,
    ) -> BoardUniverseSnapshot:
        normalized = _snapshot_rows(rows, board_name)
        if not normalized:
            raise ValueError("不能保存空板块股票池")
        fetched_at = _utc_now(now)
        target = self._path(board_code)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "board_code": str(board_code),
            "board_name": str(board_name),
            "fetched_at": fetched_at.isoformat(timespec="seconds"),
            "rows": normalized,
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
        return BoardUniverseSnapshot(
            board_code=str(board_code),
            board_name=str(board_name),
            fetched_at=fetched_at,
            rows=tuple(normalized),
        )

    def load(self, board_code: str) -> BoardUniverseSnapshot | None:
        target = self._path(board_code)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(str(payload.get("fetched_at") or ""))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            return None
        if str(payload.get("board_code") or "") != str(board_code):
            return None
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        rows = _snapshot_rows(payload.get("rows") or [], str(payload.get("board_name") or ""))
        if not rows:
            return None
        return BoardUniverseSnapshot(
            board_code=str(board_code),
            board_name=str(payload.get("board_name") or ""),
            fetched_at=fetched_at.astimezone(timezone.utc),
            rows=tuple(rows),
        )


@dataclass(frozen=True, slots=True)
class UniverseQuoteBatch:
    rows: tuple[dict, ...]
    mode: str
    board_code: str
    board_name: str
    primary_error: str | None = None
    quote_error: str | None = None
    snapshot_count: int = 0
    snapshot_fetched_at: str | None = None

    @property
    def error(self) -> str | None:
        if self.rows:
            return None
        errors = [item for item in (self.primary_error, self.quote_error) if item]
        return "；".join(errors) or "未获得有效板块行情"

    def diagnostics(self) -> dict:
        return {
            "source_mode": self.mode,
            "primary_error": self.primary_error,
            "quote_error": self.quote_error,
            "snapshot_count": self.snapshot_count,
            "snapshot_fetched_at": self.snapshot_fetched_at,
        }


class BoardUniverseProvider:
    """Fetch a complete board universe, then resolve current quotes independently."""

    def __init__(
        self,
        *,
        primary_fetcher: Callable = fetch_board_quotes,
        quote_fetcher: Callable = fetch_sina_fallback_quotes,
        snapshot_store: BoardUniverseSnapshotStore | None = None,
        universe_limit: int | None = None,
        snapshot_max_age_days: float | None = None,
    ):
        self.primary_fetcher = primary_fetcher
        self.quote_fetcher = quote_fetcher
        self.snapshot_store = snapshot_store or BoardUniverseSnapshotStore()
        configured_limit = int(os.getenv("STOCK_AGENT_BOARD_UNIVERSE_LIMIT", "1000"))
        self.universe_limit = max(1, int(universe_limit if universe_limit is not None else configured_limit))
        configured_age = float(os.getenv("STOCK_AGENT_BOARD_SNAPSHOT_MAX_AGE_DAYS", "7"))
        self.snapshot_max_age_days = max(
            0.0,
            float(snapshot_max_age_days if snapshot_max_age_days is not None else configured_age),
        )

    def fetch(
        self,
        board_code: str,
        *,
        board_name: str,
        now: datetime | None = None,
    ) -> UniverseQuoteBatch:
        current = _utc_now(now)
        try:
            rows, primary_error = self.primary_fetcher(
                board_code,
                board_name=board_name,
                limit=self.universe_limit,
            )
        except Exception as exc:
            rows, primary_error = [], str(exc)
        if rows and not primary_error:
            ordered = sorted((dict(item) for item in rows), key=lambda item: str(item.get("symbol") or ""))
            try:
                snapshot = self.snapshot_store.save(board_code, board_name, ordered, now=current)
                snapshot_count = len(snapshot.rows)
                snapshot_fetched_at = snapshot.fetched_at.isoformat(timespec="seconds")
                snapshot_error = None
            except Exception as exc:
                snapshot_count = len(ordered)
                snapshot_fetched_at = None
                snapshot_error = f"板块快照保存失败：{exc}"
            return UniverseQuoteBatch(
                rows=tuple(ordered),
                mode="primary",
                board_code=str(board_code),
                board_name=str(board_name),
                quote_error=snapshot_error,
                snapshot_count=snapshot_count,
                snapshot_fetched_at=snapshot_fetched_at,
            )

        snapshot = self.snapshot_store.load(board_code)
        if snapshot is None:
            return UniverseQuoteBatch(
                rows=(),
                mode="unavailable",
                board_code=str(board_code),
                board_name=str(board_name),
                primary_error=primary_error or "主板块数据源未返回有效成分",
                quote_error="没有可用的完整板块股票池快照",
            )

        snapshot_age_days = (current - snapshot.fetched_at).total_seconds() / 86_400
        if snapshot_age_days > self.snapshot_max_age_days:
            return UniverseQuoteBatch(
                rows=(),
                mode="unavailable",
                board_code=str(board_code),
                board_name=str(board_name),
                primary_error=primary_error or "主板块数据源未返回有效成分",
                quote_error=f"板块股票池快照已过期（{snapshot_age_days:.1f} 天）",
                snapshot_count=len(snapshot.rows),
                snapshot_fetched_at=snapshot.fetched_at.isoformat(timespec="seconds"),
            )

        try:
            fallback_rows, quote_error = self.quote_fetcher(
                symbols=snapshot.rows,
                board_name=board_name,
                source_label=f"新浪财经实时行情（{board_name}完整板块快照）",
            )
        except Exception as exc:
            fallback_rows, quote_error = [], str(exc)
        ordered = sorted(
            (dict(item) for item in fallback_rows),
            key=lambda item: str(item.get("symbol") or ""),
        )
        return UniverseQuoteBatch(
            rows=tuple(ordered),
            mode="snapshot_realtime" if ordered else "unavailable",
            board_code=str(board_code),
            board_name=str(board_name),
            primary_error=primary_error,
            quote_error=quote_error,
            snapshot_count=len(snapshot.rows),
            snapshot_fetched_at=snapshot.fetched_at.isoformat(timespec="seconds"),
        )
