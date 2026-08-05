"""MongoDB Community document driver with explicit capability semantics."""

from __future__ import annotations

from threading import RLock
from types import TracebackType
from typing import Any

from pymongo import MongoClient, ReadPreference, errors
from pymongo.client_session import ClientSession
from pymongo.results import (
    BulkWriteResult as PyMongoBulkWriteResult,
)
from pymongo.results import (
    DeleteResult,
    InsertManyResult,
    InsertOneResult,
    UpdateResult,
)

from af.domain.document.contracts import (
    AggregationPipeline,
    BulkOperation,
    BulkWriteResult,
    DocumentFeature,
    DocumentFilter,
    DocumentOperationError,
    DocumentProjection,
    DocumentSessionError,
    DocumentTimeSeriesError,
    DocumentWriteError,
    IndexDefinition,
    IndexType,
    QueryResult,
    SessionContext,
    TimeSeriesOptions,
    TransactionNotSupportedError,
    UnsupportedDocumentFeature,
    WriteConcern,
    WriteResult,
)
from af.domain.document.contracts import (
    DocumentWriteError as DuplicateKeyError,
)
from af.domain.document.contracts import (
    ReadPreference as DocReadPreference,
)

__all__ = ("MongoDBDriver",)

_CORE_FEATURES: frozenset[DocumentFeature] = frozenset(
    {
        DocumentFeature.CRUD,
        DocumentFeature.AGGREGATION,
        DocumentFeature.INDEX,
        DocumentFeature.SESSION,
        DocumentFeature.BULK,
        DocumentFeature.TIME_SERIES,
    }
)

_READ_PREF_MAP: dict[DocReadPreference, int] = {
    DocReadPreference.PRIMARY: ReadPreference.PRIMARY,
    DocReadPreference.PRIMARY_PREFERRED: ReadPreference.PRIMARY_PREFERRED,
    DocReadPreference.SECONDARY: ReadPreference.SECONDARY,
    DocReadPreference.SECONDARY_PREFERRED: ReadPreference.SECONDARY_PREFERRED,
    DocReadPreference.NEAREST: ReadPreference.NEAREST,
}

_WRITE_CONCERN_MAP: dict[WriteConcern, dict[str, Any]] = {
    WriteConcern.ACKNOWLEDGED: {"w": 1},
    WriteConcern.JOURNALED: {"w": 1, "j": True},
    WriteConcern.MAJORITY: {"w": "majority"},
}

_INDEX_TYPE_MAP: dict[IndexType, str] = {
    IndexType.KEY: "text",
    IndexType.TEXT: "text",
    IndexType.GEO_2D: "2d",
    IndexType.GEO_2SPHERE: "2dsphere",
    IndexType.HASHED: "hashed",
    IndexType.TTL: "ttl",
}


def _sanitize_error(
    error: BaseException, *, operation: str, collection: str | None = None
) -> DocumentOperationError:
    """Convert pymongo errors to domain errors."""
    if isinstance(error, errors.OperationFailure):
        code = getattr(error, "code", None)
        if code == 11000:
            return DuplicateKeyError(
                str(error),
                collection=collection,
                operation=operation,
                code=code,
            )
        if code in (43, 10107, 13388):
            return DocumentSessionError(
                str(error),
                collection=collection,
                operation=operation,
                code=code,
            )
        return DocumentWriteError(
            str(error),
            collection=collection,
            operation=operation,
            code=code,
        )
    if isinstance(error, errors.ConnectionFailure):
        return DocumentOperationError(
            f"Connection failure during {operation}: {error}",
            collection=collection,
            operation=operation,
        )
    if isinstance(error, errors.ServerSelectionTimeoutError):
        return DocumentOperationError(
            f"Server selection timeout during {operation}: {error}",
            collection=collection,
            operation=operation,
        )
    return DocumentOperationError(
        str(error) if error else f"Unknown error during {operation}",
        collection=collection,
        operation=operation,
    )


def _to_read_preference(pref: DocReadPreference) -> int:
    return _READ_PREF_MAP.get(pref, ReadPreference.PRIMARY)


def _to_write_concern(concern: WriteConcern) -> dict[str, Any]:
    return _WRITE_CONCERN_MAP.get(concern, {"w": 1})


def _filter_to_dict(f: DocumentFilter | None) -> dict[str, Any]:
    if f is None:
        return {}
    return dict(f.criteria)


def _projection_to_dict(p: DocumentProjection | None) -> dict[str, Any] | None:
    if p is None:
        return None
    if p.include:
        return dict(p.include)
    if p.exclude:
        return dict(p.exclude)
    return None


def _index_spec(definition: IndexDefinition) -> list[tuple[str, Any]]:
    spec: list[tuple[str, Any]] = []
    for field_name, idx_type in definition.fields:
        if idx_type == IndexType.TTL:
            spec.append((field_name, 1))
        else:
            type_str = _INDEX_TYPE_MAP.get(idx_type, "text")
            spec.append((field_name, type_str))
    return spec


