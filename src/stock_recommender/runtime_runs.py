"""Bounded, process-safe runtime execution journal."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


DEFAULT_MAX_RUN_ROWS = 2_000
_URL_CREDENTIAL = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s]+):[^@/\s]+@", re.I)
_NAMED_SECRET = re.compile(
    r"(?i)\b(password|token|secret|api[_-]?key)=([^&\s]+)"
)


def _redact_message(value: str) -> str:
    value = _URL_CREDENTIAL.sub(r"\1:***@", value)
    return _NAMED_SECRET.sub(r"\1=***", value)


def runtime_run_log_path(path: str | Path | None = None) -> Path:
    configured = (
        str(path)
        if path is not None
        else os.getenv(
            "STOCK_AGENT_RUN_LOG_PATH",
            "/tmp/stock-agent-runtime-runs.sqlite3",
        )
    )
    return Path(configured).expanduser()


def record_runtime_run(
    record: Mapping[str, Any],
    *,
    path: str | Path | None = None,
    max_rows: int = DEFAULT_MAX_RUN_ROWS,
) -> None:
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    target = runtime_run_log_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_record = dict(record)
    if isinstance(safe_record.get("error"), str):
        safe_record["error"] = _redact_message(safe_record["error"])
    payload = json.dumps(
        safe_record,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )
    with closing(sqlite3.connect(target, timeout=5.0)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                strategy_id TEXT,
                duration_seconds REAL NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO runtime_runs
            (started_at,status,mode,strategy_id,duration_seconds,payload)
            VALUES (?,?,?,?,?,?)
            """,
            (
                str(record["started_at"]),
                str(record["status"]),
                str(record["mode"]),
                record.get("strategy_id"),
                float(record["duration_seconds"]),
                payload,
            ),
        )
        connection.execute(
            """
            DELETE FROM runtime_runs
            WHERE id <= COALESCE((SELECT MAX(id) FROM runtime_runs), 0) - ?
            """,
            (max_rows,),
        )
        connection.commit()


def recent_runtime_runs(
    *,
    path: str | Path | None = None,
    limit: int = 50,
) -> tuple[dict[str, Any], ...]:
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    target = runtime_run_log_path(path)
    if not target.exists():
        return ()
    with closing(sqlite3.connect(target, timeout=5.0)) as connection:
        rows = connection.execute(
            "SELECT payload FROM runtime_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return tuple(json.loads(item[0]) for item in rows)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "DEFAULT_MAX_RUN_ROWS",
    "recent_runtime_runs",
    "record_runtime_run",
    "runtime_run_log_path",
    "utc_now_iso",
]
