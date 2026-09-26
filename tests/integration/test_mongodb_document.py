"""Opt-in MongoDB service checks; skips are not conformance evidence."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from bson import ObjectId
from pymongo import MongoClient

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
)
from af.infrastructure.mongo.driver import MongoDBDriver


@pytest.fixture
def mongo() -> Iterator[MongoDBDriver]:
    url = os.environ.get("AF_MONGODB_TEST_URL")
    if not url:
        pytest.skip("AF_MONGODB_TEST_URL is not configured")
    database = "af_conformance_" + uuid4().hex
    driver = MongoDBDriver(url, database)
    try:
        driver.verify_capabilities()
        yield driver
    finally:
        driver.close()
        with MongoClient(url) as cleanup:
            cleanup.drop_database(database)


@pytest.mark.asyncio
async def test_mongodb_real_crud_bson_aggregation_indexes_and_bulk(mongo: MongoDBDriver) -> None:
    identifier = ObjectId()
    assert (
        await mongo.insert_one("records", {"_id": identifier, "value": 2})
    ).inserted_id == identifier
    assert await mongo.find_one("records", DocumentFilter({"_id": identifier})) == {
        "_id": identifier,
        "value": 2,
    }
    await mongo.create_index("records", IndexDefinition([("value", IndexType.KEY)], unique=True))
    with pytest.raises(DocumentWriteError):
        await mongo.insert_one("records", {"value": 2})
    await mongo.bulk_write(
        "records",
        [
            BulkOperation("update", DocumentFilter({"_id": identifier}), {"$set": {"value": 3}}),
            BulkOperation("insert", document={"value": 4}),
        ],
    )
    assert await mongo.aggregate(
        "records", AggregationPipeline([{"$group": {"_id": None, "sum": {"$sum": "$value"}}}])
    ) == [{"_id": None, "sum": 7}]
    assert (await mongo.delete_many("records", DocumentFilter())).deleted_count == 2


@pytest.mark.asyncio
async def test_mongodb_real_transaction_commit_rollback_or_preflight_denial(
    mongo: MongoDBDriver,
) -> None:
    if DocumentFeature.TRANSACTION not in mongo.features:
        with pytest.raises(TransactionNotSupportedError):
            async with mongo.transaction():
                pytest.fail("Standalone transaction entered")
        return
    await mongo.insert_one("records", {"_id": "seed"})
    async with mongo.transaction() as session:
        await mongo.insert_one("records", {"_id": "committed"}, session=session)
    with pytest.raises(ValueError, match="rollback"):
        async with mongo.transaction() as session:
            await mongo.insert_one("records", {"_id": "rolled-back"}, session=session)
            raise ValueError("rollback")
    assert await mongo.find_one("records", DocumentFilter({"_id": "committed"})) is not None
    assert await mongo.find_one("records", DocumentFilter({"_id": "rolled-back"})) is None


@pytest.mark.asyncio
async def test_mongodb_real_timeseries_retention_configuration(mongo: MongoDBDriver) -> None:
    if DocumentFeature.TIME_SERIES not in mongo.features:
        pytest.skip("MongoDB server does not support time series")
    await mongo.create_time_series_collection(
        "measurements",
        TimeSeriesOptions(
            "ts", meta_field="sensor", granularity="minute", expire_after_seconds=3600
        ),
    )
    options = await mongo.get_time_series_options("measurements")
    assert options is not None and options.expire_after_seconds == 3600
    assert options.granularity == "minute"
    await mongo.insert_one("measurements", {"ts": datetime.now(UTC), "sensor": "one", "value": 3})
    assert await mongo.count_documents("measurements") == 1
