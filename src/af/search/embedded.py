"""Embedded SQLite FTS5 search driver with policy pushdown."""

from __future__ import annotations

import hashlib
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
from uuid import uuid4

from af.framework.errors import PlatformError, PlatformReason
from af.search.contracts import ResourceEnvelope, SearchHit, SearchQuery
from af.search.projection_receipts import (
    SearchProjectionDelivery,
    SearchProjectionTargetCheckpoint,
    SearchTombstone,
)

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


def _generation(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("embedded search generation is invalid")
    return value


def _projection_version(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 9_223_372_036_854_775_807:
        raise ValueError("embedded search projection version is invalid")
    return value


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


def _discard_connection(
    connection: sqlite3.Connection,
    primary: BaseException,
) -> None:
    for cleanup in (connection.rollback, connection.close):
        try:
            cleanup()
        except BaseException as cleanup_error:
            if _control_flow_rank(cleanup_error) > _control_flow_rank(primary):
                raise


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

    def __init__(
        self,
        database: str | Path,
        *,
        physical_name: str | None = None,
        projection_version: int | None = None,
    ) -> None:
        if isinstance(database, Path):
            path = str(database)
        elif type(database) is str and database:
            path = database
        else:
            raise ValueError("embedded search database path must be non-empty")
        if (physical_name is None) != (projection_version is None):
            raise ValueError("embedded search target metadata must be complete")
        resolved_physical_name = None if physical_name is None else _generation(physical_name)
        resolved_projection_version = (
            None if projection_version is None else _projection_version(projection_version)
        )
        connection: sqlite3.Connection | None = None
        target_generation: str | None = None
        target_identity = ":memory:"
        target_path: Path | None = None
        target_file_identity: tuple[int, int] | None = None
        try:
            if path != ":memory:":
                resolved_path = Path(path).expanduser().resolve()
                resolved_path.parent.mkdir(parents=True, exist_ok=True)
                target_path = resolved_path
            connection = sqlite3.connect(
                path,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
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
            if resolved_physical_name is not None and resolved_projection_version is not None:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_search_projection_target (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        physical_name TEXT NOT NULL,
                        generation TEXT NOT NULL,
                        projection_version INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_search_projection_checkpoint (
                        projection_name TEXT NOT NULL,
                        source_name TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        position INTEGER NOT NULL CHECK (
                            position >= 0 AND position <= 9223372036854775807
                        ),
                        PRIMARY KEY (projection_name, source_name)
                    )
                    """
                )
                stored = tuple(
                    connection.execute(
                        """
                        SELECT physical_name, generation, projection_version
                        FROM af_search_projection_target
                        WHERE singleton = 1
                        """
                    )
                )
                if not stored:
                    indexed = tuple(connection.execute("SELECT COUNT(*) FROM af_search_fts"))
                    if indexed != ((0,),):
                        raise _failure(
                            PlatformReason.INTEGRITY_FAILURE,
                            stage="search.embedded.generation",
                        )
                    target_generation = uuid4().hex
                    connection.execute(
                        """
                        INSERT INTO af_search_projection_target (
                            singleton,
                            physical_name,
                            generation,
                            projection_version
                        ) VALUES (1, ?, ?, ?)
                        """,
                        (
                            resolved_physical_name,
                            target_generation,
                            resolved_projection_version,
                        ),
                    )
                elif len(stored) == 1:
                    stored_name, stored_generation, stored_version = stored[0]
                    try:
                        canonical_name = _generation(stored_name)
                        target_generation = _generation(stored_generation)
                        canonical_version = _projection_version(stored_version)
                    except ValueError as error:
                        raise _failure(
                            PlatformReason.INTEGRITY_FAILURE,
                            stage="search.embedded.generation",
                            protected_cause=error,
                        ) from None
                    if (
                        canonical_name != resolved_physical_name
                        or canonical_version != resolved_projection_version
                    ):
                        raise _failure(
                            PlatformReason.INTEGRITY_FAILURE,
                            stage="search.embedded.generation",
                        )
                else:
                    raise _failure(
                        PlatformReason.INTEGRITY_FAILURE,
                        stage="search.embedded.generation",
                    )
                connection.commit()
                if target_path is not None:
                    target_stat = target_path.stat()
                    target_file_identity = (target_stat.st_dev, target_stat.st_ino)
                    target_identity = f"{target_file_identity[0]}:{target_file_identity[1]}"
        except BaseException as error:
            if connection is not None:
                _discard_connection(connection, error)
            if isinstance(error, PlatformError):
                raise
            if isinstance(error, (OSError, sqlite3.Error)):
                raise _failure(
                    PlatformReason.CAPABILITY_UNAVAILABLE,
                    stage="search.embedded.connect",
                    protected_cause=error,
                ) from None
            _raise_operation_error(error, stage="search.embedded.connect")
        assert connection is not None
        if target_generation is not None:
            target_generation = hashlib.sha256(
                f"{target_generation}\0{target_identity}".encode()
            ).hexdigest()
        self._connection = connection
        self._lock = RLock()
        self._closed = False
        self._closing = False
        self._poisoned = False
        self._physical_name = resolved_physical_name
        self._generation = target_generation
        self._projection_version = resolved_projection_version
        self._target_path = target_path
        self._target_file_identity = target_file_identity

    @property
    def physical_name(self) -> str:
        """Return the durable physical target name stored in this index."""

        if self._physical_name is None:
            raise _failure(
                PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="search.embedded.generation",
            )
        return self._physical_name

    @property
    def generation(self) -> str:
        """Return the immutable physical store identity used by projection checkpoints."""

        if self._generation is None:
            raise _failure(
                PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="search.embedded.generation",
            )
        return self._generation

    @property
    def projection_version(self) -> int:
        """Return the projection schema version stored in this index."""

        if self._projection_version is None:
            raise _failure(
                PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="search.embedded.generation",
            )
        return self._projection_version

    def projection_checkpoint(
        self,
        *,
        projection_name: str,
        source_name: str,
    ) -> SearchProjectionTargetCheckpoint | None:
        """Read target-local evidence committed atomically with a projection mutation."""

        projection_name = _generation(projection_name)
        source_name = _generation(source_name)
        with self._lock:
            self._assert_projection_target()
            try:
                return self._load_projection_checkpoint(projection_name, source_name)
            except BaseException as error:
                _raise_operation_error(
                    error,
                    stage="search.embedded.projection.checkpoint",
                )

    def apply_projection(
        self,
        delivery: SearchProjectionDelivery,
        /,
        *,
        projection_name: str,
        expected: SearchProjectionTargetCheckpoint | None,
    ) -> bool:
        """Atomically apply one mutation and its target-local replay checkpoint."""

        if type(delivery) is not SearchProjectionDelivery:
            raise TypeError("embedded projection requires a canonical delivery")
        projection_name = _generation(projection_name)
        if expected is not None and type(expected) is not SearchProjectionTargetCheckpoint:
            raise TypeError("embedded projection expected checkpoint is invalid")
        candidate = SearchProjectionTargetCheckpoint(
            message_id=delivery.message_id,
            position=delivery.position,
        )
        with self._lock:
            self._assert_projection_target()
            if delivery.mutation.projection_version != self._projection_version:
                raise ValueError("embedded projection version does not match target")
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                current = self._load_projection_checkpoint(
                    projection_name,
                    delivery.source_name,
                )
                if current == candidate:
                    self._connection.commit()
                    return False
                if current != expected:
                    reason = self._checkpoint_mismatch_reason(
                        current=current,
                        expected=expected,
                        candidate=candidate,
                    )
                    raise _failure(
                        reason,
                        stage="search.embedded.projection.checkpoint",
                    )
                if isinstance(delivery.mutation, ResourceEnvelope):
                    self._upsert_resource(delivery.mutation)
                else:
                    self._delete_identity(delivery.mutation)
                if expected is None:
                    self._connection.execute(
                        """
                        INSERT INTO af_search_projection_checkpoint (
                            projection_name,
                            source_name,
                            message_id,
                            position
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            projection_name,
                            delivery.source_name,
                            candidate.message_id,
                            candidate.position,
                        ),
                    )
                else:
                    cursor = self._connection.execute(
                        """
                        UPDATE af_search_projection_checkpoint
                        SET message_id = ?, position = ?
                        WHERE projection_name = ?
                          AND source_name = ?
                          AND message_id = ?
                          AND position = ?
                        """,
                        (
                            candidate.message_id,
                            candidate.position,
                            projection_name,
                            delivery.source_name,
                            expected.message_id,
                            expected.position,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise _failure(
                            PlatformReason.INTEGRITY_FAILURE,
                            stage="search.embedded.projection.checkpoint",
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
                            stage="search.embedded.projection.rollback",
                        )
                    if _control_flow_rank(error):
                        _raise_operation_error(
                            error,
                            stage="search.embedded.projection",
                        )
                    _raise_operation_error(
                        rollback_error,
                        stage="search.embedded.projection.rollback",
                    )
                _raise_operation_error(error, stage="search.embedded.projection")
        return True

    def _assert_projection_target(self) -> None:
        self._assert_open()
        if (
            self._physical_name is None
            or self._generation is None
            or self._projection_version is None
        ):
            raise _failure(
                PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="search.embedded.projection",
            )

    def _load_projection_checkpoint(
        self,
        projection_name: str,
        source_name: str,
    ) -> SearchProjectionTargetCheckpoint | None:
        rows = tuple(
            self._connection.execute(
                """
                SELECT message_id, position
                FROM af_search_projection_checkpoint
                WHERE projection_name = ? AND source_name = ?
                """,
                (projection_name, source_name),
            )
        )
        if not rows:
            return None
        if len(rows) != 1 or len(rows[0]) != 2:
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.embedded.projection.checkpoint",
            )
        try:
            return SearchProjectionTargetCheckpoint(
                message_id=rows[0][0],
                position=rows[0][1],
            )
        except ValueError as error:
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.embedded.projection.checkpoint",
                protected_cause=error,
            ) from None

    @staticmethod
    def _checkpoint_mismatch_reason(
        *,
        current: SearchProjectionTargetCheckpoint | None,
        expected: SearchProjectionTargetCheckpoint | None,
        candidate: SearchProjectionTargetCheckpoint,
    ) -> PlatformReason:
        if (
            current is not None
            and current.position == candidate.position
            and current.message_id != candidate.message_id
        ):
            return PlatformReason.INTEGRITY_FAILURE
        if current is not None and (expected is None or current.position > expected.position):
            return PlatformReason.TRANSIENT_FAILURE
        return PlatformReason.INTEGRITY_FAILURE

    def _shutdown_started(self) -> bool:
        return self._closed or self._closing or self._poisoned

    def _assert_open(self) -> None:
        if self._shutdown_started():
            raise _failure(PlatformReason.SHUTDOWN_FAILED, stage="search.embedded.connection")
        if self._target_path is not None and self._target_file_identity is not None:
            try:
                current = self._target_path.stat()
            except OSError as error:
                self._poisoned = True
                raise _failure(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="search.embedded.connection.identity",
                    protected_cause=error,
                ) from None
            if (current.st_dev, current.st_ino) != self._target_file_identity:
                self._poisoned = True
                raise _failure(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="search.embedded.connection.identity",
                )

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
                    self._upsert_resource(item)
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

    def _upsert_resource(self, resource: ResourceEnvelope) -> None:
        self._connection.execute(
            """
            DELETE FROM af_search_fts
            WHERE tenant_id = ? AND source_id = ? AND resource_id = ?
            """,
            (resource.tenant_id, resource.source_id, resource.resource_id),
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
                resource.resource_id,
                resource.kind.value,
                resource.source_id,
                resource.authoritative_locator,
                resource.tenant_id,
                resource.projection_version,
                resource.text,
                json.dumps(resource.acl, separators=(",", ":")),
                json.dumps(resource.provenance, separators=(",", ":")),
            ),
        )

    def _delete_identity(self, tombstone: SearchTombstone) -> bool:
        identity = tombstone.identity
        return self._delete_values(
            identity.tenant_id,
            identity.source_id,
            identity.resource_id,
        )

    def _delete_values(self, tenant_id: str, source_id: str, resource_id: str) -> bool:
        cursor = self._connection.execute(
            """
            DELETE FROM af_search_fts
            WHERE tenant_id = ? AND source_id = ? AND resource_id = ?
            """,
            (tenant_id, source_id, resource_id),
        )
        return cursor.rowcount > 0

    def delete(self, *, tenant_id: str, source_id: str, resource_id: str) -> bool:
        """Delete one exact canonical identity as an index tombstone."""

        identity = (_identity(tenant_id), _identity(source_id), _identity(resource_id))
        with self._lock:
            self._assert_open()
            try:
                deleted = self._delete_values(*identity)
            except BaseException as error:
                _raise_operation_error(error, stage="search.embedded.delete")
        return deleted

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
            if isinstance(error, sqlite3.Error) and self._shutdown_started():
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
