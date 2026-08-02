"""PostgreSQL integration helpers with fail-closed schema isolation."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager


_TEST_SCHEMA_PATTERN = re.compile(r"\Astock_agent_test_[0-9a-f]{32}\Z")


def _psycopg():
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:  # pragma: no cover - exercised by the explicit skip
        raise RuntimeError(
            "PostgreSQL integration tests require the 'integration' extra"
        ) from exc
    return psycopg, sql


def require_isolated_schema_name(schema: str) -> str:
    """Reject every identifier outside our generated disposable namespace."""

    if type(schema) is not str or _TEST_SCHEMA_PATTERN.fullmatch(schema) is None:
        raise ValueError(
            "integration schema must match stock_agent_test_<32 lowercase hex chars>"
        )
    return schema


def schema_exists(database_url: str, schema: str) -> bool:
    require_isolated_schema_name(schema)
    psycopg, _sql = _psycopg()
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)",
                (schema,),
            )
            return bool(cursor.fetchone()[0])


def non_test_schemas(database_url: str) -> tuple[str, ...]:
    """Snapshot pre-existing schemas without reading any business tables."""

    psycopg, _sql = _psycopg()
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT nspname
                FROM pg_namespace
                WHERE nspname NOT LIKE 'stock_agent_test\\_%' ESCAPE '\\'
                ORDER BY nspname
                """
            )
            return tuple(str(row[0]) for row in cursor.fetchall())


@contextmanager
def isolated_postgres_schema(database_url: str) -> Iterator[str]:
    """Create a unique schema and unconditionally remove only that schema."""

    psycopg, sql = _psycopg()
    schema = require_isolated_schema_name(f"stock_agent_test_{uuid.uuid4().hex}")
    with psycopg.connect(database_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield schema
    finally:
        # A new connection makes cleanup independent of a store/client connection
        # being closed or terminated inside the test body.
        with psycopg.connect(database_url, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )
