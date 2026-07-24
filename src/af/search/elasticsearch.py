"""Bounded Elasticsearch lexical driver with mandatory policy pushdown."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from collections.abc import Mapping
from contextlib import AbstractContextManager
from threading import RLock
from time import monotonic
from types import TracebackType
from typing import Literal, Protocol

from af.framework.errors import PlatformError, PlatformReason
from af.search.contracts import ResourceEnvelope, SearchHit, SearchQuery

__all__ = (
    "ElasticsearchDriver",
    "ElasticsearchOperationError",
    "elasticsearch_index_body",
)

_INDEX = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,198}[a-z0-9])?$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.:-]{0,254}[A-Za-z0-9])?$")
_SERVER_VERSION = re.compile(r"^9\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_UNSAFE_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_SUPPORTED_FILTERS = frozenset({"tenant_id", "acl"})
_SOURCE_FIELDS = (
    "resource_id",
    "source_id",
    "authoritative_locator",
    "tenant_id",
    "projection_version",
    "acl",
    "provenance",
)
_MAX_BATCH = 10_000
_MAX_BULK_ITEMS = 256
_MAX_BULK_BYTES = 9_000_000
_MAX_LONG = 9_223_372_036_854_775_807
_MAX_SHARDS = 10_000


class ElasticsearchOperationError(PlatformError):
    """Sanitized Elasticsearch operation failure."""


def _failure(
    reason: PlatformReason,
    *,
    stage: str,
    protected_cause: BaseException | None = None,
) -> ElasticsearchOperationError:
    return ElasticsearchOperationError(
        reason,
        stage=stage,
        protected_cause=protected_cause,
    )


def _index_name(value: object) -> str:
    if type(value) is not str or _INDEX.fullmatch(value) is None:
        raise ValueError("Elasticsearch index name must be canonical lowercase text")
    return value


def _identity(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("search identity must be canonical")
    return value


def _timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= 60.0
    ):
        raise ValueError("Elasticsearch request timeout is invalid")
    return float(value)


def elasticsearch_index_body() -> dict[str, object]:
    """Return the strict mapping required by the AF Elasticsearch driver."""

    return {
        "mappings": {
            "dynamic": "strict",
            "_meta": {"af_schema_version": "1"},
            "properties": {
                "resource_id": {"type": "keyword"},
                "kind": {"type": "keyword"},
                "source_id": {"type": "keyword"},
                "authoritative_locator": {"type": "keyword", "index": False},
                "tenant_id": {"type": "keyword"},
                "projection_version": {"type": "long"},
                "acl": {"type": "keyword"},
                "provenance": {"type": "keyword", "index": False},
                "text": {"type": "text"},
                "attributes": {"type": "object", "enabled": False},
            },
        }
    }


class ElasticsearchIndicesClient(Protocol):
    """Typed subset of the optional client's index namespace."""

    def get_mapping(
        self,
        *,
        index: object,
    ) -> object: ...


class ElasticsearchClient(Protocol):
    """Typed subset of elasticsearch-py used by the trusted adapter."""

    indices: ElasticsearchIndicesClient

    def options(
        self,
        *,
        request_timeout: object,
        max_retries: object,
        retry_on_status: object,
        retry_on_timeout: object,
        ignore_status: object = (),
    ) -> ElasticsearchClient: ...

    def info(self) -> object: ...

    def bulk(
        self,
        *,
        operations: object,
        index: object = None,
        refresh: object = None,
    ) -> object: ...

    def search(
        self,
        *,
        index: object = None,
        query: object = None,
        source: object = None,
        size: object = None,
        sort: object = None,
        timeout: object = None,
        track_total_hits: object = None,
        allow_partial_search_results: object = None,
    ) -> object: ...

    def delete(
        self,
        *,
        index: object,
        id: object,
        refresh: object = None,
    ) -> object: ...

    def close(self) -> None: ...


