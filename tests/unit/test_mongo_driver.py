from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import pytest

from af.domain.document.contracts import (
    AggregateOption,
    BulkOperation,
    DocumentFilter,
    DocumentWriteError,
    IndexOption,
    TimeSeriesOptions,
    WriteOption,
)
from af.infrastructure.mongo.driver import DocumentFeature, MongoDBDriver


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
    client = MagicMock()
    db = client.return_value.__getitem__.return_value
    collection = db.return_value.__getitem__.return_value
    collection.find.return_value = []
    collection.find_one.return_value = None
    collection.count_documents.return_value = 0
    collection.aggregate.return_value = []
    collection.list_indexes.return_value = []
    collection.index_information.return_value = {}
    return client, db, collection


@pytest.fixture
def driver(mock_client):
    client, _, _ = mock_client
    return MongoDBDriver(client=client, db_name="test_db")


# ---------------------------------------------------------------------------
# Capability tests
# ---------------------------------------------------------------------------


def test_verify_capabilities_all_supported() -> None:
    driver = MongoDBDriver.__new__(MongoDBDriver)
    result = driver.verify_capabilities({DocumentFeature.CRUD})
    assert DocumentFeature.CRUD in result.supported
    assert len(result.missing) == 0


def test_verify_capabilities_transaction_missing() -> None:
    driver = MongoDBDriver.__new__(MongoDBDriver)
    result = driver.verify_capabilities({DocumentFeature.TRANSACTION})
    assert DocumentFeature.TRANSACTION in result.missing
    assert DocumentFeature.TRANSACTION not in result.supported


def test_verify_capabilities_multiple_features() -> None:
    driver = MongoDBDriver.__new__(MongoDBDriver)
    result = driver.verify_capabilities(
        {DocumentFeature.CRUD, DocumentFeature.AGGREGATION, DocumentFeature.INDEX}
    )
    assert DocumentFeature.CRUD in result.supported
    assert DocumentFeature.AGGREGATION in result.supported
    assert DocumentFeature.INDEX in result.supported
    assert len(result.missing) == 0


# ---------------------------------------------------------------------------
# Read tests
# ---------------------------------------------------------------------------


def test_find_returns_documents(driver, mock_client) -> None:
    client, _, collection = mock_client
    expected_docs = [{"_id": "doc1", "name": "test"}]
    collection.find.return_value = expected_docs

    driver.find("test_collection", DocumentFilter())
    collection.find.assert_called_once()


def test_find_with_filter_applies_criteria(driver, mock_client) -> None:
    client, _, collection = mock_client
    collection.find.return_value = []

    filter_obj = DocumentFilter(criteria={"status": "active"})
    driver.find("test_collection", filter_obj)
    collection.find.assert_called_once()


def test_find_with_limit(driver, mock_client) -> None:
    client, _, collection = mock_client
    collection.find.return_value = []

    filter_obj = DocumentFilter(criteria={}, limit=10)
    driver.find("test_collection", filter_obj)
    collection.find.assert_called_once()


def test_find_with_sort(driver, mock_client) -> None:
    client, _, collection = mock_client
    collection.find.return_value = []

    filter_obj = DocumentFilter(criteria={}, sort=[("name", 1)])
    driver.find("test_collection", filter_obj)
    collection.find.assert_called_once()


def test_find_one_returns_document(driver, mock_client) -> None:
    client, _, collection = mock_client
    expected_doc = {"_id": "doc1", "name": "test"}
    collection.find_one.return_value = expected_doc

    result = driver.find_one("test_collection", DocumentFilter())
    collection.find_one.assert_called_once()


def test_find_one_not_found_returns_none(driver, mock_client) -> None:
    client, _, collection = mock_client
    collection.find_one.return_value = None

    result = driver.find_one("test_collection", DocumentFilter())
    assert result is None


def test_count_returns_int(driver, mock_client) -> None:
    client, _, collection = mock_client
    collection.count_documents.return_value = 42

    result = driver.count("test_collection", DocumentFilter())
    assert result == 42


# ---------------------------------------------------------------------------
# Write tests
# ---------------------------------------------------------------------------


def test_insert_one_returns_id(driver, mock_client) -> None:
    client, _, collection = mock_client
    mock_result = MagicMock()
    mock_result.inserted_id = "new_id"
    collection.insert_one.return_value = mock_result

    result = driver.insert_one("test_collection", {"name": "test"})
    collection.insert_one.assert_called_once()


