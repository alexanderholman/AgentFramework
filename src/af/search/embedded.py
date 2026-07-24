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
from typing import Literal, Never, cast

from af.framework.errors import PlatformError, PlatformReason
from af.search.contracts import ResourceEnvelope, SearchHit, SearchQuery

__all__ = ("EmbeddedSearchDriver", "SearchDriverIneligible")

_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.:-]{0,254}[A-Za-z0-9])?$")
_TOKEN = re.compile(r"\w+", re.UNICODE)
_HIGHLIGHT_WHITESPACE = re.compile(r"[\t\r\n]+")
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


def _operation_reason(error: sqlite3.Error) -> PlatformReason:
    if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
        return PlatformReason.CANCELLED
    return PlatformReason.PERMANENT_FAILURE


def _control_flow_rank(error: BaseException) -> int:
    if not isinstance(error, Exception):
        return 3
    if isinstance(error, sqlite3.Error) and _operation_reason(error) is PlatformReason.CANCELLED:
        return 2
    if isinstance(error, PlatformError):
        if error.reason is PlatformReason.CANCELLED:
            return 2
        if error.reason is PlatformReason.SHUTDOWN_FAILED:
            return 1
    return 0


def _raise_operation_error(error: BaseException, *, stage: str) -> Never:
    if isinstance(error, PlatformError):
        raise error from None
    if isinstance(error, sqlite3.Error):
        raise _failure(
            _operation_reason(error),
            stage=stage,
            protected_cause=error,
        ) from None
    if isinstance(error, Exception):
        raise _failure(
            PlatformReason.PERMANENT_FAILURE,
            stage=stage,
            protected_cause=error,
        ) from None
    raise error from None


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
        self._closing = False
        self._poisoned = False

    def _shutdown_started(self) -> bool:
        return self._closed or self._closing or self._poisoned

    def _assert_open(self) -> None:
        if self._shutdown_started():
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
        identities = tuple((item.tenant_id, item.source_id, item.resource_id) for item in resources)
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
            except BaseException as error:
                try:
                    self._connection.rollback()
                except BaseException as rollback_error:
                    self._poisoned = True
                    if _control_flow_rank(rollback_error) > _control_flow_rank(error):
                        _raise_operation_error(
                            rollback_error,
                            stage="search.embedded.upsert.rollback",
                        )
                    if _control_flow_rank(error):
                        _raise_operation_error(
                            error,
                            stage="search.embedded.upsert",
                        )
                    _raise_operation_error(
                        rollback_error,
                        stage="search.embedded.upsert.rollback",
                    )
                _raise_operation_error(error, stage="search.embedded.upsert")
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
            except BaseException as error:
                _raise_operation_error(error, stage="search.embedded.delete")
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
                tenant_id,
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
            ORDER BY rank_score ASC, resource_id ASC, source_id ASC, tenant_id ASC
            LIMIT ?
        """
        deadline = monotonic() + query.deadline_ms / 1_000
        with self._lock:
            self._assert_open()
            try:
                self._connection.set_progress_handler(
                    lambda: int(monotonic() >= deadline),
                    1_000,
                )
            except BaseException as error:
                self._poisoned = True
                _raise_operation_error(
                    error,
                    stage="search.embedded.query.progress-setup",
                )
            rows: tuple[tuple[object, ...], ...] = ()
            operation_error: BaseException | None = None
            try:
                rows = tuple(
                    self._connection.execute(
                        sql,
                        (expression, *tenant_ids, *acl, query.limit),
                    )
                )
            except BaseException as error:
                operation_error = error
            try:
                self._connection.set_progress_handler(None, 0)
            except BaseException as cleanup_error:
                self._poisoned = True
                if operation_error is None or _control_flow_rank(
                    cleanup_error
                ) > _control_flow_rank(operation_error):
                    _raise_operation_error(
                        cleanup_error,
                        stage="search.embedded.query.progress-cleanup",
                    )
            if operation_error is not None:
                _raise_operation_error(
                    operation_error,
                    stage="search.embedded.query",
                )
        return tuple(self._hits(rows, acl))

    @staticmethod
    def _hits(
        rows: tuple[tuple[object, ...], ...], requested_acl: tuple[str, ...]
    ) -> Iterator[SearchHit]:
        allowed = frozenset(requested_acl)
        for rank, row in enumerate(rows, start=1):
            stored_acl = tuple(json.loads(str(row[5])))
            evidence = tuple(value for value in stored_acl if value in allowed)
            provenance = tuple(json.loads(str(row[6])))
            score = max(0.0, -float(cast(float, row[7])))
            highlight = _HIGHLIGHT_WHITESPACE.sub(" ", str(row[8]))
            yield SearchHit(
                resource_id=str(row[0]),
                source_id=str(row[1]),
                authoritative_locator=str(row[2]),
                tenant_id=str(row[3]),
                projection_version=cast(int, row[4]),
                acl_evidence=evidence,
                provenance=provenance,
                score=score,
                rank=rank,
                highlights=(highlight,) if highlight else (),
            )

    def cancel(self) -> None:
        if self._shutdown_started():
            return
        try:
            self._connection.interrupt()
        except BaseException as error:
            if self._shutdown_started():
                return
            _raise_operation_error(error, stage="search.embedded.cancel")

    def close(self) -> None:
        with self._lock:
            if self._closed or self._closing:
                return
            self._closing = True
            try:
                self._connection.close()
            except BaseException as error:
                self._poisoned = True
                if not isinstance(error, Exception) or (
                    isinstance(error, PlatformError) and _control_flow_rank(error)
                ):
                    raise
                raise _failure(
                    PlatformReason.SHUTDOWN_FAILED,
                    stage="search.embedded.shutdown",
                    protected_cause=error,
                ) from None
            else:
                self._closed = True
                self._poisoned = False
            finally:
                self._closing = False

    def __enter__(self) -> EmbeddedSearchDriver:
        self._assert_open()
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error_type, traceback
        try:
            self.close()
        except BaseException as close_error:
            if error is not None and _control_flow_rank(close_error) <= _control_flow_rank(error):
                return False
            raise
        return False