def _mapping(
    value: object,
    *,
    statuses: tuple[int, ...] = (200,),
) -> Mapping[object, object] | None:
    try:
        body = value.body  # type: ignore[attr-defined]
        meta = value.meta  # type: ignore[attr-defined]
        status = meta.status
    except AttributeError:
        if isinstance(value, Mapping):
            return value
        return None
    except Exception:
        return None
    if type(status) is int and status in statuses and isinstance(body, Mapping):
        return body
    return None


def _status_code(error: Exception) -> int | None:
    try:
        value = getattr(error, "status_code", None)
    except Exception:
        return None
    if type(value) is int:
        return value
    return None


def _transport_error_types() -> tuple[
    tuple[type[Exception], ...],
    tuple[type[Exception], ...],
    tuple[type[Exception], ...],
]:
    try:
        module = importlib.import_module("elasticsearch")
        transport = importlib.import_module("elastic_transport")
        ssl_error = _exception_type(vars(module).get("SSLError"))
        timeout_error = _exception_type(vars(module).get("ConnectionTimeout"))
        connection_error = _exception_type(vars(module).get("ConnectionError"))
        transport_error = _exception_type(vars(transport).get("TransportError"))
    except Exception:
        return (), (), ()
    if (
        ssl_error is None
        or timeout_error is None
        or connection_error is None
        or transport_error is None
    ):
        return (), (), ()
    if not (
        issubclass(ssl_error, connection_error)
        and issubclass(connection_error, transport_error)
        and issubclass(timeout_error, transport_error)
        and not issubclass(timeout_error, connection_error)
    ):
        return (), (), ()
    return (ssl_error,), (timeout_error,), (connection_error,)


def _unsupported_product_error_types() -> tuple[type[Exception], ...]:
    try:
        module = importlib.import_module("elasticsearch")
        unsupported = _exception_type(vars(module).get("UnsupportedProductError"))
        api_error = _exception_type(vars(module).get("ApiError"))
    except Exception:
        return ()
    if unsupported is None or api_error is None or not issubclass(unsupported, api_error):
        return ()
    return (unsupported,)


def _exception_type(value: object) -> type[Exception] | None:
    if not isinstance(value, type) or not issubclass(value, Exception):
        return None
    return value


def _operation_reason(
    error: Exception,
) -> PlatformReason:
    status = _status_code(error)
    ssl_errors, timeout_errors, connection_errors = _transport_error_types()
    if isinstance(error, _unsupported_product_error_types()):
        return PlatformReason.PERMANENT_FAILURE
    if isinstance(error, ssl_errors):
        return PlatformReason.PERMANENT_FAILURE
    if isinstance(error, timeout_errors):
        return PlatformReason.TRANSIENT_FAILURE
    if isinstance(error, connection_errors):
        return PlatformReason.TRANSIENT_FAILURE
    if status in {401, 403}:
        return PlatformReason.AUTHORIZATION_DENIED
    if status == 404:
        return PlatformReason.CAPABILITY_UNAVAILABLE
    if status in {408, 429} or (status is not None and status >= 500):
        return PlatformReason.TRANSIENT_FAILURE
    return PlatformReason.PERMANENT_FAILURE


def _request(
    client: ElasticsearchClient,
    *,
    request_timeout: float,
    ignore_status: tuple[int, ...] = (),
) -> ElasticsearchClient:
    if ignore_status:
        return client.options(
            request_timeout=request_timeout,
            ignore_status=ignore_status,
            max_retries=0,
            retry_on_status=(),
            retry_on_timeout=False,
        )
    return client.options(
        request_timeout=request_timeout,
        max_retries=0,
        retry_on_status=(),
        retry_on_timeout=False,
    )


def _call_failure(
    error: Exception,
    *,
    stage: str,
) -> ElasticsearchOperationError:
    return _failure(
        _operation_reason(error),
        stage=stage,
        protected_cause=error,
    )


def _integrity(stage: str) -> ElasticsearchOperationError:
    return _failure(
        PlatformReason.INTEGRITY_FAILURE,
        stage=stage,
        protected_cause=ValueError("Elasticsearch acknowledgement is invalid"),
    )


