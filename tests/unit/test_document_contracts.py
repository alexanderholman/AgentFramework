from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest

from af.domain.document.contracts import (
    AggregationPipeline,
    BulkOperation,
    BulkWriteResult,
    CursorResult,
    DocumentChange,
    DocumentFeature,
    DocumentFilter,
    DocumentIndexError,
    DocumentNotFoundError,
    DocumentOperationError,
    DocumentProjection,
    DocumentSessionError,
    DocumentTimeSeriesError,
    DocumentWriteError,
    DuplicateKeyError,
    IndexDefinition,
    IndexType,
    QueryResult,
    ReadPreference,
    SessionContext,
    TimeSeriesOptions,
    TransactionNotSupportedError,
    UnsupportedDocumentFeature,
    ValidationWarning,
    WriteConcern,
    WriteConcernError,
    WriteResult,
)

# ---------------------------------------------------------------------------
# Enum membership
# ---------------------------------------------------------------------------


def test_document_feature_values() -> None:
    assert DocumentFeature.CRUD in DocumentFeature
    assert DocumentFeature.AGGREGATION in DocumentFeature
    assert DocumentFeature.INDEX in DocumentFeature
    assert DocumentFeature.SESSION in DocumentFeature
    assert DocumentFeature.BULK in DocumentFeature
    assert DocumentFeature.TIME_SERIES in DocumentFeature
    assert DocumentFeature.TRANSACTION in DocumentFeature


def test_document_feature_value_strings() -> None:
    assert DocumentFeature.CRUD.value == "crud"
    assert DocumentFeature.AGGREGATION.value == "aggregation"
    assert DocumentFeature.INDEX.value == "index"
    assert DocumentFeature.SESSION.value == "session"
    assert DocumentFeature.BULK.value == "bulk"
    assert DocumentFeature.TIME_SERIES.value == "time_series"
    assert DocumentFeature.TRANSACTION.value == "transaction"


def test_write_concern_values() -> None:
    assert WriteConcern.ACKNOWLEDGED in WriteConcern
    assert WriteConcern.JOURNALED in WriteConcern
    assert WriteConcern.MAJORITY in WriteConcern


def test_write_concern_value_strings() -> None:
    assert WriteConcern.ACKNOWLEDGED.value == "acknowledged"
    assert WriteConcern.JOURNALED.value == "journaled"
    assert WriteConcern.MAJORITY.value == "majority"


def test_read_preference_values() -> None:
    assert ReadPreference.PRIMARY in ReadPreference
    assert ReadPreference.PRIMARY_PREFERRED in ReadPreference
    assert ReadPreference.SECONDARY in ReadPreference
    assert ReadPreference.SECONDARY_PREFERRED in ReadPreference
    assert ReadPreference.NEAREST in ReadPreference


def test_index_type_values() -> None:
    assert IndexType.KEY in IndexType
    assert IndexType.TEXT in IndexType
    assert IndexType.GEO_2D in IndexType
    assert IndexType.GEO_2SPHERE in IndexType
    assert IndexType.HASHED in IndexType
    assert IndexType.TTL in IndexType


# ---------------------------------------------------------------------------
# DocumentFilter
# ---------------------------------------------------------------------------


def test_document_filter_defaults() -> None:
    f = DocumentFilter()
    assert f.criteria == {}


def test_document_filter_explicit() -> None:
    f = DocumentFilter(criteria={"status": "active", "count": {"$gt": 0}})
    assert f.criteria == {"status": "active", "count": {"$gt": 0}}


def test_document_filter_merge_none() -> None:
    f = DocumentFilter(criteria={"a": 1})
    merged = f.merge(None)
    assert merged.criteria == {"a": 1}
    assert merged is f


def test_document_filter_merge_updates() -> None:
    f1 = DocumentFilter(criteria={"a": 1})
    f2 = DocumentFilter(criteria={"b": 2})
    merged = f1.merge(f2)
    assert merged.criteria == {"a": 1, "b": 2}
    assert f1.criteria == {"a": 1}
    assert f2.criteria == {"b": 2}


