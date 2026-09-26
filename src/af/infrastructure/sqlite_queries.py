"""Restart-safe operator-managed query allowlist; requests cannot register queries."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock
from types import TracebackType

from af.framework.persisted_queries import PersistedQuery, validate_digest


class SQLitePersistedQueryRegistry:
    def __init__(self, path: str | Path, *, max_queries: int = 1_000) -> None:
        if type(max_queries) is not int or not 1 <= max_queries <= 100_000:
            raise ValueError("invalid persisted query capacity")
        self._max_queries = max_queries
        self._lock = RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
        with self._connection:
            self._connection.execute("""CREATE TABLE IF NOT EXISTS af_persisted_queries (
                digest TEXT NOT NULL, operation_name TEXT NOT NULL,
                tenant_id TEXT NOT NULL, policy_version TEXT NOT NULL,
                schema_digest TEXT NOT NULL, document TEXT NOT NULL,
                PRIMARY KEY (digest, operation_name, tenant_id, policy_version))""")

    def approve(self, query: PersistedQuery) -> None:
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            previous = self.resolve(
                query.digest,
                operation_name=query.operation_name,
                tenant_id=query.tenant_id,
                policy_version=query.policy_version,
            )
            if previous is not None:
                if previous != query:
                    raise ValueError("persisted approval differs; revoke it before replacing")
                return
            count = self._connection.execute(
                "SELECT COUNT(*) FROM af_persisted_queries"
            ).fetchone()[0]
            if count >= self._max_queries:
                raise ValueError("persisted query registry is full")
            self._connection.execute(
                "INSERT INTO af_persisted_queries VALUES (?, ?, ?, ?, ?, ?)",
                (
                    query.digest,
                    query.operation_name or "",
                    query.tenant_id or "",
                    query.policy_version,
                    query.schema_digest,
                    query.document,
                ),
            )

    def resolve(
        self,
        digest: str,
        *,
        operation_name: str | None = None,
        tenant_id: str | None = None,
        policy_version: str = "1",
    ) -> PersistedQuery | None:
        validate_digest(digest)
        with self._lock:
            row = self._connection.execute(
                "SELECT document, schema_digest FROM af_persisted_queries "
                "WHERE digest=? AND operation_name=? AND tenant_id=? AND policy_version=?",
                (digest, operation_name or "", tenant_id or "", policy_version),
            ).fetchone()
        if row is None:
            return None
        query = PersistedQuery(row[0], row[1], operation_name, tenant_id, policy_version)
        if query.digest != digest:
            raise ValueError("persisted query integrity check failed")
        return query

    def revoke(
        self,
        digest: str,
        *,
        operation_name: str | None = None,
        tenant_id: str | None = None,
        policy_version: str = "1",
    ) -> None:
        validate_digest(digest)
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM af_persisted_queries "
                "WHERE digest=? AND operation_name=? AND tenant_id=? AND policy_version=?",
                (digest, operation_name or "", tenant_id or "", policy_version),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLitePersistedQueryRegistry:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