def _document_id(tenant_id: str, source_id: str, resource_id: str) -> str:
    payload = "\0".join(("af-search-document-v1", tenant_id, source_id, resource_id)).encode()
    return hashlib.sha256(payload).hexdigest()


def _document(resource: ResourceEnvelope) -> dict[str, object]:
    return {
        "resource_id": resource.resource_id,
        "kind": resource.kind.value,
        "source_id": resource.source_id,
        "authoritative_locator": resource.authoritative_locator,
        "tenant_id": resource.tenant_id,
        "projection_version": resource.projection_version,
        "acl": list(resource.acl),
        "provenance": list(resource.provenance),
        "text": resource.text,
        "attributes": dict(resource.attributes),
    }


def _encoded_bytes(value: Mapping[str, object]) -> int:
    return (
        len(
            json.dumps(
                value,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
        + 1
    )


def _evidence(value: object, *, digest: bool = False) -> tuple[str, ...]:
    if type(value) is not list or not value or len(value) > 256:
        raise ValueError("Elasticsearch hit evidence is invalid")
    checked: list[str] = []
    for item in value:
        if (
            type(item) is not str
            or not item
            or len(item) > 256
            or _UNSAFE_TEXT.search(item) is not None
            or (digest and _SHA256.fullmatch(item) is None)
        ):
            raise ValueError("Elasticsearch hit evidence is invalid")
        checked.append(item)
    if len(set(checked)) != len(checked):
        raise ValueError("Elasticsearch hit evidence is invalid")
    return tuple(checked)


def _control_flow_rank(error: BaseException) -> int:
    if not isinstance(error, Exception):
        return 3
    if isinstance(error, PlatformError):
        if error.reason is PlatformReason.CANCELLED:
            return 2
        if error.reason is PlatformReason.SHUTDOWN_FAILED:
            return 1
    return 0


class ElasticsearchDriver(AbstractContextManager["ElasticsearchDriver"]):
    """Elasticsearch 9 lexical adapter with fail-closed policy enforcement."""

    def __init__(
        self,
        client: ElasticsearchClient,
        *,
        index: str,
        request_timeout: float = 5.0,
    ) -> None:
        self._client = client
        self._index = _index_name(index)
        self._request_timeout = _timeout(request_timeout)
        self._lock = RLock()
        self._closed = False
        self._closing = False
        self._server_version = self._verify_service()
        self._verify_mapping()

    @property
    def server_version(self) -> str:
        """Return the verified Elasticsearch service version."""

        return self._server_version

    def _verify_service(self) -> str:
        try:
            response = _request(
                self._client,
                request_timeout=self._request_timeout,
            ).info()
        except Exception as error:
            raise _call_failure(error, stage="search.elasticsearch.connect") from None
        root = _mapping(response)
        version = None if root is None else _mapping(root.get("version"))
        number = None if version is None else version.get("number")
        if (
            type(number) is not str
            or _SERVER_VERSION.fullmatch(number) is None
            or version is None
            or version.get("build_flavor") != "default"
            or version.get("build_snapshot") is not False
            or root is None
            or root.get("tagline") != "You Know, for Search"
        ):
            raise _failure(
                PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="search.elasticsearch.identity",
            )
        return number

    def _verify_mapping(self) -> None:
        try:
            response = _request(
                self._client,
                request_timeout=self._request_timeout,
            ).indices.get_mapping(index=self._index)
        except Exception as error:
            raise _call_failure(error, stage="search.elasticsearch.mapping") from None
        root = _mapping(response)
        if root is None or len(root) != 1:
            raise _failure(
                PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="search.elasticsearch.mapping",
            )
        record = _mapping(next(iter(root.values())))
        mappings = None if record is None else _mapping(record.get("mappings"))
        expected = elasticsearch_index_body()["mappings"]
        if mappings is None or dict(mappings) != expected:
            raise _failure(
                PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="search.elasticsearch.mapping",
            )

    def _assert_open(self) -> None:
        if self._closed or self._closing:
            raise _failure(
                PlatformReason.SHUTDOWN_FAILED,
                stage="search.elasticsearch.connection",
            )

    @staticmethod
    def _validate_resources(
        resources: tuple[ResourceEnvelope, ...],
    ) -> tuple[tuple[str, str, str], ...]:
        if (
            type(resources) is not tuple
            or not resources
            or len(resources) > _MAX_BATCH
            or any(not isinstance(item, ResourceEnvelope) for item in resources)
        ):
            raise ValueError("search resources must be a bounded non-empty tuple")
        identities = tuple((item.tenant_id, item.source_id, item.resource_id) for item in resources)
        if len(set(identities)) != len(identities):
            raise ValueError("search resource identities must be unique")
        if any(item.projection_version > _MAX_LONG for item in resources):
            raise _failure(
                PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="search.elasticsearch.projection-version",
            )
        return identities

    def upsert(self, resources: tuple[ResourceEnvelope, ...], /) -> int:
        """Write bounded idempotent chunks and acknowledge only complete success."""

        self._validate_resources(resources)
        with self._lock:
            self._assert_open()
            chunk_resources: list[ResourceEnvelope] = []
            chunk_body: list[Mapping[str, object]] = []
            chunk_bytes = 0
            for resource in resources:
                document_id = _document_id(
                    resource.tenant_id,
                    resource.source_id,
                    resource.resource_id,
                )
                action: Mapping[str, object] = {"index": {"_id": document_id}}
                document = _document(resource)
                item_bytes = _encoded_bytes(action) + _encoded_bytes(document)
                if chunk_resources and (
                    len(chunk_resources) >= _MAX_BULK_ITEMS
                    or chunk_bytes + item_bytes > _MAX_BULK_BYTES
                ):
                    self._write_chunk(tuple(chunk_resources), tuple(chunk_body))
                    chunk_resources.clear()
                    chunk_body.clear()
                    chunk_bytes = 0
                chunk_resources.append(resource)
                chunk_body.extend((action, document))
                chunk_bytes += item_bytes
            if chunk_resources:
                self._write_chunk(tuple(chunk_resources), tuple(chunk_body))
        return len(resources)

    def _write_chunk(
        self,
        resources: tuple[ResourceEnvelope, ...],
        body: tuple[Mapping[str, object], ...],
    ) -> None:
        try:
            response = _request(
                self._client,
                request_timeout=self._request_timeout,
            ).bulk(
                index=self._index,
                operations=body,
                refresh="wait_for",
            )
        except Exception as error:
            raise _call_failure(error, stage="search.elasticsearch.bulk") from None
        root = _mapping(response)
        items = None if root is None else root.get("items")
        errors = None if root is None else root.get("errors")
        if type(errors) is not bool or type(items) is not list or len(items) != len(resources):
            raise _integrity("search.elasticsearch.bulk-ack")
        failed_statuses: list[int] = []
        for resource, item in zip(resources, items, strict=True):
            item_record = _mapping(item)
            acknowledgement = None if item_record is None else _mapping(item_record.get("index"))
            expected_id = _document_id(
                resource.tenant_id,
                resource.source_id,
                resource.resource_id,
            )
            status = None if acknowledgement is None else acknowledgement.get("status")
            document_id = None if acknowledgement is None else acknowledgement.get("_id")
            if type(status) is not int or not 200 <= status <= 599 or document_id != expected_id:
                raise _integrity("search.elasticsearch.bulk-ack")
            if not 200 <= status < 300:
                failed_statuses.append(status)
        if errors != bool(failed_statuses):
            raise _integrity("search.elasticsearch.bulk-ack")
        if failed_statuses:
            transient = all(status in {408, 429} or status >= 500 for status in failed_statuses)
            raise _failure(
                (
                    PlatformReason.TRANSIENT_FAILURE
                    if transient
                    else PlatformReason.PERMANENT_FAILURE
                ),
                stage="search.elasticsearch.bulk-partial",
            )

    def search(self, query: SearchQuery, /) -> tuple[SearchHit, ...]:
        """Search only when tenant and ACL predicates can be pushed before ranking."""

        if not isinstance(query, SearchQuery):
            raise TypeError("Elasticsearch requires a SearchQuery")
        fields = frozenset(item.field for item in query.filters)
        tenant_ids = query.required_filter("tenant_id")
        requested_acl = query.required_filter("acl")
        if fields != _SUPPORTED_FILTERS or tenant_ids is None or requested_acl is None:
            raise _failure(
                PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="search.elasticsearch.policy-pushdown",
            )
        deadline_seconds = query.deadline_ms / 1_000
        deadline = monotonic() + deadline_seconds
        acquired = False
        try:
            acquired = self._lock.acquire(blocking=False)
            remaining_ms = query.deadline_ms
            if not acquired:
                remaining = max(0.0, deadline - monotonic())
                acquired = remaining > 0 and self._lock.acquire(timeout=remaining)
            if acquired:
                remaining_ms = min(
                    query.deadline_ms,
                    int(max(0.0, deadline - monotonic()) * 1_000),
                )
            if not acquired or remaining_ms < 1:
                raise _failure(
                    PlatformReason.CANCELLED,
                    stage="search.elasticsearch.query-queue-timeout",
                )
            self._assert_open()
            timeout_seconds = min(self._request_timeout, remaining_ms / 1_000)
            query_body: Mapping[str, object] = {
                "bool": {
                    "must": [
                        {
                            "match": {
                                "text": {
                                    "query": query.text,
                                    "operator": "and",
                                }
                            }
                        }
                    ],
                    "filter": [
                        {"terms": {"tenant_id": list(tenant_ids)}},
                        {"terms": {"acl": list(requested_acl)}},
                    ],
                }
            }
            try:
                response = _request(
                    self._client,
                    request_timeout=timeout_seconds,
                ).search(
                    index=self._index,
                    query=query_body,
                    source=list(_SOURCE_FIELDS),
                    size=query.limit,
                    sort=[
                        {"_score": "desc"},
                        {"resource_id": "asc"},
                        {"source_id": "asc"},
                        {"tenant_id": "asc"},
                    ],
                    timeout=f"{remaining_ms}ms",
                    track_total_hits=False,
                    allow_partial_search_results=False,
                )
            except Exception as error:
                raise _call_failure(error, stage="search.elasticsearch.query") from None
            if monotonic() >= deadline:
                raise _failure(
                    PlatformReason.CANCELLED,
                    stage="search.elasticsearch.query-deadline",
                )
        finally:
            if acquired:
                self._lock.release()
        return self._hits(
            response,
            query=query,
            tenant_ids=tenant_ids,
            requested_acl=requested_acl,
        )

    @staticmethod
    def _hits(
        response: object,
        *,
        query: SearchQuery,
        tenant_ids: tuple[str, ...],
        requested_acl: tuple[str, ...],
    ) -> tuple[SearchHit, ...]:
        root = _mapping(response)
        shards = None if root is None else _mapping(root.get("_shards"))
        hits_record = None if root is None else _mapping(root.get("hits"))
        raw_hits = None if hits_record is None else hits_record.get("hits")
        total_shards = None if shards is None else shards.get("total")
        successful_shards = None if shards is None else shards.get("successful")
        skipped_shards = None if shards is None else shards.get("skipped")
        failed_shards = None if shards is None else shards.get("failed")
        if root is not None and root.get("timed_out") is True:
            raise _failure(
                PlatformReason.CANCELLED,
                stage="search.elasticsearch.query-timeout",
            )
        if (
            root is None
            or type(root.get("timed_out")) is not bool
            or type(total_shards) is not int
            or not 1 <= total_shards <= _MAX_SHARDS
            or type(successful_shards) is not int
            or successful_shards != total_shards
            or type(skipped_shards) is not int
            or not 0 <= skipped_shards <= total_shards
            or type(failed_shards) is not int
            or failed_shards != 0
            or type(raw_hits) is not list
            or len(raw_hits) > query.limit
        ):
            raise _integrity("search.elasticsearch.query-ack")
        allowed_tenants = frozenset(tenant_ids)
        allowed_acl = frozenset(requested_acl)
        identities: set[tuple[str, str, str]] = set()
        results: list[SearchHit] = []
        sort_keys: list[tuple[float, str, str, str]] = []
        try:
            for rank, raw_hit in enumerate(raw_hits, start=1):
                hit_record = _mapping(raw_hit)
                source = None if hit_record is None else _mapping(hit_record.get("_source"))
                if hit_record is None or source is None:
                    raise ValueError("Elasticsearch hit shape is invalid")
                resource_id = source.get("resource_id")
                source_id = source.get("source_id")
                tenant_id = source.get("tenant_id")
                if (
                    type(resource_id) is not str
                    or type(source_id) is not str
                    or type(tenant_id) is not str
                    or tenant_id not in allowed_tenants
                    or hit_record.get("_id") != _document_id(tenant_id, source_id, resource_id)
                ):
                    raise ValueError("Elasticsearch hit identity is invalid")
                stored_acl = _evidence(source.get("acl"))
                acl_evidence = tuple(value for value in stored_acl if value in allowed_acl)
                if not acl_evidence:
                    raise ValueError("Elasticsearch hit ACL evidence is invalid")
                provenance = _evidence(source.get("provenance"), digest=True)
                projection_version = source.get("projection_version")
                if type(projection_version) is not int or not 1 <= projection_version <= _MAX_LONG:
                    raise ValueError("Elasticsearch projection version is invalid")
                identity = (tenant_id, source_id, resource_id)
                if identity in identities:
                    raise ValueError("Elasticsearch returned a duplicate identity")
                identities.add(identity)
                hit = SearchHit(
                    resource_id=resource_id,
                    source_id=source_id,
                    authoritative_locator=source.get("authoritative_locator"),  # type: ignore[arg-type]
                    tenant_id=tenant_id,
                    projection_version=projection_version,
                    acl_evidence=acl_evidence,
                    provenance=provenance,
                    score=hit_record.get("_score"),  # type: ignore[arg-type]
                    rank=rank,
                )
                results.append(hit)
                sort_keys.append((-hit.score, resource_id, source_id, tenant_id))
            if sort_keys != sorted(sort_keys):
                raise ValueError("Elasticsearch hit order is invalid")
        except (TypeError, ValueError) as error:
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.elasticsearch.hit",
                protected_cause=error,
            ) from None
        return tuple(results)

    def delete(self, *, tenant_id: str, source_id: str, resource_id: str) -> bool:
        """Delete one tenant-scoped stable document identity."""

        tenant_id = _identity(tenant_id)
        source_id = _identity(source_id)
        resource_id = _identity(resource_id)
        document_id = _document_id(tenant_id, source_id, resource_id)
        with self._lock:
            self._assert_open()
            try:
                response = _request(
                    self._client,
                    request_timeout=self._request_timeout,
                    ignore_status=(404,),
                ).delete(
                    index=self._index,
                    id=document_id,
                    refresh="wait_for",
                )
            except Exception as error:
                raise _call_failure(error, stage="search.elasticsearch.delete") from None
        root = _mapping(response, statuses=(200, 404))
        result = None if root is None else root.get("result")
        acknowledged_id = None if root is None else root.get("_id")
        if acknowledged_id != document_id or result not in {"deleted", "not_found"}:
            raise _integrity("search.elasticsearch.delete-ack")
        return result == "deleted"

    def cancel(self) -> None:
        """Report the unsupported boundary without claiming server cancellation."""

        raise _failure(
            PlatformReason.CAPABILITY_UNAVAILABLE,
            stage="search.elasticsearch.cancel",
        )

    def close(self) -> None:
        with self._lock:
            if self._closed or self._closing:
                return
            self._closing = True
            try:
                self._client.close()
            except Exception as error:
                raise _failure(
                    PlatformReason.SHUTDOWN_FAILED,
                    stage="search.elasticsearch.shutdown",
                    protected_cause=error,
                ) from None
            else:
                self._closed = True
            finally:
                self._closing = False

    def __enter__(self) -> ElasticsearchDriver:
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