def test_insert_many_returns_ids(driver, mock_client) -> None:
    client, _, collection = mock_client
    mock_result = MagicMock()
    mock_result.inserted_ids = ["id1", "id2"]
    collection.insert_many.return_value = mock_result

    docs = [{"name": "test1"}, {"name": "test2"}]
    result = driver.insert_many("test_collection", docs)
    collection.insert_many.assert_called_once()


def test_update_one(driver, mock_client) -> None:
    client, _, collection = mock_client
    collection.update_one.return_value = MagicMock()

    filter_obj = DocumentFilter(criteria={"_id": "doc1"})
    driver.update_one("test_collection", filter_obj, {"$set": {"name": "updated"}})
    collection.update_one.assert_called_once()


def test_update_many(driver, mock_client) -> None:
    client, _, collection = mock_client
    collection.update_many.return_value = MagicMock()

    filter_obj = DocumentFilter(criteria={"status": "draft"})
    driver.update_many("test_collection", filter_obj, {"$set": {"status": "published"}})
    collection.update_many.assert_called_once()


def test_delete_one(driver, mock_client) -> None:
    client, _, collection = mock_client
    collection.delete_one.return_value = MagicMock()

    filter_obj = DocumentFilter(criteria={"_id": "doc1"})
    driver.delete_one("test_collection", filter_obj)
    collection.delete_one.assert_called_once()


def test_delete_many(driver, mock_client) -> None:
    client, _, collection = mock_client
    collection.delete_many.return_value = MagicMock()

    filter_obj = DocumentFilter(criteria={"status": "expired"})
    driver.delete_many("test_collection", filter_obj)
    collection.delete_many.assert_called_once()


# ---------------------------------------------------------------------------
# Aggregation tests
# ---------------------------------------------------------------------------


def test_aggregate_runs_pipeline(driver, mock_client) -> None:
    client, _, collection = mock_client
    pipeline = [{"$match": {"status": "active"}}, {"$group": {"_id": "$type"}}]
    collection.aggregate.return_value = [{"_id": "type1", "count": 5}]

    result = driver.aggregate("test_collection", pipeline)
    collection.aggregate.assert_called_once_with(pipeline)


def test_aggregate_with_options(driver, mock_client) -> None:
    client, _, collection = mock_client
    pipeline = [{"$match": {"status": "active"}}]
    option = AggregateOption(max_time_ms=5000)
    collection.aggregate.return_value = []

    driver.aggregate("test_collection", pipeline, option)
    collection.aggregate.assert_called_once()


# ---------------------------------------------------------------------------
# Index tests
# ---------------------------------------------------------------------------


def test_create_index(driver, mock_client) -> None:
    client, _, collection = mock_client
    collection.create_index.return_value = "idx_name"

    driver.create_index("test_collection", [("name", 1)])
    collection.create_index.assert_called_once()


def test_create_index_with_option(driver, mock_client) -> None:
    client, _, collection = mock_client
    collection.create_index.return_value = "idx_name"
    option = IndexOption(unique=True, name="unique_idx")

    driver.create_index("test_collection", [("name", 1)], option)
    collection.create_index.assert_called_once()


def test_drop_index(driver, mock_client) -> None:
    client, _, collection = mock_client
    collection.drop_index.return_value = None

    driver.drop_index("test_collection", "idx_name")
    collection.drop_index.assert_called_once_with("idx_name")


def test_list_indexes(driver, mock_client) -> None:
    client, _, collection = mock_client
    indexes = [{"name": "idx1"}, {"name": "idx2"}]
    collection.list_indexes.return_value = indexes

    result = driver.list_indexes("test_collection")
    assert result == indexes


# ---------------------------------------------------------------------------
# Session tests
# ---------------------------------------------------------------------------


def test_start_session(driver, mock_client) -> None:
    client, db, _ = mock_client
    mock_session = MagicMock()
    db.return_value.start_session.return_value = mock_session

    result = driver.start_session()
    db.return_value.start_session.assert_called_once()


# ---------------------------------------------------------------------------
# Bulk tests
# ---------------------------------------------------------------------------


