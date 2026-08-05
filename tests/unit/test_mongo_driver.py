from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId

from af.domain.document.contracts import (
    AggregationPipeline,
    BulkOperation,
    DocumentFeature,
    DocumentFilter,
    DocumentWriteError,
    IndexDefinition,
    IndexType,
    TimeSeriesOptions,
    TransactionNotSupportedError,
    UnsupportedDocumentFeature,
    WriteConcern,
)
from af.infrastructure.mongo.driver import MongoDBDriver

# ---------------------------------------------------------------------------
# Fixtures – patch MongoClient so the driver never touches a real database
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
    """Return a MagicMock that stands in for pymongo.MongoClient.

    The fixture also builds the nested ``db`` and ``collection`` mocks that
    the driver will reach through ``client[db][collection]``.
    """
    mock_client = MagicMock()
    mock_db = mock_client.return_value.__getitem__.return_value
    mock_collection = mock_db.__getitem__.return_value

    # Default return values
    mock_collection.find.return_value = []
    mock_collection.find_one.return_value = None
    mock_collection.count_documents.return_value = 0
    mock_collection.aggregate.return_value = []
    mock_collection.list_indexes.return_value = []
    mock_collection.index_information.return_value = {}
    mock_collection.create_index.return_value = "idx_name"

    return mock_client, mock_db, mock_collection


@pytest.fixture
def driver(mock_client):
    """Create a MongoDBDriver whose internal MongoClient is mocked."""
    client_mock, _db_mock, _col_mock = mock_client
    with patch("af.infrastructure.mongo.driver.MongoClient", client_mock):
        drv = MongoDBDriver(url="mongodb://localhost:27017", database="test_db")
        yield drv


# ---------------------------------------------------------------------------
# Capability tests (no DB interaction – __new__ is fine)
# ---------------------------------------------------------------------------


def test_features_property_returns_frozenset() -> None:
    driver = MongoDBDriver.__new__(MongoDBDriver)
    driver._features = frozenset({DocumentFeature.CRUD})
    features = driver.features
    assert isinstance(features, frozenset)


def test_supports_returns_true_for_supported_feature() -> None:
    driver = MongoDBDriver.__new__(MongoDBDriver)
    driver._features = frozenset({DocumentFeature.CRUD})
    assert driver.supports(DocumentFeature.CRUD) is True


def test_supports_returns_false_for_missing_feature() -> None:
    driver = MongoDBDriver.__new__(MongoDBDriver)
    driver._features = frozenset({DocumentFeature.CRUD})
    assert driver.supports(DocumentFeature.TRANSACTION) is False


def test_require_raises_transaction_not_supported_for_transaction_feature() -> None:
    driver = MongoDBDriver.__new__(MongoDBDriver)
    driver._features = frozenset({DocumentFeature.CRUD})
    with pytest.raises(TransactionNotSupportedError):
        driver.require(DocumentFeature.TRANSACTION)


def test_require_raises_for_unsupported_non_transaction_feature() -> None:
    driver = MongoDBDriver.__new__(MongoDBDriver)
    driver._features = frozenset({DocumentFeature.CRUD})
    with pytest.raises(UnsupportedDocumentFeature):
        driver.require(DocumentFeature.TIME_SERIES)


def test_require_no_raise_for_supported_feature() -> None:
    driver = MongoDBDriver.__new__(MongoDBDriver)
    driver._features = frozenset({DocumentFeature.CRUD})
    driver.require(DocumentFeature.CRUD)


# ---------------------------------------------------------------------------
# Read tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_returns_documents(driver, mock_client) -> None:
    _, _, collection = mock_client
    expected_docs = [{"_id": "doc1", "name": "test"}]
    collection.find.return_value = iter(expected_docs)

    result = await driver.find("test_collection", DocumentFilter())
    collection.find.assert_called_once()
    assert len(result.documents) == 1


@pytest.mark.asyncio
async def test_find_with_filter_applies_criteria(driver, mock_client) -> None:
    _, _, collection = mock_client
    collection.find.return_value = iter([])

    filter_obj = DocumentFilter(criteria={"status": "active"})
    await driver.find("test_collection", filter_obj)
    collection.find.assert_called_once()


@pytest.mark.asyncio
async def test_find_with_limit_and_skip(driver, mock_client) -> None:
    _, _, collection = mock_client
    collection.find.return_value = iter([])

    await driver.find("test_collection", DocumentFilter(), limit=10, skip=5)
    collection.find.assert_called_once()


@pytest.mark.asyncio
async def test_find_with_sort(driver, mock_client) -> None:
    _, _, collection = mock_client
    collection.find.return_value = iter([])

    await driver.find("test_collection", DocumentFilter(), sort={"name": 1})
    collection.find.assert_called_once()


@pytest.mark.asyncio
async def test_find_one_returns_document(driver, mock_client) -> None:
    _, _, collection = mock_client
    expected_doc = {"_id": "doc1", "name": "test"}
    collection.find_one.return_value = expected_doc

    result = await driver.find_one("test_collection", DocumentFilter())
    collection.find_one.assert_called_once()
    assert result == expected_doc


@pytest.mark.asyncio
async def test_find_one_not_found_returns_none(driver, mock_client) -> None:
    _, _, collection = mock_client
    collection.find_one.return_value = None

    result = await driver.find_one("test_collection", DocumentFilter())
    assert result is None


@pytest.mark.asyncio
async def test_count_returns_int(driver, mock_client) -> None:
    _, _, collection = mock_client
    collection.count_documents.return_value = 42

    result = await driver.count_documents("test_collection", DocumentFilter())
    assert result == 42


# ---------------------------------------------------------------------------
# Write tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_one_returns_id(driver, mock_client) -> None:
    _, _, collection = mock_client
    mock_result = MagicMock()
    mock_result.inserted_id = "new_id"
    collection.insert_one.return_value = mock_result

    result = await driver.insert_one("test_collection", {"name": "test"})
    collection.insert_one.assert_called_once()
    assert result.inserted_id == "new_id"


@pytest.mark.asyncio
async def test_insert_many_returns_ids(driver, mock_client) -> None:
    _, _, collection = mock_client
    mock_result = MagicMock()
    mock_result.inserted_ids = ["id1", "id2"]
    collection.insert_many.return_value = mock_result

    docs = [{"name": "test1"}, {"name": "test2"}]
    result = await driver.insert_many("test_collection", docs)
    collection.insert_many.assert_called_once()
    assert result.inserted_ids == ["id1", "id2"]


@pytest.mark.asyncio
async def test_insert_with_write_concern(driver, mock_client) -> None:
    _, _, collection = mock_client
    mock_result = MagicMock()
    mock_result.inserted_id = "new_id"
    collection.insert_one.return_value = mock_result

    await driver.insert_one(
        "test_collection", {"name": "test"}, write_concern=WriteConcern.MAJORITY
    )
    collection.insert_one.assert_called_once()


@pytest.mark.asyncio
async def test_update_one(driver, mock_client) -> None:
    _, _, collection = mock_client
    mock_result = MagicMock()
    mock_result.matched_count = 1
    mock_result.modified_count = 1
    mock_result.upserted_id = None
    collection.update_one.return_value = mock_result

    filter_obj = DocumentFilter(criteria={"_id": "doc1"})
    await driver.update_one("test_collection", filter_obj, {"$set": {"name": "updated"}})
    collection.update_one.assert_called_once()


@pytest.mark.asyncio
async def test_update_many(driver, mock_client) -> None:
    _, _, collection = mock_client
    mock_result = MagicMock()
    mock_result.matched_count = 2
    mock_result.modified_count = 2
    collection.update_many.return_value = mock_result

    filter_obj = DocumentFilter(criteria={"status": "draft"})
    await driver.update_many("test_collection", filter_obj, {"$set": {"status": "published"}})
    collection.update_many.assert_called_once()


@pytest.mark.asyncio
async def test_delete_one(driver, mock_client) -> None:
    _, _, collection = mock_client
    mock_result = MagicMock()
    mock_result.deleted_count = 1
    collection.delete_one.return_value = mock_result

    filter_obj = DocumentFilter(criteria={"_id": "doc1"})
    await driver.delete_one("test_collection", filter_obj)
    collection.delete_one.assert_called_once()


