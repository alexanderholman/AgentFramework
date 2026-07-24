"""Embedded SQLite FTS5 search driver with policy pushdown."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from threading import RLock
from time import monotonic
from types import TracebackType
from typing import Literal

from af.framework.errors import PlatformError, PlatformReason
from af.search.contracts import ResourceEnvelope, SearchHit, SearchQuery

__all__ = ("EmbeddedSearchDriver", "SearchDriverIneligible")

_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.:-]{0,254}[A-Za-z0-9])?$")
_TOKEN = re.compile(r"\w+", re.UNICODE)
_SUPPORTED_FILTERS = frozenset({"tenant_id", "acl"})


class SearchDriverIneligible(PlatformError):
    """Sanitized failure when embedded search cannot safely serve a request."""


def _failure(
    reason: PlatformReason,
    *,
    stage: str,
    protected_cause: BaseException | None = None,
) -> SearchDriverIneligible:
    return SearchDriverIneligible(reason, stage=stage, protected_cause=protected_cause)


def _identity(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("search identity must be canonical")
    return value


def _expression(text: str) -> str:
    tokens = tuple(_TOKEN.findall(text))
    if not tokens or len(tokens) > 128:
        raise _failure(PlatformReason.VALIDATION_FAILED, stage="search.query")
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


class EmbeddedSearchDriver(AbstractContextManager["EmbeddedSearchDriver"]):
    """Persistent, serialized FTS5 index with tenant and ACL pre-filtering."""

    def __init__(self, database: str | Path) -> None:
        if isinstance(database, Path):
            path = str(database)
        elif type(database) is str and database:
            path = database
        else:
            raise ValueError("embedded search database path must be non-empty")
        try:
            if path != ":memory:":
                Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                path,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS af_search_fts USING fts5(
                    resource_id UNINDEXED,
                    kind UNINDEXED,
                    source_id UNINDEXED,
                    authoritative_locator UNINDEXED,
                    tenant_id UNINDEXED,
                    projection_version UNINDEXED,
                    text,
                    acl_json UNINDEXED,
                    provenance_json UNINDEXED,
                    tokenize = 'unicode61'
                )
                """
            )
        except (OSError, sqlite3.Error) as error:
            raise _failure(
                PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="search.embedded.connect",
                protected_cause=error,
            ) from None
        self._lock = RLock()
        self._closed = False

    def _assert_open(self) -> None:
        if self._closed:
            raise _failure(PlatformReason.SHUTDOWN_FAILED, stage="search.embedded.connection")

    def upsert(self, resources: tuple[ResourceEnvelope, ...], /) -> int:
        """Atomically add or replace canonical resource projections."""

        if (
            type(resources) is not tuple
            or not resources
            or len(resources) > 10_000
            or any(not isinstance(item, ResourceEnvelope) for item in resources)
        ):
            raise ValueError("search resources must be a bounded non-empty tuple")
        identities = tuple(
            (item.tenant_id, item.source_id, item.resource_id) for item in resources
        )
        if len(set(identities)) != len(identities):
            raise ValueError("search resource identities must be unique")
        with self._lock:
            self._assert_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                for item in resources:
                    self._connection.execute(
                        """
                        DELETE FROM af_search_fts
                        WHERE tenant_id = ? AND source_id = ? AND resource_id = ?
                        """,
                        (item.tenant_id, item.source_id, item.resource_id),
                    )
                    self._connection.execute(
                        """
                        INSERT INTO af_search_fts(
                            resource_id,
                            kind,
                            source_id,
                            authoritative_locator,
                            tenant_id,
                            projection_version,
                            text,
                            acl_json,
                            provenance_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item.resource_id,
                            item.kind.value,
                            item.source_id,
                            item.authoritative_locator,
                            item.tenant_id,
                            item.projection_version,
                            item.text,
                            json.dumps(item.acl, separators=(",", ":")),
                            json.dumps(item.provenance, separators=(",", ":")),
                        ),
                    )
                self._connection.commit()
            except sqlite3.Error as error:
                self._connection.rollback()
                raise _failure(
                    PlatformReason.PERMANENT_FAILURE,
                    stage="search.embedded.upsert",
                    protected_cause=error,
                ) from None
        return len(resources)

    def delete(self, *, tenant_id: str, source_id: str, resource_id: str) -> bool:
        """Delete one exact canonical identity as an index tombstone."""

        identity = (_identity(tenant_id), _identity(source_id), _identity(resource_id))
        with self._lock:
            self._assert_open()
            try:
                cursor = self._connection.execute(
                    """
                    DELETE FROM af_search_fts
                    WHERE tenant_id = ? AND source_id = ? AND resource_id = ?
                    """,
                    identity,
                )
            except sqlite3.Error as error:
                raise _failure(
                    PlatformReason.PERMANENT_FAILURE,
                    stage="search.embedded.delete",
                    protected_cause=error,
                ) from None
        return cursor.rowcount > 0

    def search(self, query: SearchQuery, /) -> tuple[SearchHit, ...]:
        """Search only after all mandatory policy predicates are pushed down."""

        if not isinstance(query, SearchQuery):
            raise TypeError("embedded search requires a SearchQuery")
        fields = frozenset(item.field for item in query.filters)
        tenant_ids = query.required_filter("tenant_id")
        acl = query.required_filter("acl")
        if fields != _SUPPORTED_FILTERS or tenant_ids is None or acl is None:
            raise _failure(
                PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="search.embedded.policy-pushdown",
            )
        expression = _expression(query.text)
        tenant_placeholders = ", ".join("?" for _ in tenant_ids)
        acl_placeholders = ", ".join("?" for _ in acl)
        sql = f"""
            SELECT
                resource_id,
                source_id,
                authoritative_locator,
                projection_version,
                acl_json,
                provenance_json,
                bm25(af_search_fts) AS rank_score,
                snippet(af_search_fts, 6, '<em>', '</em>', '…', 16) AS highlight
            FROM af_search_fts
            WHERE af_search_fts MATCH ?
              AND tenant_id IN ({tenant_placeholders})
              AND EXISTS (
                  SELECT 1
                  FROM json_each(acl_json)
                  WHERE value IN ({acl_placeholders})
              )
            ORDER BY rank_score ASC, resource_id ASC, source_id ASC
            LIMIT ?
        """
        deadline = monotonic() + query.deadline_ms / 1_000
        with self._lock:
            self._assert_open()
            self._connection.set_progress_handler(lambda: int(monotonic() >= deadline), 1_000)
            try:
                rows = tuple(
                    self._connection.execute(
                        sql,
                        (expression, *tenant_ids, *acl, query.limit),
                    )
                )
            except sqlite3.Error as error:
                reason = (
                    PlatformReason.CANCELLED
                    if monotonic() >= deadline
                    else PlatformReason.PERMANENT_FAILURE
                )
                raise _failure(
                    reason,
                    stage="search.embedded.query",
                    protected_cause=error,
                ) from None
            finally:
                self._connection.set_progress_handler(None, 0)
        return tuple(self._hits(rows, acl))

    @staticmethod
    def _hits(rows: tuple[tuple[object, ...], ...], requested_acl: tuple[str, ...]) -> Iterator[SearchHit]:
        allowed = frozenset(requested_acl)
        for rank, row in enumerate(rows, start=1):
            stored_acl = tuple(json.loads(str(row[4])))
            evidence = tuple(value for value in stored_acl if value in allowed)
            provenance = tuple(json.loads(str(row[5])))
            score = max(0.0, -float(row[6]))
            highlight = str(row[7])
            yield SearchHit(
                resource_id=str(row[0]),
                source_id=str(row[1]),
                authoritative_locator=str(row[2]),
                projection_version=int(row[3]),
                acl_evidence=evidence,
                provenance=provenance,
                score=score,
                rank=rank,
                highlights=(highlight,) if highlight else (),
            )

    def cancel(self) -> None:
        if not self._closed:
            self._connection.interrupt()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._connection.close()
            except sqlite3.Error as error:
                raise _failure(
                    PlatformReason.SHUTDOWN_FAILED,
                    stage="search.embedded.shutdown",
                    protected_cause=error,
                ) from None
            self._closed = True

    def __enter__(self) -> EmbeddedSearchDriver:
        self._assert_open()
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error_type, error, traceback
        self.close()
        return False