def test_document_filter_merge_overwrites() -> None:
    f1 = DocumentFilter(criteria={"a": 1})
    f2 = DocumentFilter(criteria={"a": 2})
    merged = f1.merge(f2)
    assert merged.criteria == {"a": 2}


def test_document_filter_is_frozen() -> None:
    f = DocumentFilter()
    with pytest.raises(FrozenInstanceError):
        f.criteria = {"x": 1}  # type: ignore


# ---------------------------------------------------------------------------
# DocumentProjection
# ---------------------------------------------------------------------------


def test_document_projection_defaults() -> None:
    p = DocumentProjection()
    assert p.include is None
    assert p.exclude is None


def test_document_projection_include() -> None:
    p = DocumentProjection(include={"name": 1, "age": 1})
    assert p.include == {"name": 1, "age": 1}
    assert p.exclude is None


def test_document_projection_exclude() -> None:
    p = DocumentProjection(exclude={"_id": 0})
    assert p.include is None
    assert p.exclude == {"_id": 0}


# ---------------------------------------------------------------------------
# AggregationPipeline
# ---------------------------------------------------------------------------


def test_aggregation_pipeline_defaults() -> None:
    pipe = AggregationPipeline()
    assert pipe.stages == []


def test_aggregation_pipeline_explicit() -> None:
    stages = [{"$match": {"status": "active"}}, {"$group": {"_id": "$type"}}]
    pipe = AggregationPipeline(stages=stages)
    assert pipe.stages == stages


def test_aggregation_pipeline_append() -> None:
    pipe = AggregationPipeline(stages=[{"$match": {"status": "active"}}])
    new_pipe = pipe.append({"$group": {"_id": "$type"}})
    assert len(new_pipe.stages) == 2
    assert new_pipe.stages[1] == {"$group": {"_id": "$type"}}
    assert len(pipe.stages) == 1


# ---------------------------------------------------------------------------
# IndexDefinition
# ---------------------------------------------------------------------------


def test_index_definition_minimal() -> None:
    idx = IndexDefinition(fields=[("name", IndexType.KEY)])
    assert idx.fields == [("name", IndexType.KEY)]
    assert idx.name is None
    assert idx.unique is False
    assert idx.sparse is False
    assert idx.expire_after_seconds is None


def test_index_definition_full() -> None:
    idx = IndexDefinition(
        fields=[("name", IndexType.TEXT), ("location", IndexType.GEO_2SPHERE)],
        name="idx_name_location",
        unique=True,
        sparse=True,
        expire_after_seconds=3600,
    )
    assert len(idx.fields) == 2
    assert idx.name == "idx_name_location"
    assert idx.unique is True
    assert idx.sparse is True
    assert idx.expire_after_seconds == 3600


# ---------------------------------------------------------------------------
# TimeSeriesOptions
# ---------------------------------------------------------------------------


def test_timeseries_options_defaults() -> None:
    ts = TimeSeriesOptions(time_field="timestamp")
    assert ts.time_field == "timestamp"
    assert ts.meta_field is None
    assert ts.granularity == "hour"
    assert ts.bucket_max_span_seconds is None
    assert ts.bucket_rounding_seconds is None


def test_timeseries_options_explicit() -> None:
    ts = TimeSeriesOptions(
        time_field="created_at",
        meta_field="metadata",
        granularity="minute",
        bucket_max_span_seconds=300,
        bucket_rounding_seconds=60,
    )
    assert ts.time_field == "created_at"
    assert ts.meta_field == "metadata"
    assert ts.granularity == "minute"
    assert ts.bucket_max_span_seconds == 300
    assert ts.bucket_rounding_seconds == 60


def test_timeseries_validate_empty_time_field() -> None:
    ts = TimeSeriesOptions(time_field="")
    errors = ts.validate()
    assert "time_field is required" in errors