@pytest.mark.asyncio
async def test_delete_many(driver, mock_client) -> None:
    _, _, collection = mock_client
    mock_result = MagicMock()
    mock_result.deleted_count = 2
    collection.delete_many.return_value = mock_result

    filter_obj = DocumentFilter(criteria={"status": "expired"})
    await driver.delete_many("test_collection", filter_obj)
    collection.delete_many.assert_called_once()


# ---------------------------------------------------------------------------
# Aggregation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregate_runs_pipeline(driver, mock_client) -> None:
    _, _, collection = mock_client
    pipeline = AggregationPipeline(
        stages=[{"$match": {"status": "active"}}, {"$group": {"_id": "$type"}}]
    )
    collection.aggregate.return_value = iter([{"_id": "type1", "count": 5}])

    result = await driver.aggregate("test_collection", pipeline)
    collection.aggregate.assert_called_once()
    assert len(result) == 1


@pytest.mark.asyncio
async def test_aggregate_empty_result(driver, mock_client) -> None:
    _, _, collection = mock_client
    pipeline = AggregationPipeline(stages=[{"$match": {"status": "inactive"}}])
    collection.aggregate.return_value = iter([])

    result = await driver.aggregate("test_collection", pipeline)
    assert result == []


# ---------------------------------------------------------------------------
# Index tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_index(driver, mock_client) -> None:
    _, _, collection = mock_client
    collection.create_index.return_value = "idx_name"

    definition = IndexDefinition(fields=[("name", IndexType.KEY)])
    await driver.create_index("test_collection", definition)
    collection.create_index.assert_called_once()


@pytest.mark.asyncio
async def test_create_index_with_unique(driver, mock_client) -> None:
    _, _, collection = mock_client
    collection.create_index.return_value = "unique_idx"

    definition = IndexDefinition(fields=[("name", IndexType.KEY)], unique=True, name="unique_idx")
    await driver.create_index("test_collection", definition)
    collection.create_index.assert_called_once()


@pytest.mark.asyncio
async def test_create_index_with_ttl(driver, mock_client) -> None:
    _, _, collection = mock_client
    collection.create_index.return_value = "ttl_idx"

    definition = IndexDefinition(
        fields=[("expires_at", IndexType.KEY)],
        expire_after_seconds=3600,
        name="ttl_idx",
    )
    await driver.create_index("test_collection", definition)
    collection.create_index.assert_called_once()


@pytest.mark.asyncio
async def test_create_index_with_text(driver, mock_client) -> None:
    _, _, collection = mock_client
    collection.create_index.return_value = "text_idx"

    definition = IndexDefinition(fields=[("description", IndexType.TEXT)], name="text_idx")
    await driver.create_index("test_collection", definition)
    collection.create_index.assert_called_once()


@pytest.mark.asyncio
async def test_drop_index(driver, mock_client) -> None:
    _, _, collection = mock_client
    collection.drop_index.return_value = None

    result = await driver.drop_index("test_collection", "idx_name")
    collection.drop_index.assert_called_once_with("idx_name")
    assert result is True


@pytest.mark.asyncio
async def test_drop_index_not_found(driver, mock_client) -> None:
    _, _, collection = mock_client
    from pymongo.errors import OperationFailure

    collection.drop_index.side_effect = OperationFailure("index not found", code=27)

    result = await driver.drop_index("test_collection", "nonexistent")
    assert result is False


@pytest.mark.asyncio
async def test_list_indexes(driver, mock_client) -> None:
    _, _, collection = mock_client
    indexes = [{"name": "idx1"}, {"name": "idx2"}]
    collection.list_indexes.return_value = indexes

    result = await driver.list_indexes("test_collection")
    assert result == indexes


# ---------------------------------------------------------------------------
# Session tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_session(driver, mock_client) -> None:
    mock_client_obj, _mock_db, _ = mock_client
    mock_session = MagicMock()
    mock_session.session_id = ObjectId("507f1f77bcf86cd799439011")
    mock_client_obj.return_value.start_session.return_value = mock_session

    result = await driver.start_session()
    assert result.session_id == "507f1f77bcf86cd799439011"


