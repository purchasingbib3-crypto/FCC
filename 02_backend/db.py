from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

settings = get_settings()
pool = ConnectionPool(
    conninfo=settings.database_url,
    min_size=1,
    max_size=12,
    timeout=30,
    kwargs={"row_factory": dict_row, "autocommit": False},
    open=False,
)


def open_pool() -> None:
    if pool.closed:
        pool.open(wait=True)


def close_pool() -> None:
    if not pool.closed:
        pool.close()


@contextmanager
def connection() -> Iterator[Connection[Any]]:
    with pool.connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def fetch_all(query: str | sql.Composed, params: Sequence[Any] | dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return list(cur.fetchall())


def fetch_one(query: str | sql.Composed, params: Sequence[Any] | dict[str, Any] | None = None) -> dict[str, Any] | None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return dict(row) if row else None


def execute(query: str | sql.Composed, params: Sequence[Any] | dict[str, Any] | None = None) -> int:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.rowcount


def qualified(name: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(settings.database_schema), sql.Identifier(name))
