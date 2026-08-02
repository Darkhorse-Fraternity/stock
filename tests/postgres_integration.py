"""PostgreSQL integration helpers with fail-closed schema isolation."""

from __future__ import annotations

import hashlib
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


def non_test_database_fingerprint(database_url: str) -> tuple[tuple, ...]:
    """Hash every user table definition and row multiset without returning data."""

    psycopg, sql = _psycopg()
    result = []
    with psycopg.connect(database_url) as connection:
        tables = connection.execute(
            r"""
            SELECT n.nspname, c.relname
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p')
              AND n.nspname <> 'information_schema'
              AND n.nspname !~ '^pg_'
              AND n.nspname !~ '^stock_agent_test_'
            ORDER BY n.nspname, c.relname
            """
        ).fetchall()
        for schema_name, table_name in tables:
            definition_rows = connection.execute(
                r"""
                SELECT 'column', a.attnum::text, a.attname,
                       pg_catalog.format_type(a.atttypid, a.atttypmod),
                       a.attnotnull::text, a.attidentity, a.attgenerated,
                       COALESCE(pg_get_expr(d.adbin, d.adrelid), '')
                FROM pg_attribute AS a
                JOIN pg_class AS c ON c.oid = a.attrelid
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                LEFT JOIN pg_attrdef AS d
                  ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                WHERE n.nspname = %s AND c.relname = %s
                  AND a.attnum > 0 AND NOT a.attisdropped
                UNION ALL
                SELECT 'constraint', con.oid::text, con.conname,
                       pg_get_constraintdef(con.oid, true), '', '', '', ''
                FROM pg_constraint AS con
                JOIN pg_class AS c ON c.oid = con.conrelid
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s
                UNION ALL
                SELECT 'index', i.indexrelid::text, ci.relname,
                       pg_get_indexdef(i.indexrelid), '', '', '', ''
                FROM pg_index AS i
                JOIN pg_class AS c ON c.oid = i.indrelid
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                JOIN pg_class AS ci ON ci.oid = i.indexrelid
                WHERE n.nspname = %s AND c.relname = %s
                ORDER BY 1, 2, 3
                """,
                (
                    schema_name,
                    table_name,
                    schema_name,
                    table_name,
                    schema_name,
                    table_name,
                ),
            ).fetchall()
            definition_material = repr(tuple(definition_rows)).encode("utf-8")
            definition_hash = hashlib.sha256(definition_material).hexdigest()
            row_count, row_hash = connection.execute(
                sql.SQL(
                    "SELECT COUNT(*), md5(COALESCE(string_agg(row_digest, '' "
                    "ORDER BY row_digest), '')) FROM (SELECT md5(row_to_json(t)::text) "
                    "AS row_digest FROM {}.{} AS t) AS rows"
                ).format(sql.Identifier(schema_name), sql.Identifier(table_name))
            ).fetchone()
            result.append(
                (
                    str(schema_name),
                    str(table_name),
                    definition_hash,
                    int(row_count),
                    str(row_hash),
                )
            )
    return tuple(result)


def test_schema_count(database_url: str) -> int:
    psycopg, _sql = _psycopg()
    with psycopg.connect(database_url) as connection:
        return int(
            connection.execute(
                r"""
                SELECT COUNT(*) FROM pg_namespace
                WHERE nspname ~ '^stock_agent_test_'
                """
            ).fetchone()[0]
        )


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