# ---------------------------------------------------------------------------
# Bulk tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_write(driver, mock_client) -> None:
    _, _, collection = mock_client
    mock_result = MagicMock()
    mock_result.inserted_count = 2
    mock_result.updated_count = 1
    mock_result.deleted_count = 1
    mock_result.matched_count = 1
    mock_result.modified_count = 1
    mock_result.upserted_count = 0
    collection.bulk_write.return_value = mock_result

    ops = [
        BulkOperation(operation="insert", document={"name": "a"}),
        BulkOperation(operation="insert", document={"name": "b"}),
    ]
    result = await driver.bulk_write("test_collection", ops)
    collection.bulk_write.assert_called_once()
    assert result.inserted_count == 2


# ---------------------------------------------------------------------------
# Time series tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_timeseries_collection(driver, mock_client) -> None:
    _, mock_db, _ = mock_client
    mock_db.create_collection.return_value = "timeseries_col"
    options = TimeSeriesOptions(time_field="timestamp", granularity="minute")

    result = await driver.create_time_series_collection("timeseries_col", options)
    mock_db.create_collection.assert_called_once()
    assert result is True


@pytest.mark.asyncio
async def test_create_timeseries_collection_with_meta_field(driver, mock_client) -> None:
    _, mock_db, _ = mock_client
    mock_db.create_collection.return_value = "timeseries_col"
    options = TimeSeriesOptions(time_field="ts", granularity="hour", meta_field="sensor_id")

    await driver.create_time_series_collection("timeseries_col", options)
    mock_db.create_collection.assert_called_once()


@pytest.mark.asyncio
async def test_create_timeseries_collection_with_bucket_max_span(driver, mock_client) -> None:
    _, mock_db, _ = mock_client
    mock_db.create_collection.return_value = "timeseries_col"
    options = TimeSeriesOptions(time_field="ts", granularity="second", bucket_max_span_seconds=60)

    await driver.create_time_series_collection("timeseries_col", options)
    mock_db.create_collection.assert_called_once()


@pytest.mark.asyncio
async def test_timeseries_granularity_mapping_second(driver, mock_client) -> None:
    _, mock_db, _ = mock_client
    mock_db.create_collection.return_value = "ts_col"
    options = TimeSeriesOptions(time_field="ts", granularity="second")

    await driver.create_time_series_collection("ts_col", options)
    call_kwargs = mock_db.create_collection.call_args.kwargs
    assert call_kwargs["timeseries"]["granularity"] == "second"


@pytest.mark.asyncio
async def test_timeseries_granularity_mapping_minute(driver, mock_client) -> None:
    _, mock_db, _ = mock_client
    mock_db.create_collection.return_value = "ts_col"
    options = TimeSeriesOptions(time_field="ts", granularity="minute")

    await driver.create_time_series_collection("ts_col", options)
    call_kwargs = mock_db.create_collection.call_args.kwargs
    assert call_kwargs["timeseries"]["granularity"] == "minute"


@pytest.mark.asyncio
async def test_timeseries_granularity_mapping_hour(driver, mock_client) -> None:
    _, mock_db, _ = mock_client
    mock_db.create_collection.return_value = "ts_col"
    options = TimeSeriesOptions(time_field="ts", granularity="hour")

    await driver.create_time_series_collection("ts_col", options)
    call_kwargs = mock_db.create_collection.call_args.kwargs
    assert call_kwargs["timeseries"]["granularity"] == "hour"


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_duplicate_raises_document_write_error(driver, mock_client) -> None:
    _, _, collection = mock_client
    from pymongo.errors import DuplicateKeyError as PyMongoDuplicateKeyError

    pymongo_error = PyMongoDuplicateKeyError("duplicate key error", code=11000)
    collection.insert_one.side_effect = pymongo_error

    with pytest.raises(DocumentWriteError):
        await driver.insert_one("test_collection", {"name": "test"})


@pytest.mark.asyncio
async def test_write_error_raises_document_write_error(driver, mock_client) -> None:
    _, _, collection = mock_client
    from pymongo.errors import WriteError as PyMongoWriteError

    pymongo_error = PyMongoWriteError("write error", code=11000)
    collection.insert_one.side_effect = pymongo_error

    with pytest.raises(DocumentWriteError):
        await driver.insert_one("test_collection", {"name": "test"})


# ---------------------------------------------------------------------------
# Driver lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closed_driver_raises_error(driver, mock_client) -> None:
    driver.close()

    from af.domain.document.contracts import DocumentOperationError

    with pytest.raises(DocumentOperationError):
        await driver.insert_one("test_collection", {"name": "test"})
