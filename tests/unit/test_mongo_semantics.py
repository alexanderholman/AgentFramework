"""Regression tests for MongoDB wire semantics, separate from service certification."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId
from pymongo.errors import ConnectionFailure

from af.domain.document.contracts import (
    AggregationPipeline,
    BulkOperation,
    DocumentFeature,
    DocumentFilter,
    DocumentOperationError,
    DocumentSessionError,
    IndexDefinition,
    IndexType,
    TimeSeriesOptions,
    TransactionNotSupportedError,
)
from af.infrastructure.mongo.driver import MongoDBDriver


def make_driver(hello=None):
    client = MagicMock()
    client.admin.command.return_value = hello or {
        "maxWireVersion": 17,
        "logicalSessionTimeoutMinutes": 30,
        "setName": "rs0",
        "hosts": ["localhost:27017"],
    }
    collection = client.__getitem__.return_value.__getitem__.return_value
    collection.with_options.return_value = collection
    with patch("af.infrastructure.mongo.driver.MongoClient", return_value=client):
        driver = MongoDBDriver("mongodb://localhost")
    return driver, client, collection


@pytest.mark.asyncio
async def test_bson_identifiers_remain_usable_across_read_and_write() -> None:
    driver, _, collection = make_driver()
    identifier = ObjectId()
    document = {"_id": identifier, "value": 4}
    collection.insert_one.return_value.inserted_id = identifier
    collection.find_one.return_value = document
    collection.find.return_value = [document]
    collection.aggregate.return_value = [document]
    assert (await driver.insert_one("records", document)).inserted_id == identifier
    assert (await driver.find_one("records"))["_id"] == identifier
    assert (await driver.find("records")).documents[0]["_id"] == identifier
    assert (await driver.aggregate("records", AggregationPipeline()))[0]["_id"] == identifier
    collection.update_one.return_value.upserted_id = 0
    result = await driver.update_one("records", DocumentFilter({"_id": identifier}), {}, True)
    assert result.upserted_id == 0


@pytest.mark.asyncio
async def test_key_index_uses_numeric_ascending_order() -> None:
    driver, _, collection = make_driver()
    await driver.create_index("records", IndexDefinition([("name", IndexType.KEY)]))
    assert collection.create_index.call_args.args[0] == [("name", 1)]


@pytest.mark.asyncio
async def test_backend_messages_are_not_exposed() -> None:
    driver, _, collection = make_driver()
    collection.insert_one.side_effect = ConnectionFailure("password=hidden mongodb://secret")
    with pytest.raises(DocumentOperationError) as error:
        await driver.insert_one("records", {})
    assert "hidden" not in str(error.value)
    assert "secret" not in str(error.value)


def test_single_member_replica_set_supports_transactions() -> None:
    driver, _, _ = make_driver()
    assert DocumentFeature.TRANSACTION in driver.features


def test_old_standalone_does_not_advertise_unavailable_features() -> None:
    driver, _, _ = make_driver({"maxWireVersion": 6})
    assert DocumentFeature.TRANSACTION not in driver.features
    assert DocumentFeature.TIME_SERIES not in driver.features
    assert DocumentFeature.SESSION not in driver.features


@pytest.mark.asyncio
async def test_timeseries_wire_granularity_and_retention() -> None:
    driver, client, _ = make_driver()
    await driver.create_time_series_collection(
        "readings", TimeSeriesOptions("ts", granularity="minute", expire_after_seconds=3600)
    )
    kwargs = client.__getitem__.return_value.create_collection.call_args.kwargs
    assert kwargs["timeseries"]["granularity"] == "minutes"
    assert kwargs["expireAfterSeconds"] == 3600


@pytest.mark.asyncio
async def test_custom_buckets_omit_incompatible_granularity() -> None:
    driver, client, _ = make_driver()
    await driver.create_time_series_collection(
        "readings", TimeSeriesOptions("ts", bucket_max_span_seconds=60, bucket_rounding_seconds=60)
    )
    options = client.__getitem__.return_value.create_collection.call_args.kwargs["timeseries"]
    assert "granularity" not in options
    assert options["bucketRoundingSeconds"] == options["bucketMaxSpanSeconds"] == 60


@pytest.mark.asyncio
async def test_bulk_rejects_unknown_operation_before_any_write() -> None:
    driver, _, collection = make_driver()
    with pytest.raises((ValueError, DocumentOperationError)):
        await driver.bulk_write(
            "records", [BulkOperation("insert", document={}), BulkOperation("typo")]
        )
    collection.bulk_write.assert_not_called()


@pytest.mark.asyncio
async def test_transaction_attaches_session_and_commits() -> None:
    driver, client, collection = make_driver()
    native = client.start_session.return_value
    async with driver.transaction() as session:
        await driver.insert_one("records", {"a": 1}, session=session)
        await driver.find("records", session=session)
        assert collection.insert_one.call_args.kwargs["session"] is native
        assert collection.find.call_args.kwargs["session"] is native
    native.start_transaction.assert_called_once()
    native.commit_transaction.assert_called_once()
    native.end_session.assert_called_once()
    assert not session.is_active


@pytest.mark.asyncio
async def test_transaction_aborts_and_preserves_application_exception() -> None:
    driver, client, _ = make_driver()
    with pytest.raises(ValueError, match="application"):
        async with driver.transaction():
            raise ValueError("application")
    client.start_session.return_value.abort_transaction.assert_called_once()
    client.start_session.return_value.commit_transaction.assert_not_called()


@pytest.mark.asyncio
async def test_standalone_transaction_fails_before_session_or_write() -> None:
    driver, client, collection = make_driver({"maxWireVersion": 17})
    with pytest.raises(TransactionNotSupportedError):
        async with driver.transaction():
            await driver.insert_one("records", {})
    client.start_session.assert_not_called()
    collection.insert_one.assert_not_called()


@pytest.mark.asyncio
async def test_closed_and_foreign_sessions_fail_before_database_access() -> None:
    driver, _, collection = make_driver()
    other, _, _ = make_driver()
    session = await other.start_session()
    with pytest.raises(DocumentSessionError):
        await driver.insert_one("records", {}, session=session)
    await session.close()
    with pytest.raises(DocumentSessionError):
        await other.find("records", session=session)
    collection.insert_one.assert_not_called()