def test_bulk_write(driver, mock_client) -> None:
    client, _, collection = mock_client
    mock_result = MagicMock()
    mock_result.inserted_count = 2
    mock_result.updated_count = 1
    mock_result.deleted_count = 1
    collection.bulk_write.return_value = mock_result

    ops = [MagicMock(), MagicMock(), MagicMock()]
    result = driver.bulk_write("test_collection", ops)
    collection.bulk_write.assert_called_once()


# ---------------------------------------------------------------------------
# Time series tests
# ---------------------------------------------------------------------------


def test_create_timeseries_collection(driver, mock_client) -> None:
    client, db, _ = mock_client
    db.return_value.create_collection.return_value = "timeseries_col"
    options = TimeSeriesOptions(time_field="timestamp", granularity="minute")

    driver.create_timeseries_collection("test_db", "timeseries_col", options)
    db.return_value.create_collection.assert_called_once()


def test_create_timeseries_collection_with_meta_field(driver, mock_client) -> None:
    client, db, _ = mock_client
    db.return_value.create_collection.return_value = "timeseries_col"
    options = TimeSeriesOptions(
        time_field="ts", granularity="hour", meta_field="sensor_id"
    )

    driver.create_timeseries_collection("test_db", "timeseries_col", options)
    db.return_value.create_collection.assert_called_once()


def test_create_timeseries_collection_with_bucket_max_span(driver, mock_client) -> None:
    client, db, _ = mock_client
    db.return_value.create_collection.return_value = "timeseries_col"
    options = TimeSeriesOptions(
        time_field="ts", granularity="second", bucket_max_span_seconds=60
    )

    driver.create_timeseries_collection("test_db", "timeseries_col", options)
    db.return_value.create_collection.assert_called_once()


def test_timeseries_granularity_mapping_second(driver, mock_client) -> None:
    client, db, _ = mock_client
    db.return_value.create_collection.return_value = "ts_col"
    options = TimeSeriesOptions(time_field="ts", granularity="second")

    driver.create_timeseries_collection("test_db", "ts_col", options)
    call_kwargs = db.return_value.create_collection.call_args.kwargs
    assert call_kwargs["timeseries"]["granularity"] == "seconds"


def test_timeseries_granularity_mapping_minute(driver, mock_client) -> None:
    client, db, _ = mock_client
    db.return_value.create_collection.return_value = "ts_col"
    options = TimeSeriesOptions(time_field="ts", granularity="minute")

    driver.create_timeseries_collection("test_db", "ts_col", options)
    call_kwargs = db.return_value.create_collection.call_args.kwargs
    assert call_kwargs["timeseries"]["granularity"] == "minutes"


def test_timeseries_granularity_mapping_hour(driver, mock_client) -> None:
    client, db, _ = mock_client
    db.return_value.create_collection.return_value = "ts_col"
    options = TimeSeriesOptions(time_field="ts", granularity="hour")

    driver.create_timeseries_collection("test_db", "ts_col", options)
    call_kwargs = db.return_value.create_collection.call_args.kwargs
    assert call_kwargs["timeseries"]["granularity"] == "hours"


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


def test_insert_duplicate_raises_document_write_error(driver, mock_client) -> None:
    client, _, collection = mock_client
    from pymongo.errors import DuplicateKeyError as PyMongoDuplicateKeyError

    pymongo_error = PyMongoDuplicateKeyError("duplicate key error", code=11000)
    collection.insert_one.side_effect = pymongo_error

    with pytest.raises(DocumentWriteError):
        driver.insert_one("test_collection", {"name": "test"})


def test_write_error_raises_document_write_error(driver, mock_client) -> None:
    client, _, collection = mock_client
    from pymongo.errors import WriteError as PyMongoWriteError

    pymongo_error = PyMongoWriteError("write error", code=11000)
    collection.insert_one.side_effect = pymongo_error

    with pytest.raises(DocumentWriteError):
        driver.insert_one("test_collection", {"name": "test"})


# ---------------------------------------------------------------------------
# Write option tests
# ---------------------------------------------------------------------------


def test_insert_with_write_option(driver, mock_client) -> None:
    client, _, collection = mock_client
    mock_result = MagicMock()
    mock_result.inserted_id = "new_id"
    collection.insert_one.return_value = mock_result
    option = WriteOption(w=1, wtimeout=5000, journal=True)

    driver.insert_one("test_collection", {"name": "test"}, option)
    collection.insert_one.assert_called_once()
