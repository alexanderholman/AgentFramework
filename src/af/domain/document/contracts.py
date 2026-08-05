"""Driver-neutral document contracts for AF document operations.

This module defines the types, protocols, and errors that any document
driver must satisfy.  It stays independent of concrete database
implementations so that MongoDB, PostgreSQL JSONB, or other stores can
plug in without changing domain logic.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import (
    Any,
    AsyncIterator,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import asyncio
from dataclasses import dataclass, field
from typing import Protocol


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DocumentFeature(Enum):
    """Capabilities a document driver may support."""

    CRUD = "crud"
    AGGREGATION = "aggregation"
    INDEX = "index"
    SESSION = "session"
    BULK = "bulk"
    TIME_SERIES = "time_series"
    TRANSACTION = "transaction"


class WriteConcern(Enum):
    """Write acknowledgment levels."""

    ACKNOWLEDGED = "acknowledged"
    JOURNALED = "journaled"
    MAJORITY = "majority"


class ReadPreference(Enum):
    """Read routing preferences."""

    PRIMARY = "primary"
    PRIMARY_PREFERRED = "primary_preferred"
    SECONDARY = "secondary"
    SECONDARY_PREFERRED = "secondary_preferred"
    NEAREST = "nearest"


class IndexType(Enum):
    """Supported index types."""

    KEY = "key"
    TEXT = "text"
    GEO_2D = "2d"
    GEO_2SPHERE = "2dsphere"
    HASHED = "hashed"
    TTL = "ttl"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentFilter:
    """Query filter expressed as a mapping of field paths to conditions."""

    criteria: Dict[str, Any] = field(default_factory=dict)

    def merge(self, other: Optional["DocumentFilter"] = None) -> "DocumentFilter":
        """Return a new filter that is the union of both criterion sets."""
        if other is None:
            return self
        merged = dict(self.criteria)
        merged.update(other.criteria)
        return DocumentFilter(criteria=merged)


@dataclass(frozen=True)
class DocumentProjection:
    """Fields to include or exclude from query results."""

    include: Optional[Dict[str, int]] = None
    exclude: Optional[Dict[str, int]] = None


@dataclass(frozen=True)
class AggregationPipeline:
    """Ordered sequence of aggregation stages."""

    stages: List[Dict[str, Any]] = field(default_factory=list)

    def append(self, stage: Dict[str, Any]) -> "AggregationPipeline":
        """Return a new pipeline with an additional stage."""
        new_stages = list(self.stages)
        new_stages.append(stage)
        return AggregationPipeline(stages=new_stages)


@dataclass(frozen=True)
class IndexDefinition:
    """Specification for a document index."""

    fields: List[Tuple[str, IndexType]]
    name: Optional[str] = None
    unique: bool = False
    sparse: bool = False
    expire_after_seconds: Optional[int] = None


@dataclass(frozen=True)
class TimeSeriesOptions:
    """Configuration for a time-series collection."""

    time_field: str
    meta_field: Optional[str] = None
    granularity: str = "hour"
    bucket_max_span_seconds: Optional[int] = None
    bucket_rounding_seconds: Optional[int] = None

    def validate(self) -> List[str]:
        """Return validation errors (empty list if valid)."""
        errors: List[str] = []
        if not self.time_field:
            errors.append("time_field is required")
        allowed = {"second", "minute", "hour"}
        if self.granularity not in allowed:
            errors.append(f"granularity must be one of {allowed}")
        return errors


@dataclass(frozen=True)
class DocumentChange:
    """Represents a single document change event for projection/outbox."""

    collection: str
    operation: str  # "insert", "update", "delete", "replace"
    document_id: Any
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BulkOperation:
    """A single operation within a bulk write batch."""

    operation: str  # "insert", "update", "delete", "replace"
    filter: Optional[DocumentFilter] = None
    document: Optional[Dict[str, Any]] = None
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BulkWriteResult:
    """Summary of a bulk write execution."""

    inserted_count: int = 0
    matched_count: int = 0
    modified_count: int = 0
    deleted_count: int = 0
    upserted_count: int = 0
    write_errors: List[Dict[str, Any]] = field(default_factory=list)
    write_concern_errors: List[Dict[str, Any]] = field(default_factory=list)


class CursorResult:
    """Lazy cursor wrapper for query results."""

    def __init__(self, items: Optional[List[Dict[str, Any]]] = None):
        self._items: List[Dict[str, Any]] = items or []
        self._index: int = 0

    def __aiter__(self) -> AsyncIterator[Dict[str, Any]]:
        return self

    async def __anext__(self) -> Dict[str, Any]:
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item

    @classmethod
    def from_items(cls, items: List[Dict[str, Any]]) -> "CursorResult":
        return cls(items=items)


@dataclass(frozen=True)
class QueryResult:
    """Result of a find/query operation."""

    documents: List[Dict[str, Any]] = field(default_factory=list)
    cursor: Optional[CursorResult] = None
    count: int = 0


@dataclass(frozen=True)
class WriteResult:
    """Result of a write operation."""

    acknowledged: bool = False
    inserted_id: Optional[Any] = None
    inserted_ids: List[Any] = field(default_factory=list)
    matched_count: int = 0
    modified_count: int = 0
    deleted_count: int = 0
    upserted_id: Optional[Any] = None


class SessionContext:
    """Represents an active session for transactions."""

    def __init__(self, session_id: str):
        self.session_id: str = session_id
        self.is_active: bool = True

    async def close(self) -> None:
        self.is_active = False


# ---------------------------------------------------------------------------
# Driver Protocol
# ---------------------------------------------------------------------------


class DocumentDriver(Protocol):
    """Interface that any document store driver must implement."""

    async def insert_one(
        self,
        collection: str,
        document: Dict[str, Any],
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
    ) -> WriteResult: ...

    async def insert_many(
        self,
        collection: str,
        documents: List[Dict[str, Any]],
        ordered: bool = True,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
    ) -> WriteResult: ...

    async def find_one(
        self,
        collection: str,
        filter: Optional[DocumentFilter] = None,
        projection: Optional[DocumentProjection] = None,
        read_preference: ReadPreference = ReadPreference.PRIMARY,
    ) -> Optional[Dict[str, Any]]: ...

    async def find(
        self,
        collection: str,
        filter: Optional[DocumentFilter] = None,
        projection: Optional[DocumentProjection] = None,
        sort: Optional[Dict[str, int]] = None,
        limit: int = 0,
        skip: int = 0,
        read_preference: ReadPreference = ReadPreference.PRIMARY,
    ) -> QueryResult: ...

    async def count_documents(
        self,
        collection: str,
        filter: Optional[DocumentFilter] = None,
    ) -> int: ...

    async def update_one(
        self,
        collection: str,
        filter: DocumentFilter,
        update: Dict[str, Any],
        upsert: bool = False,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
    ) -> WriteResult: ...

    async def update_many(
        self,
        collection: str,
        filter: DocumentFilter,
        update: Dict[str, Any],
        upsert: bool = False,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
    ) -> WriteResult: ...

    async def replace_one(
        self,
        collection: str,
        filter: DocumentFilter,
        replacement: Dict[str, Any],
        upsert: bool = False,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
    ) -> WriteResult: ...

    async def delete_one(
        self,
        collection: str,
        filter: DocumentFilter,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
    ) -> WriteResult: ...

    async def delete_many(
        self,
        collection: str,
        filter: DocumentFilter,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
    ) -> WriteResult: ...

    async def aggregate(
        self,
        collection: str,
        pipeline: AggregationPipeline,
    ) -> List[Dict[str, Any]]: ...

    async def create_index(
        self,
        collection: str,
        definition: IndexDefinition,
    ) -> str: ...

    async def list_indexes(
        self,
        collection: str,
    ) -> List[Dict[str, Any]]: ...

    async def drop_index(
        self,
        collection: str,
        index_name: str,
    ) -> bool: ...

    async def start_session(self) -> SessionContext: ...

    async def bulk_write(
        self,
        collection: str,
        operations: List[BulkOperation],
        ordered: bool = True,
    ) -> BulkWriteResult: ...

    async def create_time_series_collection(
        self,
        name: str,
        options: TimeSeriesOptions,
    ) -> bool: ...

    async def get_time_series_options(
        self,
        collection: str,
    ) -> Optional[TimeSeriesOptions]: ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DocumentOperationError(Exception):
    """Base error for document operations."""

    def __init__(
        self,
        message: str,
        *,
        collection: Optional[str] = None,
        operation: Optional[str] = None,
        code: Optional[int] = None,
    ):
        super().__init__(message)
        self.collection = collection
        self.operation = operation
        self.code = code


class UnsupportedDocumentFeature(DocumentOperationError):
    """Raised when a requested feature is not supported by the driver."""

    def __init__(self, feature: DocumentFeature):
        super().__init__(
            f"Unsupported document feature: {feature.value}",
            operation="capability_check",
        )
        self.feature = feature


class DuplicateKeyError(DocumentOperationError):
    """Raised on unique index violation."""

    def __init__(self, message: str):
        super().__init__(message, code=11000)


class WriteConcernError(DocumentOperationError):
    """Raised when write concern cannot be satisfied."""


class TransactionNotSupportedError(DocumentOperationError):
    """Raised when transaction topology is unsupported."""

    def __init__(self, reason: str = "Single-node MongoDB Community does not support multi-document transactions"):
        super().__init__(reason, operation="transaction")


class DocumentWriteError(DocumentOperationError):
    """Raised on write failures (including duplicate key via code 11000)."""


class DocumentIndexError(DocumentOperationError):
    """Raised on index creation, drop, or query failures."""


class DocumentNotFoundError(DocumentOperationError):
    """Raised when a requested document is not found."""


class DocumentSessionError(DocumentOperationError):
    """Raised on session/transaction errors (codes 43, 10107, 13388)."""


class DocumentTimeSeriesError(DocumentOperationError):
    """Raised on time-series collection operations failure."""


class ValidationWarning(UserWarning):
    """Non-fatal validation notice."""