class MongoDBDriver:
    """MongoDB Community document driver with capability discovery."""

    def __init__(
        self, url: str, database: str = "af_default", *, replica_set: str | None = None
    ) -> None:
        if not url or not isinstance(url, str):
            raise ValueError("MongoDB URL must be a non-empty string")
        if not database or not isinstance(database, str):
            raise ValueError("MongoDB database name must be a non-empty string")

        self._url = url
        self._database_name = database
        self._replica_set = replica_set
        self._lock = RLock()
        self._closed = False
        self._session: ClientSession | None = None

        try:
            self._client = MongoClient(url, replicaSet=replica_set)
            self._database = self._client[database]
        except errors.PyMongoError as e:
            raise DocumentOperationError(
                f"Failed to connect to MongoDB: {e}",
                operation="connect",
            ) from e

        self._features = self._discover_features()

    def _discover_features(self) -> frozenset[DocumentFeature]:
        """Discover supported features at runtime."""
        features = set(_CORE_FEATURES)

        try:
            if self._is_replica_set():
                features.add(DocumentFeature.TRANSACTION)
        except Exception:
            pass

        return frozenset(features)

    def _is_replica_set(self) -> bool:
        """Check if connected to a replica set."""
        try:
            is_master = self._client.admin.command("ismaster")
            return "hosts" in is_master and len(is_master.get("hosts", [])) > 1
        except Exception:
            return False

    @property
    def features(self) -> frozenset[DocumentFeature]:
        return self._features

    def supports(self, feature: DocumentFeature) -> bool:
        if not isinstance(feature, DocumentFeature):
            raise TypeError("document feature is invalid")
        return feature in self._features

    def require(self, feature: DocumentFeature) -> None:
        if not self.supports(feature):
            if feature is DocumentFeature.TRANSACTION:
                raise TransactionNotSupportedError("transactions require a replica set topology")
            raise UnsupportedDocumentFeature(feature)

    def _assert_open(self) -> None:
        if self._closed:
            raise DocumentOperationError(
                "Driver is closed",
                operation="connection_check",
            )

    async def insert_one(
        self,
        collection: str,
        document: dict[str, Any],
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
    ) -> WriteResult:
        self._assert_open()
        try:
            coll = self._database[collection]
            wc = _to_write_concern(write_concern)
            result: InsertOneResult = coll.insert_one(
                document,
                write_concern=wc,
            )
            return WriteResult(
                acknowledged=True,
                inserted_id=str(result.inserted_id),
            )
        except Exception as e:
            raise _sanitize_error(e, operation="insert_one", collection=collection) from e

    async def insert_many(
        self,
        collection: str,
        documents: list[dict[str, Any]],
        ordered: bool = True,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
    ) -> WriteResult:
        self._assert_open()
        try:
            coll = self._database[collection]
            wc = _to_write_concern(write_concern)
            result: InsertManyResult = coll.insert_many(
                documents,
                ordered=ordered,
                write_concern=wc,
            )
            return WriteResult(
                acknowledged=True,
                inserted_ids=[str(id_) for id_ in result.inserted_ids],
            )
        except Exception as e:
            raise _sanitize_error(e, operation="insert_many", collection=collection) from e

    async def find_one(
        self,
        collection: str,
        filter: DocumentFilter | None = None,
        projection: DocumentProjection | None = None,
        read_preference: DocReadPreference = DocReadPreference.PRIMARY,
    ) -> dict[str, Any] | None:
        self._assert_open()
        try:
            coll = self._database[collection]
            doc = coll.find_one(
                _filter_to_dict(filter),
                projection=_projection_to_dict(projection),
                read_preference=_to_read_preference(read_preference),
            )
            if doc and "_id" in doc:
                doc["_id"] = str(doc["_id"])
            return doc
        except Exception as e:
            raise _sanitize_error(e, operation="find_one", collection=collection) from e

    async def find(
        self,
        collection: str,
        filter: DocumentFilter | None = None,
        projection: DocumentProjection | None = None,
        sort: dict[str, int] | None = None,
        limit: int = 0,
        skip: int = 0,
        read_preference: DocReadPreference = DocReadPreference.PRIMARY,
    ) -> QueryResult:
        self._assert_open()
        try:
            coll = self._database[collection]
            cursor = coll.find(
                _filter_to_dict(filter),
                projection=_projection_to_dict(projection),
                sort=sort,
                limit=limit,
                skip=skip,
                read_preference=_to_read_preference(read_preference),
            )
            documents = []
            for doc in cursor:
                if "_id" in doc:
                    doc["_id"] = str(doc["_id"])
                documents.append(doc)
            return QueryResult(
                documents=documents,
                count=len(documents),
            )
        except Exception as e:
            raise _sanitize_error(e, operation="find", collection=collection) from e

    async def count_documents(
        self,
        collection: str,
        filter: DocumentFilter | None = None,
    ) -> int:
        self._assert_open()
        try:
            coll = self._database[collection]
            return coll.count_documents(_filter_to_dict(filter))
        except Exception as e:
            raise _sanitize_error(e, operation="count_documents", collection=collection) from e

    async def update_one(
        self,
        collection: str,
        filter: DocumentFilter,
        update: dict[str, Any],
        upsert: bool = False,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
    ) -> WriteResult:
        self._assert_open()
        try:
            coll = self._database[collection]
            wc = _to_write_concern(write_concern)
            result: UpdateResult = coll.update_one(
                _filter_to_dict(filter),
                update,
                upsert=upsert,
                write_concern=wc,
            )
            return WriteResult(
                acknowledged=True,
                matched_count=result.matched_count,
                modified_count=result.modified_count,
                upserted_id=str(result.upserted_id) if result.upserted_id else None,
            )
        except Exception as e:
            raise _sanitize_error(e, operation="update_one", collection=collection) from e

    async def update_many(
        self,
        collection: str,
        filter: DocumentFilter,
        update: dict[str, Any],
        upsert: bool = False,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
    ) -> WriteResult:
        self._assert_open()
        try:
            coll = self._database[collection]
            wc = _to_write_concern(write_concern)
            result: UpdateResult = coll.update_many(
                _filter_to_dict(filter),
                update,
                upsert=upsert,
                write_concern=wc,
            )
            return WriteResult(
                acknowledged=True,
                matched_count=result.matched_count,
                modified_count=result.modified_count,
            )
        except Exception as e:
            raise _sanitize_error(e, operation="update_many", collection=collection) from e

    async def replace_one(
        self,
        collection: str,
        filter: DocumentFilter,
        replacement: dict[str, Any],
        upsert: bool = False,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
    ) -> WriteResult:
        self._assert_open()
        try:
            coll = self._database[collection]
            wc = _to_write_concern(write_concern)
            result: UpdateResult = coll.replace_one(
                _filter_to_dict(filter),
                replacement,
                upsert=upsert,
                write_concern=wc,
            )
            return WriteResult(
                acknowledged=True,
                matched_count=result.matched_count,
                modified_count=result.modified_count,
                upserted_id=str(result.upserted_id) if result.upserted_id else None,
            )
        except Exception as e:
            raise _sanitize_error(e, operation="replace_one", collection=collection) from e

    async def delete_one(
        self,
        collection: str,
        filter: DocumentFilter,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
    ) -> WriteResult:
        self._assert_open()
        try:
            coll = self._database[collection]
            wc = _to_write_concern(write_concern)
            result: DeleteResult = coll.delete_one(
                _filter_to_dict(filter),
                write_concern=wc,
            )
            return WriteResult(
                acknowledged=True,
                deleted_count=result.deleted_count,
            )
        except Exception as e:
            raise _sanitize_error(e, operation="delete_one", collection=collection) from e

    async def delete_many(
        self,
        collection: str,
        filter: DocumentFilter,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
    ) -> WriteResult:
        self._assert_open()
        try:
            coll = self._database[collection]
            wc = _to_write_concern(write_concern)
            result: DeleteResult = coll.delete_many(
                _filter_to_dict(filter),
                write_concern=wc,
            )
            return WriteResult(
                acknowledged=True,
                deleted_count=result.deleted_count,
            )
        except Exception as e:
            raise _sanitize_error(e, operation="delete_many", collection=collection) from e

    async def aggregate(
        self,
        collection: str,
        pipeline: AggregationPipeline,
    ) -> list[dict[str, Any]]:
        self._assert_open()
        try:
            coll = self._database[collection]
            cursor = coll.aggregate(list(pipeline.stages))
            results = []
            for doc in cursor:
                if "_id" in doc:
                    doc["_id"] = str(doc["_id"])
                results.append(doc)
            return results
        except Exception as e:
            raise _sanitize_error(e, operation="aggregate", collection=collection) from e

    async def create_index(
        self,
        collection: str,
        definition: IndexDefinition,
    ) -> str:
        self._assert_open()
        try:
            coll = self._database[collection]
            spec = _index_spec(definition)
            kwargs: dict[str, Any] = {}
            if definition.unique:
                kwargs["unique"] = True
            if definition.sparse:
                kwargs["sparse"] = True
            if definition.expire_after_seconds is not None:
                kwargs["expireAfterSeconds"] = definition.expire_after_seconds
            if definition.name:
                kwargs["name"] = definition.name

            index_name = coll.create_index(spec, **kwargs)
            return index_name
        except Exception as e:
            raise _sanitize_error(e, operation="create_index", collection=collection) from e

    async def list_indexes(
        self,
        collection: str,
    ) -> list[dict[str, Any]]:
        self._assert_open()
        try:
            coll = self._database[collection]
            indexes = []
            for index in coll.list_indexes():
                indexes.append(dict(index))
            return indexes
        except Exception as e:
            raise _sanitize_error(e, operation="list_indexes", collection=collection) from e

    async def drop_index(
        self,
        collection: str,
        index_name: str,
    ) -> bool:
        self._assert_open()
        try:
            coll = self._database[collection]
            coll.drop_index(index_name)
            return True
        except errors.OperationFailure as e:
            if e.code == 27:
                return False
            raise _sanitize_error(e, operation="drop_index", collection=collection) from e
        except Exception as e:
            raise _sanitize_error(e, operation="drop_index", collection=collection) from e

    async def start_session(self) -> SessionContext:
        self._assert_open()
        self.require(DocumentFeature.SESSION)
        try:
            session = self._client.start_session()
            self._session = session
            return SessionContext(session_id=str(session.session_id))
        except Exception as e:
            raise _sanitize_error(e, operation="start_session") from e

    async def bulk_write(
        self,
        collection: str,
        operations: list[BulkOperation],
        ordered: bool = True,
    ) -> BulkWriteResult:
        self._assert_open()
        try:
            coll = self._database[collection]
            pymongo_ops = []
            for op in operations:
                if op.operation == "insert":
                    from pymongo import InsertOne

                    pymongo_ops.append(InsertOne(op.document or {}))
                elif op.operation == "update":
                    from pymongo import UpdateOne

                    pymongo_ops.append(
                        UpdateOne(
                            _filter_to_dict(op.filter),
                            op.document or {},
                            **op.options,
                        )
                    )
                elif op.operation == "delete":
                    from pymongo import DeleteOne

                    pymongo_ops.append(DeleteOne(_filter_to_dict(op.filter)))
                elif op.operation == "replace":
                    from pymongo import ReplaceOne

                    pymongo_ops.append(
                        ReplaceOne(
                            _filter_to_dict(op.filter),
                            op.document or {},
                            **op.options,
                        )
                    )

            result: PyMongoBulkWriteResult = coll.bulk_write(pymongo_ops, ordered=ordered)
            return BulkWriteResult(
                inserted_count=result.inserted_count,
                matched_count=result.matched_count,
                modified_count=result.modified_count,
                deleted_count=result.deleted_count,
                upserted_count=result.upserted_count,
            )
        except Exception as e:
            raise _sanitize_error(e, operation="bulk_write", collection=collection) from e

    async def create_time_series_collection(
        self,
        name: str,
        options: TimeSeriesOptions,
    ) -> bool:
        self._assert_open()
        self.require(DocumentFeature.TIME_SERIES)
        try:
            validation_errors = options.validate()
            if validation_errors:
                raise DocumentTimeSeriesError(
                    f"Invalid time series options: {', '.join(validation_errors)}",
                    collection=name,
                )

            ts_options: dict[str, Any] = {
                "timeField": options.time_field,
                "granularity": options.granularity,
            }
            if options.meta_field:
                ts_options["metaField"] = options.meta_field
            if options.bucket_max_span_seconds is not None:
                ts_options["bucketMaxSpanSeconds"] = options.bucket_max_span_seconds
            if options.bucket_rounding_seconds is not None:
                ts_options["bucketRoundingSeconds"] = options.bucket_rounding_seconds

            self._database.create_collection(
                name,
                timeseries=ts_options,
            )
            return True
        except DocumentTimeSeriesError:
            raise
        except Exception as e:
            raise _sanitize_error(
                e, operation="create_time_series_collection", collection=name
            ) from e

    async def get_time_series_options(
        self,
        collection: str,
    ) -> TimeSeriesOptions | None:
        self._assert_open()
        try:
            info = self._database.command(
                "listCollections", filters={"name": collection}, cursor={}
            )
            for coll_info in info.get("cursor", {}).get("next", []):
                opts = coll_info.get("options", {})
                if "timeseries" in opts:
                    ts = opts["timeseries"]
                    return TimeSeriesOptions(
                        time_field=ts.get("timeField", ""),
                        meta_field=ts.get("metaField"),
                        granularity=ts.get("granularity", "hour"),
                        bucket_max_span_seconds=ts.get("bucketMaxSpanSeconds"),
                        bucket_rounding_seconds=ts.get("bucketRoundingSeconds"),
                    )
            return None
        except Exception as e:
            raise _sanitize_error(
                e, operation="get_time_series_options", collection=collection
            ) from e

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self._session:
                    self._session.end_session()
                    self._session = None
                self._client.close()
            except Exception:
                pass
            self._closed = True

    def __enter__(self) -> MongoDBDriver:
        self._assert_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        self.close()
        return False
