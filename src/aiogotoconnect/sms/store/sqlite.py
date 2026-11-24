"""Port of store/sqlite.ts (stdlib sqlite3; same schema and SQL)."""

import builtins
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable

from aiogotoconnect.sms.store.types import Json, json_equal, next_claim

_TABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class SqliteStore:
    """SQLite adapter over a path or a caller-owned ``sqlite3.Connection``.

    Every read-modify-write runs in a ``BEGIN IMMEDIATE`` transaction, so it is atomic across
    processes too. Calls are synchronous inside the async methods (nothing awaits), so they are
    also atomic on the event loop. A path is opened here with WAL; a passed connection is left
    as configured by the caller.
    """

    def __init__(
        self,
        db: str | os.PathLike[str] | sqlite3.Connection,
        table: str = "goto_sms_kv",
        now: Callable[[], float] | None = None,
    ) -> None:
        # The table name is interpolated into SQL (it cannot be a bound parameter): restrict it.
        if not _TABLE_NAME.fullmatch(table):
            raise ValueError(f"invalid table name: {table}")
        self._table = table
        self._now = now or (lambda: time.time() * 1000)
        self._owns_conn = not isinstance(db, sqlite3.Connection)
        if isinstance(db, sqlite3.Connection):
            self._conn = db
        else:
            # Autocommit mode: transactions are explicit (see _transaction).
            self._conn = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "tenant TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
            "PRIMARY KEY (tenant, key))"
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the connection if this store opened it; a caller-owned one is left open."""
        if self._owns_conn:
            self._conn.close()

    async def get(self, tenant: str, key: str) -> Json:
        return self._read(tenant, key)

    async def set(self, tenant: str, key: str, value: Json) -> None:
        with self._conn:
            self._write(tenant, key, value)

    async def delete(self, tenant: str, key: str) -> None:
        with self._conn:
            self._conn.execute(
                f"DELETE FROM {self._table} WHERE tenant = ? AND key = ?", (tenant, key)
            )

    async def list(self, tenant: str, prefix: str) -> builtins.list[tuple[str, Json]]:
        # substr compare, not LIKE: prefixes may contain % or _.
        rows = self._conn.execute(
            f"SELECT key, value FROM {self._table} "
            "WHERE tenant = ? AND substr(key, 1, length(?)) = ? ORDER BY key",
            (tenant, prefix, prefix),
        ).fetchall()
        return [(row[0], json.loads(row[1])) for row in rows]

    async def compare_and_set(self, tenant: str, key: str, expected: Json, next: Json) -> bool:
        with self._transaction():
            if not json_equal(self._read(tenant, key), expected):
                return False
            self._write(tenant, key, next)
            return True

    async def claim(self, tenant: str, key: str, ttl_ms: float, owner: str) -> bool:
        with self._transaction():
            new_claim = next_claim(self._read(tenant, key), owner, ttl_ms, self._now())
            if new_claim is None:
                return False
            self._write(tenant, key, new_claim)
            return True

    def _read(self, tenant: str, key: str) -> Json:
        row = self._conn.execute(
            f"SELECT value FROM {self._table} WHERE tenant = ? AND key = ?", (tenant, key)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def _write(self, tenant: str, key: str, value: Json) -> None:
        self._conn.execute(
            f"INSERT INTO {self._table} (tenant, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (tenant, key) DO UPDATE SET value = excluded.value",
            (tenant, key, json.dumps(value)),
        )

    def _transaction(self) -> "_ImmediateTransaction":
        return _ImmediateTransaction(self._conn)


class _ImmediateTransaction:
    """``BEGIN IMMEDIATE`` ... commit, or rollback on error (and on early ``return False``)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> None:
        # A caller-owned connection may already be inside a transaction; join it then.
        self._began = not self._conn.in_transaction
        if self._began:
            self._conn.execute("BEGIN IMMEDIATE")

    def __exit__(self, *exc: object) -> None:
        if not self._began:
            return
        # Committing after a no-op early return is harmless; roll back only on error.
        if exc[0] is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")