def test_timeseries_validate_invalid_granularity() -> None:
    ts = TimeSeriesOptions(time_field="ts", granularity="invalid")
    errors = ts.validate()
    assert any("granularity" in e for e in errors)


def test_timeseries_validate_valid() -> None:
    ts = TimeSeriesOptions(time_field="ts", granularity="hour")
    assert ts.validate() == []


def test_timeseries_validate_all_granularities() -> None:
    for g in ("second", "minute", "hour"):
        ts = TimeSeriesOptions(time_field="ts", granularity=g)
        assert ts.validate() == []


# ---------------------------------------------------------------------------
# DocumentChange
# ---------------------------------------------------------------------------


def test_document_change() -> None:
    change = DocumentChange(
        collection="users",
        operation="insert",
        document_id="doc-1",
        metadata={"source": "test"},
    )
    assert change.collection == "users"
    assert change.operation == "insert"
    assert change.document_id == "doc-1"
    assert change.metadata == {"source": "test"}
    assert isinstance(change.timestamp, datetime)


# ---------------------------------------------------------------------------
# BulkOperation
# ---------------------------------------------------------------------------


def test_bulk_operation_insert() -> None:
    op = BulkOperation(operation="insert", document={"name": "test"})
    assert op.operation == "insert"
    assert op.document == {"name": "test"}
    assert op.filter is None
    assert op.options == {}


def test_bulk_operation_update() -> None:
    f = DocumentFilter(criteria={"status": "active"})
    op = BulkOperation(operation="update", filter=f, document={"$set": {"status": "inactive"}})
    assert op.operation == "update"
    assert op.filter is f
    assert op.document == {"$set": {"status": "inactive"}}


# ---------------------------------------------------------------------------
# BulkWriteResult
# ---------------------------------------------------------------------------


def test_bulk_write_result_defaults() -> None:
    result = BulkWriteResult()
    assert result.inserted_count == 0
    assert result.matched_count == 0
    assert result.modified_count == 0
    assert result.deleted_count == 0
    assert result.upserted_count == 0
    assert result.write_errors == []
    assert result.write_concern_errors == []


def test_bulk_write_result_explicit() -> None:
    result = BulkWriteResult(
        inserted_count=5,
        matched_count=3,
        modified_count=2,
        deleted_count=1,
        upserted_count=1,
        write_errors=[{"code": 11000}],
    )
    assert result.inserted_count == 5
    assert result.matched_count == 3
    assert result.modified_count == 2
    assert result.deleted_count == 1
    assert result.upserted_count == 1
    assert len(result.write_errors) == 1


# ---------------------------------------------------------------------------
# CursorResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_result_empty() -> None:
    cursor = CursorResult()
    items = []
    async for item in cursor:
        items.append(item)
    assert items == []


@pytest.mark.asyncio
async def test_cursor_result_items() -> None:
    data = [{"_id": 1}, {"_id": 2}]
    cursor = CursorResult.from_items(data)
    results = []
    async for item in cursor:
        results.append(item)
    assert results == data


@pytest.mark.asyncio
async def test_cursor_result_iteration_order() -> None:
    data = [{"i": i} for i in range(5)]
    cursor = CursorResult.from_items(data)
    results = []
    async for item in cursor:
        results.append(item)
    assert results == data


# ---------------------------------------------------------------------------
# QueryResult
# ---------------------------------------------------------------------------


def test_query_result_defaults() -> None:
    qr = QueryResult()
    assert qr.documents == []
    assert qr.cursor is None
    assert qr.count == 0


def test_query_result_explicit() -> None:
    qr = QueryResult(documents=[{"_id": 1}], count=1)
    assert len(qr.documents) == 1
    assert qr.count == 1


# ---------------------------------------------------------------------------
# WriteResult
# ---------------------------------------------------------------------------


