"""MongoDB Community document driver with explicit capability semantics."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from threading import RLock
from types import TracebackType
from typing import Any, Literal

from pymongo import (
    DeleteOne,
    InsertOne,
    MongoClient,
    ReadPreference,
    ReplaceOne,
    UpdateOne,
    errors,
)
from pymongo.client_session import ClientSession
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.read_preferences import _ServerMode
from pymongo.results import (
    BulkWriteResult as PyMongoBulkWriteResult,
)
from pymongo.results import (
    DeleteResult,
    InsertManyResult,
    InsertOneResult,
    UpdateResult,
)
from pymongo.write_concern import WriteConcern as PyMongoWriteConcern

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

type _MongoWriteOperation = (
    InsertOne[dict[str, Any]] | DeleteOne | ReplaceOne[dict[str, Any]] | UpdateOne
)

_CORE_FEATURES: frozenset[DocumentFeature] = frozenset(
    {
        DocumentFeature.CRUD,
        DocumentFeature.AGGREGATION,
        DocumentFeature.INDEX,
        DocumentFeature.BULK,
    }
)

_READ_PREF_MAP: dict[DocReadPreference, _ServerMode] = {
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

_INDEX_TYPE_MAP: dict[IndexType, str | int] = {
    IndexType.KEY: 1,
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
                f"Document operation failed: {operation}",
                collection=collection,
                operation=operation,
                code=code,
            )
        if code in (43, 10107, 13388):
            return DocumentSessionError(
                f"Document operation failed: {operation}",
                collection=collection,
                operation=operation,
                code=code,
            )
        return DocumentWriteError(
            f"Document operation failed: {operation}",
            collection=collection,
            operation=operation,
            code=code,
        )
    if isinstance(error, errors.ConnectionFailure):
        return DocumentOperationError(
            f"Document connection failed: {operation}",
            collection=collection,
            operation=operation,
        )
    if isinstance(error, errors.ServerSelectionTimeoutError):
        return DocumentOperationError(
            f"Document server unavailable: {operation}",
            collection=collection,
            operation=operation,
        )
    return DocumentOperationError(
        f"Document operation failed: {operation}",
        collection=collection,
        operation=operation,
    )


def _to_read_preference(pref: DocReadPreference) -> _ServerMode:
    return _READ_PREF_MAP.get(pref, ReadPreference.PRIMARY)


def _to_write_concern(concern: WriteConcern) -> PyMongoWriteConcern:
    return PyMongoWriteConcern(**_WRITE_CONCERN_MAP.get(concern, {"w": 1}))


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


class _MongoSessionContext(SessionContext):
    def __init__(self, native: ClientSession, on_close: Callable[[str], None]) -> None:
        super().__init__(str(native.session_id))
        self.native = native
        self._on_close = on_close

    def end(self) -> None:
        if self.is_active:
            try:
                self.native.end_session()
            finally:
                self.is_active = False
                self._on_close(self.session_id)

    async def close(self) -> None:
        self.end()


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
        self._sessions: dict[str, _MongoSessionContext] = {}

        try:
            self._client: MongoClient[dict[str, Any]] = MongoClient(url, replicaSet=replica_set)
            self._database: Database[dict[str, Any]] = self._client[database]
        except errors.PyMongoError:
            raise DocumentOperationError(
                "Failed to connect to MongoDB",
                operation="connect",
            ) from None

        self._features = self._discover_features()

    def _discover_features(self) -> frozenset[DocumentFeature]:
        """Discover topology/version capabilities without assuming a host count."""
        features = set(_CORE_FEATURES)
        try:
            hello = self._client.admin.command("hello")
            wire = hello.get("maxWireVersion", 0)
            if type(wire) is not int:
                return frozenset(features)
            if wire >= 13:
                features.add(DocumentFeature.TIME_SERIES)
            if type(hello.get("logicalSessionTimeoutMinutes")) is int:
                features.add(DocumentFeature.SESSION)
                if (hello.get("setName") and wire >= 7) or (
                    hello.get("msg") == "isdbgrid" and wire >= 8
                ):
                    features.add(DocumentFeature.TRANSACTION)
        except errors.PyMongoError:
            pass
        return frozenset(features)

    def verify_capabilities(self) -> frozenset[DocumentFeature]:
        """Refresh topology evidence only after a successful liveness check."""
        self._assert_open()
        try:
            self._client.admin.command("ping")
            self._features = self._discover_features()
            return self._features
        except errors.PyMongoError as error:
            raise _sanitize_error(error, operation="verify_capabilities") from None

    def _session_options(self, session: SessionContext | None) -> dict[str, Any]:
        if session is None:
            return {}
        if (
            not isinstance(session, _MongoSessionContext)
            or not session.is_active
            or self._sessions.get(session.session_id) is not session
        ):
            raise DocumentSessionError("Session is closed or belongs to another driver")
        return {"session": session.native}

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

    def _read_collection(
        self, collection: str, read_preference: DocReadPreference
    ) -> Collection[dict[str, Any]]:
        return self._database[collection].with_options(
            read_preference=_to_read_preference(read_preference)
        )

    def _write_collection(
        self, collection: str, write_concern: WriteConcern
    ) -> Collection[dict[str, Any]]:
        return self._database[collection].with_options(
            write_concern=_to_write_concern(write_concern)
        )

    async def insert_one(
        self,
        collection: str,
        document: dict[str, Any],
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
        *,
        session: SessionContext | None = None,
    ) -> WriteResult:
        self._assert_open()
        session_options = self._session_options(session)
        try:
            coll = self._write_collection(collection, write_concern)
            result: InsertOneResult = coll.insert_one(document, **session_options)
            return WriteResult(
                acknowledged=True,
                inserted_id=result.inserted_id,
            )
        except Exception as e:
            raise _sanitize_error(e, operation="insert_one", collection=collection) from None

    async def insert_many(
        self,
        collection: str,
        documents: list[dict[str, Any]],
        ordered: bool = True,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
        *,
        session: SessionContext | None = None,
    ) -> WriteResult:
        self._assert_open()
        session_options = self._session_options(session)
        try:
            coll = self._write_collection(collection, write_concern)
            result: InsertManyResult = coll.insert_many(
                documents,
                ordered=ordered,
                **session_options,
            )
            return WriteResult(
                acknowledged=True,
                inserted_ids=list(result.inserted_ids),
            )
        except Exception as e:
            raise _sanitize_error(e, operation="insert_many", collection=collection) from None

    async def find_one(
        self,
        collection: str,
        filter: DocumentFilter | None = None,
        projection: DocumentProjection | None = None,
        read_preference: DocReadPreference = DocReadPreference.PRIMARY,
        *,
        session: SessionContext | None = None,
    ) -> dict[str, Any] | None:
        self._assert_open()
        session_options = self._session_options(session)
        try:
            coll = self._read_collection(collection, read_preference)
            doc = coll.find_one(
                _filter_to_dict(filter),
                projection=_projection_to_dict(projection),
                **session_options,
            )
            return doc
        except Exception as e:
            raise _sanitize_error(e, operation="find_one", collection=collection) from None

    async def find(
        self,
        collection: str,
        filter: DocumentFilter | None = None,
        projection: DocumentProjection | None = None,
        sort: dict[str, int] | None = None,
        limit: int = 0,
        skip: int = 0,
        read_preference: DocReadPreference = DocReadPreference.PRIMARY,
        *,
        session: SessionContext | None = None,
    ) -> QueryResult:
        self._assert_open()
        session_options = self._session_options(session)
        try:
            coll = self._read_collection(collection, read_preference)
            cursor = coll.find(
                _filter_to_dict(filter),
                projection=_projection_to_dict(projection),
                **session_options,
                sort=sort,
                limit=limit,
                skip=skip,
            )
            documents = []
            for doc in cursor:
                documents.append(doc)
            return QueryResult(
                documents=documents,
                count=len(documents),
            )
        except Exception as e:
            raise _sanitize_error(e, operation="find", collection=collection) from None

    async def count_documents(
        self,
        collection: str,
        filter: DocumentFilter | None = None,
        *,
        session: SessionContext | None = None,
    ) -> int:
        self._assert_open()
        session_options = self._session_options(session)
        try:
            coll = self._database[collection]
            return int(coll.count_documents(_filter_to_dict(filter), **session_options))
        except Exception as e:
            raise _sanitize_error(e, operation="count_documents", collection=collection) from None

    async def update_one(
        self,
        collection: str,
        filter: DocumentFilter,
        update: dict[str, Any],
        upsert: bool = False,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
        *,
        session: SessionContext | None = None,
    ) -> WriteResult:
        self._assert_open()
        session_options = self._session_options(session)
        try:
            coll = self._write_collection(collection, write_concern)
            result: UpdateResult = coll.update_one(
                _filter_to_dict(filter),
                update,
                upsert=upsert,
                **session_options,
            )
            return WriteResult(
                acknowledged=True,
                matched_count=result.matched_count,
                modified_count=result.modified_count,
                upserted_id=result.upserted_id,
            )
        except Exception as e:
            raise _sanitize_error(e, operation="update_one", collection=collection) from None

    async def update_many(
        self,
        collection: str,
        filter: DocumentFilter,
        update: dict[str, Any],
        upsert: bool = False,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
        *,
        session: SessionContext | None = None,
    ) -> WriteResult:
        self._assert_open()
        session_options = self._session_options(session)
        try:
            coll = self._write_collection(collection, write_concern)
            result: UpdateResult = coll.update_many(
                _filter_to_dict(filter),
                update,
                upsert=upsert,
                **session_options,
            )
            return WriteResult(
                acknowledged=True,
                matched_count=result.matched_count,
                modified_count=result.modified_count,
            )
        except Exception as e:
            raise _sanitize_error(e, operation="update_many", collection=collection) from None

    async def replace_one(
        self,
        collection: str,
        filter: DocumentFilter,
        replacement: dict[str, Any],
        upsert: bool = False,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
        *,
        session: SessionContext | None = None,
    ) -> WriteResult:
        self._assert_open()
        session_options = self._session_options(session)
        try:
            coll = self._write_collection(collection, write_concern)
            result: UpdateResult = coll.replace_one(
                _filter_to_dict(filter),
                replacement,
                upsert=upsert,
                **session_options,
            )
            return WriteResult(
                acknowledged=True,
                matched_count=result.matched_count,
                modified_count=result.modified_count,
                upserted_id=result.upserted_id,
            )
        except Exception as e:
            raise _sanitize_error(e, operation="replace_one", collection=collection) from None

    async def delete_one(
        self,
        collection: str,
        filter: DocumentFilter,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
        *,
        session: SessionContext | None = None,
    ) -> WriteResult:
        self._assert_open()
        session_options = self._session_options(session)
        try:
            coll = self._write_collection(collection, write_concern)
            result: DeleteResult = coll.delete_one(_filter_to_dict(filter), **session_options)
            return WriteResult(
                acknowledged=True,
                deleted_count=result.deleted_count,
            )
        except Exception as e:
            raise _sanitize_error(e, operation="delete_one", collection=collection) from None

    async def delete_many(
        self,
        collection: str,
        filter: DocumentFilter,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
        *,
        session: SessionContext | None = None,
    ) -> WriteResult:
        self._assert_open()
        session_options = self._session_options(session)
        try:
            coll = self._write_collection(collection, write_concern)
            result: DeleteResult = coll.delete_many(_filter_to_dict(filter), **session_options)
            return WriteResult(
                acknowledged=True,
                deleted_count=result.deleted_count,
            )
        except Exception as e:
            raise _sanitize_error(e, operation="delete_many", collection=collection) from None

    async def aggregate(
        self,
        collection: str,
        pipeline: AggregationPipeline,
        *,
        session: SessionContext | None = None,
    ) -> list[dict[str, Any]]:
        self._assert_open()
        session_options = self._session_options(session)
        try:
            coll = self._database[collection]
            cursor = coll.aggregate(list(pipeline.stages), **session_options)
            results = []
            for doc in cursor:
                results.append(doc)
            return results
        except Exception as e:
            raise _sanitize_error(e, operation="aggregate", collection=collection) from None

    async def create_index(
        self,
        collection: str,
        definition: IndexDefinition,
        *,
        session: SessionContext | None = None,
    ) -> str:
        self._assert_open()
        session_options = self._session_options(session)
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

            index_name = coll.create_index(spec, **kwargs, **session_options)
            return str(index_name)
        except Exception as e:
            raise _sanitize_error(e, operation="create_index", collection=collection) from None

    async def list_indexes(
        self,
        collection: str,
        *,
        session: SessionContext | None = None,
    ) -> list[dict[str, Any]]:
        self._assert_open()
        session_options = self._session_options(session)
        try:
            coll = self._database[collection]
            indexes = []
            for index in coll.list_indexes(**session_options):
                indexes.append(dict(index))
            return indexes
        except Exception as e:
            raise _sanitize_error(e, operation="list_indexes", collection=collection) from None

    async def drop_index(
        self,
        collection: str,
        index_name: str,
        *,
        session: SessionContext | None = None,
    ) -> bool:
        self._assert_open()
        session_options = self._session_options(session)
        try:
            coll = self._database[collection]
            coll.drop_index(index_name, **session_options)
            return True
        except errors.OperationFailure as e:
            if e.code == 27:
                return False
            raise _sanitize_error(e, operation="drop_index", collection=collection) from None
        except Exception as e:
            raise _sanitize_error(e, operation="drop_index", collection=collection) from None

    def _forget_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def start_session(self) -> SessionContext:
        self._assert_open()
        self.require(DocumentFeature.SESSION)
        try:
            native = self._client.start_session()
            handle = _MongoSessionContext(native, self._forget_session)
            self._sessions[handle.session_id] = handle
            return handle
        except errors.PyMongoError as error:
            raise _sanitize_error(error, operation="start_session") from None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[SessionContext]:
        """Use ``session=handle`` on every operation belonging to this transaction."""
        self._assert_open()
        self.require(DocumentFeature.TRANSACTION)
        handle = await self.start_session()
        native = self._session_options(handle)["session"]
        try:
            try:
                native.start_transaction()
            except errors.PyMongoError as error:
                raise _sanitize_error(error, operation="start_transaction") from None
            try:
                yield handle
            except BaseException:
                if native.in_transaction:
                    # Preserve the original application/cancellation exception.
                    with suppress(errors.PyMongoError):
                        native.abort_transaction()
                raise
            else:
                try:
                    native.commit_transaction()
                except errors.PyMongoError as error:
                    raise _sanitize_error(error, operation="commit_transaction") from None
        finally:
            try:
                await handle.close()
            finally:
                self._sessions.pop(handle.session_id, None)

    async def bulk_write(
        self,
        collection: str,
        operations: list[BulkOperation],
        ordered: bool = True,
        *,
        session: SessionContext | None = None,
    ) -> BulkWriteResult:
        self._assert_open()
        session_options = self._session_options(session)
        try:
            coll = self._database[collection]
            pymongo_ops: list[_MongoWriteOperation] = []
            for op in operations:
                if op.operation not in {"insert", "update", "delete", "replace"}:
                    raise ValueError("Unknown bulk document operation")
                if op.operation == "insert":
                    pymongo_ops.append(InsertOne(op.document or {}))
                elif op.operation == "update":
                    pymongo_ops.append(
                        UpdateOne(
                            _filter_to_dict(op.filter),
                            op.document or {},
                            **op.options,
                        )
                    )
                elif op.operation == "delete":
                    pymongo_ops.append(DeleteOne(_filter_to_dict(op.filter)))
                elif op.operation == "replace":
                    pymongo_ops.append(
                        ReplaceOne(
                            _filter_to_dict(op.filter),
                            op.document or {},
                            **op.options,
                        )
                    )

            result: PyMongoBulkWriteResult = coll.bulk_write(
                pymongo_ops, ordered=ordered, **session_options
            )
            return BulkWriteResult(
                inserted_count=result.inserted_count,
                matched_count=result.matched_count,
                modified_count=result.modified_count,
                deleted_count=result.deleted_count,
                upserted_count=result.upserted_count,
            )
        except Exception as e:
            raise _sanitize_error(e, operation="bulk_write", collection=collection) from None

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
            }
            if options.bucket_max_span_seconds is None:
                ts_options["granularity"] = options.granularity.rstrip("s") + "s"
            if options.meta_field:
                ts_options["metaField"] = options.meta_field
            if options.bucket_max_span_seconds is not None:
                ts_options["bucketMaxSpanSeconds"] = options.bucket_max_span_seconds
            if options.bucket_rounding_seconds is not None:
                ts_options["bucketRoundingSeconds"] = options.bucket_rounding_seconds

            collection_options: dict[str, Any] = {}
            if options.expire_after_seconds is not None:
                collection_options["expireAfterSeconds"] = options.expire_after_seconds
            self._database.create_collection(
                name,
                timeseries=ts_options,
                **collection_options,
            )
            return True
        except DocumentTimeSeriesError:
            raise
        except Exception as e:
            raise _sanitize_error(
                e, operation="create_time_series_collection", collection=name
            ) from None

    async def get_time_series_options(
        self,
        collection: str,
    ) -> TimeSeriesOptions | None:
        self._assert_open()
        try:
            info = self._database.command("listCollections", filter={"name": collection}, cursor={})
            for coll_info in info.get("cursor", {}).get("firstBatch", []):
                opts = coll_info.get("options", {})
                if "timeseries" in opts:
                    ts = opts["timeseries"]
                    return TimeSeriesOptions(
                        time_field=ts.get("timeField", ""),
                        meta_field=ts.get("metaField"),
                        granularity=ts.get("granularity", "hours").rstrip("s"),
                        bucket_max_span_seconds=ts.get("bucketMaxSpanSeconds"),
                        bucket_rounding_seconds=ts.get("bucketRoundingSeconds"),
                        expire_after_seconds=opts.get("expireAfterSeconds"),
                    )
            return None
        except Exception as e:
            raise _sanitize_error(
                e, operation="get_time_series_options", collection=collection
            ) from None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                for handle in tuple(self._sessions.values()):
                    with suppress(Exception):
                        handle.end()
                self._sessions.clear()
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
    ) -> Literal[False]:
        self.close()
        return False