def test_write_result_defaults() -> None:
    wr = WriteResult()
    assert wr.acknowledged is False
    assert wr.inserted_id is None
    assert wr.inserted_ids == []
    assert wr.matched_count == 0
    assert wr.modified_count == 0
    assert wr.deleted_count == 0
    assert wr.upserted_id is None


def test_write_result_explicit() -> None:
    wr = WriteResult(acknowledged=True, inserted_id="doc-1", modified_count=1)
    assert wr.acknowledged is True
    assert wr.inserted_id == "doc-1"
    assert wr.modified_count == 1


# ---------------------------------------------------------------------------
# SessionContext
# ---------------------------------------------------------------------------


def test_session_context_init() -> None:
    ctx = SessionContext("session-1")
    assert ctx.session_id == "session-1"
    assert ctx.is_active is True


@pytest.mark.asyncio
async def test_session_context_close() -> None:
    ctx = SessionContext("session-1")
    await ctx.close()
    assert ctx.is_active is False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


def test_document_operation_error() -> None:
    err = DocumentOperationError(
        "something failed", collection="users", operation="insert", code=500
    )
    assert err.collection == "users"
    assert err.operation == "insert"
    assert err.code == 500
    assert "something failed" in str(err)


def test_document_operation_error_defaults() -> None:
    err = DocumentOperationError("generic error")
    assert err.collection is None
    assert err.operation is None
    assert err.code is None


def test_unsupported_document_feature() -> None:
    err = UnsupportedDocumentFeature(DocumentFeature.AGGREGATION)
    assert err.feature == DocumentFeature.AGGREGATION
    assert "aggregation" in str(err)
    assert err.operation == "capability_check"


def test_duplicate_key_error() -> None:
    err = DuplicateKeyError("duplicate key")
    assert err.code == 11000
    assert "duplicate key" in str(err)


def test_write_concern_error() -> None:
    err = WriteConcernError("write concern timeout")
    assert "write concern timeout" in str(err)


def test_transaction_not_supported_default() -> None:
    err = TransactionNotSupportedError()
    assert "Standalone MongoDB deployments" in str(err)
    assert err.operation == "transaction"


def test_transaction_not_supported_custom() -> None:
    err = TransactionNotSupportedError(reason="custom reason")
    assert "custom reason" in str(err)


def test_document_write_error() -> None:
    err = DocumentWriteError("write failed", collection="users", operation="insert")
    assert err.collection == "users"
    assert err.operation == "insert"


def test_document_index_error() -> None:
    err = DocumentIndexError("index creation failed")
    assert "index creation failed" in str(err)


def test_document_not_found_error() -> None:
    err = DocumentNotFoundError("doc not found")
    assert "doc not found" in str(err)


def test_document_session_error() -> None:
    err = DocumentSessionError("session expired")
    assert "session expired" in str(err)


def test_document_timeseries_error() -> None:
    err = DocumentTimeSeriesError("timeseries creation failed")
    assert "timeseries creation failed" in str(err)


def test_validation_warning() -> None:
    warn = ValidationWarning("non-fatal notice")
    assert "non-fatal notice" in str(warn)


# ---------------------------------------------------------------------------
# Inheritance chain
# ---------------------------------------------------------------------------


def test_error_inheritance() -> None:
    assert issubclass(UnsupportedDocumentFeature, DocumentOperationError)
    assert issubclass(DuplicateKeyError, DocumentOperationError)
    assert issubclass(WriteConcernError, DocumentOperationError)
    assert issubclass(TransactionNotSupportedError, DocumentOperationError)
    assert issubclass(DocumentWriteError, DocumentOperationError)
    assert issubclass(DocumentIndexError, DocumentOperationError)
    assert issubclass(DocumentNotFoundError, DocumentOperationError)
    assert issubclass(DocumentSessionError, DocumentOperationError)
    assert issubclass(DocumentTimeSeriesError, DocumentOperationError)
    assert issubclass(DocumentOperationError, Exception)
    assert issubclass(ValidationWarning, UserWarning)
