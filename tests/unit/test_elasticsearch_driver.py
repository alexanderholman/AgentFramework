from __future__ import annotations

import hashlib
from collections.abc import Mapping
from threading import Event, Thread

import pytest

from af.framework.errors import PlatformReason
from af.search import elasticsearch as elasticsearch_module
from af.search.contracts import (
    ResourceEnvelope,
    ResourceKind,
    SearchFilter,
    SearchQuery,
)
from af.search.elasticsearch import (
    ElasticsearchDriver,
    ElasticsearchOperationError,
    elasticsearch_index_body,
)


def _resource(
    resource_id: str,
    *,
    tenant_id: str = "tenant-a",
    acl: tuple[str, ...] = ("group:research",),
    text: str = "bounded searchable energy content",
    projection_version: int = 1,
) -> ResourceEnvelope:
    return ResourceEnvelope(
        resource_id=resource_id,
        kind=ResourceKind.TEXT,
        source_id="source-primary",
        authoritative_locator=f"af://source-primary/{resource_id}",
        tenant_id=tenant_id,
        projection_version=projection_version,
        acl=acl,
        provenance=("sha256:" + "a" * 64,),
        text=text,
        attributes={"language": "en"},
    )


def _query(
    *,
    tenant_ids: tuple[str, ...] = ("tenant-a",),
    acl: tuple[str, ...] = ("group:research",),
    limit: int = 20,
    deadline_ms: int = 1_000,
) -> SearchQuery:
    return SearchQuery(
        text="energy",
        filters=(
            SearchFilter("tenant_id", tenant_ids),
            SearchFilter("acl", acl),
        ),
        limit=limit,
        deadline_ms=deadline_ms,
    )


class _Indices:
    def __init__(self, owner: RecordingClient) -> None:
        self._owner = owner
        self.mapping: object = {"af-search": {"mappings": elasticsearch_index_body()["mappings"]}}
        self.error: Exception | None = None

    def get_mapping(
        self,
        *,
        index: object,
    ) -> object:
        self._owner.mapping_calls.append((index, self._owner._take_options()))
        if self.error is not None:
            raise self.error
        return self.mapping


def _complete_shards() -> dict[str, int]:
    return {"total": 1, "successful": 1, "skipped": 0, "failed": 0}


class RecordingClient:
    def __init__(self) -> None:
        self.indices = _Indices(self)
        self.info_response: object = {
            "version": {
                "number": "9.4.4",
                "build_flavor": "default",
                "build_snapshot": False,
            },
            "tagline": "You Know, for Search",
        }
        self.bulk_response: object | None = None
        self.search_response: object = {
            "timed_out": False,
            "_shards": _complete_shards(),
            "hits": {"hits": []},
        }
        self.delete_response: object = {"_id": "unused", "result": "not_found"}
        self.info_calls: list[dict[str, object]] = []
        self.mapping_calls: list[tuple[object, dict[str, object]]] = []
        self.bulk_calls: list[dict[str, object]] = []
        self.search_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.closed = 0
        self.info_error: Exception | None = None
        self.bulk_error: Exception | None = None
        self.search_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.close_error: Exception | None = None
        self._request_options: dict[str, object] | None = None

    def options(self, **kwargs: object) -> RecordingClient:
        assert self._request_options is None
        self._request_options = kwargs
        return self

    def _take_options(self) -> dict[str, object]:
        assert self._request_options is not None
        options = self._request_options
        self._request_options = None
        return options

    def info(self) -> object:
        self.info_calls.append(self._take_options())
        if self.info_error is not None:
            raise self.info_error
        return self.info_response

    def bulk(
        self,
        *,
        operations: object,
        index: object = None,
        refresh: object = None,
    ) -> object:
        self.bulk_calls.append(
            {
                "index": index,
                "operations": operations,
                "refresh": refresh,
                "options": self._take_options(),
            }
        )
        if self.bulk_error is not None:
            raise self.bulk_error
        if self.bulk_response is not None:
            return self.bulk_response
        assert type(operations) is tuple
        return {
            "errors": False,
            "items": [
                {"index": {"_id": action["index"]["_id"], "status": 201}}
                for action in operations[::2]
            ],
        }

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
    ) -> object:
        self.search_calls.append(
            {
                "index": index,
                "query": query,
                "source": source,
                "size": size,
                "sort": sort,
                "timeout": timeout,
                "track_total_hits": track_total_hits,
                "allow_partial_search_results": allow_partial_search_results,
                "options": self._take_options(),
            }
        )
        if self.search_error is not None:
            raise self.search_error
        return self.search_response

    def delete(
        self,
        *,
        index: object,
        id: object,
        refresh: object = None,
    ) -> object:
        self.delete_calls.append(
            {
                "index": index,
                "id": id,
                "refresh": refresh,
                "options": self._take_options(),
            }
        )
        if self.delete_error is not None:
            raise self.delete_error
        return self.delete_response

    def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def _document_id(resource: ResourceEnvelope) -> str:
    identity = "\0".join(
        ("af-search-document-v1", resource.tenant_id, resource.source_id, resource.resource_id)
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _hit_source(resource: ResourceEnvelope) -> dict[str, object]:
    return {
        "resource_id": resource.resource_id,
        "source_id": resource.source_id,
        "authoritative_locator": resource.authoritative_locator,
        "tenant_id": resource.tenant_id,
        "projection_version": resource.projection_version,
        "acl": list(resource.acl),
        "provenance": list(resource.provenance),
    }


def test_elasticsearch_bulk_write_uses_stable_identity_and_canonical_document() -> None:
    client = RecordingClient()
    resource = _resource("resource-a")
    driver = ElasticsearchDriver(client, index="af-search", request_timeout=5.0)

    assert driver.upsert((resource,)) == 1

    call = client.bulk_calls[0]
    assert call["index"] == "af-search"
    assert call["refresh"] == "wait_for"
    assert call["options"] == {
        "request_timeout": 5.0,
        "max_retries": 0,
        "retry_on_status": (),
        "retry_on_timeout": False,
    }
    assert call["operations"] == (
        {"index": {"_id": _document_id(resource)}},
        {
            "resource_id": "resource-a",
            "kind": "text",
            "source_id": "source-primary",
            "authoritative_locator": "af://source-primary/resource-a",
            "tenant_id": "tenant-a",
            "projection_version": 1,
            "acl": ["group:research"],
            "provenance": ["sha256:" + "a" * 64],
            "text": "bounded searchable energy content",
            "attributes": {"language": "en"},
        },
    )


def test_elasticsearch_pushes_tenant_and_acl_filters_before_top_k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(elasticsearch_module, "monotonic", lambda: 100.0)
    client = RecordingClient()
    resource = _resource("resource-a")
    client.search_response = {
        "timed_out": False,
        "_shards": _complete_shards(),
        "hits": {
            "hits": [
                {
                    "_id": _document_id(resource),
                    "_score": 2.5,
                    "_source": _hit_source(resource),
                }
            ]
        },
    }
    driver = ElasticsearchDriver(client, index="af-search", request_timeout=5.0)

    hits = driver.search(
        _query(
            tenant_ids=("tenant-a", "tenant-b"),
            acl=("group:research", "user:alice"),
            limit=1,
            deadline_ms=250,
        )
    )

    assert len(hits) == 1
    assert hits[0].resource_id == "resource-a"
    assert hits[0].acl_evidence == ("group:research",)
    call = client.search_calls[0]
    assert call["index"] == "af-search"
    assert call["allow_partial_search_results"] is False
    assert call["options"] == {
        "request_timeout": 0.25,
        "max_retries": 0,
        "retry_on_status": (),
        "retry_on_timeout": False,
    }
    assert call["size"] == 1
    assert call["timeout"] == "250ms"
    assert call["track_total_hits"] is False
    assert call["source"] == [
        "resource_id",
        "source_id",
        "authoritative_locator",
        "tenant_id",
        "projection_version",
        "acl",
        "provenance",
    ]
    assert call["query"]["bool"]["filter"] == [  # type: ignore[index]
        {"terms": {"tenant_id": ["tenant-a", "tenant-b"]}},
        {"terms": {"acl": ["group:research", "user:alice"]}},
    ]
    assert call["sort"] == [
        {"_score": "desc"},
        {"resource_id": "asc"},
        {"source_id": "asc"},
        {"tenant_id": "asc"},
    ]


def test_elasticsearch_absolute_deadline_bounds_pre_dispatch_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = iter((100.0, 100.1, 100.1))
    monkeypatch.setattr(elasticsearch_module, "monotonic", lambda: next(observed))
    client = RecordingClient()
    driver = ElasticsearchDriver(client, index="af-search")

    assert driver.search(_query(deadline_ms=250)) == ()

    call = client.search_calls[0]
    assert call["timeout"] == "150ms"
    assert call["options"] == {
        "request_timeout": 0.15,
        "max_retries": 0,
        "retry_on_status": (),
        "retry_on_timeout": False,
    }


def test_elasticsearch_rejects_success_returned_after_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = iter((100.0, 100.0, 100.002))
    monkeypatch.setattr(elasticsearch_module, "monotonic", lambda: next(observed))
    client = RecordingClient()
    driver = ElasticsearchDriver(client, index="af-search")

    with pytest.raises(ElasticsearchOperationError) as captured:
        driver.search(_query(deadline_ms=1))

    assert captured.value.reason is PlatformReason.CANCELLED


def test_elasticsearch_rejects_backend_policy_leaks_and_unknown_filters() -> None:
    client = RecordingClient()
    leaked = _resource("resource-a", tenant_id="tenant-private", acl=("group:private",))
    client.search_response = {
        "timed_out": False,
        "_shards": _complete_shards(),
        "hits": {
            "hits": [
                {
                    "_id": _document_id(leaked),
                    "_score": 100.0,
                    "_source": _hit_source(leaked),
                }
            ]
        },
    }
    driver = ElasticsearchDriver(client, index="af-search")

    with pytest.raises(ElasticsearchOperationError) as captured:
        driver.search(_query())

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert "tenant-private" not in str(captured.value)
    query = SearchQuery(
        text="energy",
        filters=(
            SearchFilter("tenant_id", ("tenant-a",)),
            SearchFilter("acl", ("group:research",)),
            SearchFilter("language", ("en",)),
        ),
    )
    with pytest.raises(ElasticsearchOperationError) as unsupported:
        driver.search(query)
    assert unsupported.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE
    assert len(client.search_calls) == 1


def test_elasticsearch_treats_hostile_query_text_only_as_match_data() -> None:
    client = RecordingClient()
    driver = ElasticsearchDriver(client, index="af-search")
    query = SearchQuery(
        text='{"query_string":{"query":"tenant_id:*"}} OR *:*',
        filters=(
            SearchFilter("tenant_id", ("tenant-a",)),
            SearchFilter("acl", ("group:research",)),
        ),
    )

    assert driver.search(query) == ()

    call = client.search_calls[0]
    query_body = call["query"]
    assert type(query_body) is dict
    assert query_body["bool"]["must"] == [
        {
            "match": {
                "text": {
                    "query": '{"query_string":{"query":"tenant_id:*"}} OR *:*',
                    "operator": "and",
                }
            }
        }
    ]
    assert "query_string" not in query_body["bool"]
    assert "post_filter" not in call


def test_elasticsearch_chunks_bulk_writes_by_encoded_bytes() -> None:
    client = RecordingClient()
    driver = ElasticsearchDriver(client, index="af-search")
    text = "energy " + "x" * (1_048_576 - len("energy "))
    resources = tuple(_resource(f"resource-{index}", text=text) for index in range(9))

    assert driver.upsert(resources) == 9

    assert len(client.bulk_calls) == 2
    assert [len(call["operations"]) for call in client.bulk_calls] == [16, 2]  # type: ignore[arg-type]


def test_elasticsearch_rejects_projection_versions_outside_backend_long() -> None:
    client = RecordingClient()
    driver = ElasticsearchDriver(client, index="af-search")

    with pytest.raises(ElasticsearchOperationError) as captured:
        driver.upsert((_resource("resource-a", projection_version=2**63),))

    assert captured.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE
    assert client.bulk_calls == []


def test_elasticsearch_bulk_partial_failure_is_exhaustive_and_sanitized() -> None:
    client = RecordingClient()
    first = _resource("resource-a")
    second = _resource("resource-b")
    client.bulk_response = {
        "errors": True,
        "items": [
            {"index": {"_id": _document_id(first), "status": 201}},
            {
                "index": {
                    "_id": _document_id(second),
                    "status": 429,
                    "error": {"reason": "private-index-secret"},
                }
            },
        ],
    }
    driver = ElasticsearchDriver(client, index="af-search")

    with pytest.raises(ElasticsearchOperationError) as captured:
        driver.upsert((first, second))

    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert "private-index-secret" not in str(captured.value)
    assert "private-index-secret" not in repr(captured.value)

    client.bulk_response = {"errors": False, "items": []}
    with pytest.raises(ElasticsearchOperationError) as malformed:
        driver.upsert((first,))
    assert malformed.value.reason is PlatformReason.INTEGRITY_FAILURE


def test_elasticsearch_delete_is_tenant_scoped_and_idempotent() -> None:
    client = RecordingClient()
    resource = _resource("resource-a", tenant_id="tenant-b")
    expected_id = _document_id(resource)
    client.delete_response = {"_id": expected_id, "result": "deleted"}
    driver = ElasticsearchDriver(client, index="af-search", request_timeout=3.0)

    assert driver.delete(
        tenant_id="tenant-b",
        source_id="source-primary",
        resource_id="resource-a",
    )
    assert client.delete_calls == [
        {
            "index": "af-search",
            "id": expected_id,
            "refresh": "wait_for",
            "options": {
                "request_timeout": 3.0,
                "ignore_status": (404,),
                "max_retries": 0,
                "retry_on_status": (),
                "retry_on_timeout": False,
            },
        }
    ]

    client.delete_response = {"_id": expected_id, "result": "not_found"}
    assert not driver.delete(
        tenant_id="tenant-b",
        source_id="source-primary",
        resource_id="resource-a",
    )


def test_elasticsearch_timeouts_cancellation_and_shutdown_are_honest() -> None:
    client = RecordingClient()
    client.search_response = {
        "timed_out": True,
        "_shards": _complete_shards(),
        "hits": {"hits": []},
    }
    driver = ElasticsearchDriver(client, index="af-search")

    with pytest.raises(ElasticsearchOperationError) as timed_out:
        driver.search(_query())
    assert timed_out.value.reason is PlatformReason.CANCELLED

    with pytest.raises(ElasticsearchOperationError) as unsupported:
        driver.cancel()
    assert unsupported.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE

    driver.close()
    driver.close()
    assert client.closed == 1
    with pytest.raises(ElasticsearchOperationError) as closed:
        driver.upsert((_resource("resource-a"),))
    assert closed.value.reason is PlatformReason.SHUTDOWN_FAILED


@pytest.mark.parametrize(
    "mapping",
    [
        {},
        {"af-search": {"mappings": {}}},
        {
            "af-search": {
                "mappings": {
                    **elasticsearch_index_body()["mappings"],
                    "dynamic": True,
                }
            }
        },
        {
            "first": {"mappings": elasticsearch_index_body()["mappings"]},
            "second": {"mappings": elasticsearch_index_body()["mappings"]},
        },
    ],
)
def test_elasticsearch_requires_one_preexisting_strict_af_mapping(mapping: object) -> None:
    client = RecordingClient()
    client.indices.mapping = mapping

    with pytest.raises(ElasticsearchOperationError) as captured:
        ElasticsearchDriver(client, index="af-search")

    assert captured.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE


@pytest.mark.parametrize(
    "info",
    [
        {},
        {
            "version": {
                "number": "8.19.6",
                "build_flavor": "default",
                "build_snapshot": False,
            },
            "tagline": "You Know, for Search",
        },
        {
            "version": {
                "number": "9.4.4",
                "build_flavor": "serverless",
                "build_snapshot": False,
            },
            "tagline": "You Know, for Search",
        },
        {
            "version": {
                "number": "9.4.4",
                "build_flavor": "default",
                "build_snapshot": True,
            },
            "tagline": "You Know, for Search",
        },
        {
            "version": {
                "number": "private\nsecret",
                "build_flavor": "default",
                "build_snapshot": False,
            },
            "tagline": "You Know, for Search",
        },
        {
            "version": {
                "number": "9.4.4",
                "build_flavor": "default",
                "build_snapshot": False,
            },
            "tagline": "private product",
        },
    ],
)
def test_elasticsearch_rejects_unverified_service_identity(info: Mapping[str, object]) -> None:
    client = RecordingClient()
    client.info_response = info

    with pytest.raises(ElasticsearchOperationError) as captured:
        ElasticsearchDriver(client, index="af-search")

    assert captured.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE


class _TransportError(RuntimeError):
    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__("private-transport-secret")


class _UnverifiedStringStatusError(RuntimeError):
    status_code = "N/A"


class _Meta:
    def __init__(self, status: int) -> None:
        self.status = status


class _ObjectApiResponse(Mapping[object, object]):
    def __init__(self, body: Mapping[object, object], *, status: int = 200) -> None:
        self.body = body
        self.meta = _Meta(status)

    def __getitem__(self, key: object) -> object:
        return self.body[key]

    def __iter__(self):
        return iter(self.body)

    def __len__(self) -> int:
        return len(self.body)


_ElasticsearchConnectionError = type(
    "ConnectionError",
    (RuntimeError,),
    {"__module__": "elastic_transport", "status_code": "N/A"},
)
_ElasticsearchConnectionTimeout = type(
    "ConnectionTimeout",
    (RuntimeError,),
    {"__module__": "elastic_transport", "status_code": "TIMEOUT"},
)
_ElasticsearchSSLError = type(
    "SSLError",
    (_ElasticsearchConnectionError,),
    {"__module__": "elastic_transport", "status_code": "N/A"},
)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, PlatformReason.AUTHORIZATION_DENIED),
        (404, PlatformReason.CAPABILITY_UNAVAILABLE),
        (429, PlatformReason.TRANSIENT_FAILURE),
        (503, PlatformReason.TRANSIENT_FAILURE),
        (400, PlatformReason.PERMANENT_FAILURE),
        (None, PlatformReason.PERMANENT_FAILURE),
    ],
)
def test_elasticsearch_connection_failures_are_typed_and_sanitized(
    status: int | None,
    reason: PlatformReason,
) -> None:
    client = RecordingClient()
    client.info_error = _TransportError(status)

    with pytest.raises(ElasticsearchOperationError) as captured:
        ElasticsearchDriver(client, index="af-search")

    assert captured.value.reason is reason
    assert "private-transport-secret" not in str(captured.value)
    assert "private-transport-secret" not in repr(captured.value)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (_ElasticsearchConnectionError("private-network-secret"), PlatformReason.TRANSIENT_FAILURE),
        (
            _ElasticsearchConnectionTimeout("private-timeout-secret"),
            PlatformReason.TRANSIENT_FAILURE,
        ),
        (_ElasticsearchSSLError("private-tls-secret"), PlatformReason.PERMANENT_FAILURE),
    ],
)
def test_elasticsearch_official_connection_failures_are_typed_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    reason: PlatformReason,
) -> None:
    monkeypatch.setattr(
        elasticsearch_module,
        "_transport_error_types",
        lambda: (
            (_ElasticsearchSSLError,),
            (_ElasticsearchConnectionTimeout,),
            (_ElasticsearchConnectionError,),
        ),
    )
    client = RecordingClient()
    client.info_error = error

    with pytest.raises(ElasticsearchOperationError) as captured:
        ElasticsearchDriver(client, index="af-search")

    assert captured.value.reason is reason
    assert "private" not in str(captured.value)
    assert "private" not in repr(captured.value)


def test_elasticsearch_unknown_string_status_fails_closed() -> None:
    client = RecordingClient()
    client.info_error = _UnverifiedStringStatusError("private-spoofed-secret")

    with pytest.raises(ElasticsearchOperationError) as captured:
        ElasticsearchDriver(client, index="af-search")

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert "private-spoofed-secret" not in str(captured.value)


def test_elasticsearch_unsupported_product_is_permanent_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnsupportedProductError(RuntimeError):
        status_code = 503

    monkeypatch.setattr(
        elasticsearch_module,
        "_unsupported_product_error_types",
        lambda: (UnsupportedProductError,),
    )
    client = RecordingClient()
    client.info_error = UnsupportedProductError("private-product-secret")

    with pytest.raises(ElasticsearchOperationError) as captured:
        ElasticsearchDriver(client, index="af-search")

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert "private-product-secret" not in str(captured.value)


def test_elasticsearch_validates_official_object_api_response_metadata() -> None:
    client = RecordingClient()
    resource = _resource("resource-a")
    document_id = _document_id(resource)
    client.info_response = _ObjectApiResponse(client.info_response)
    client.indices.mapping = _ObjectApiResponse(client.indices.mapping)
    client.bulk_response = _ObjectApiResponse(
        {
            "errors": False,
            "items": [{"index": {"_id": document_id, "status": 201}}],
        }
    )
    client.search_response = _ObjectApiResponse(
        {
            "timed_out": False,
            "_shards": _complete_shards(),
            "hits": {"hits": []},
        }
    )
    client.delete_response = _ObjectApiResponse(
        {"_id": document_id, "result": "not_found"},
        status=404,
    )
    driver = ElasticsearchDriver(client, index="af-search")

    assert driver.upsert((resource,)) == 1
    assert driver.search(_query()) == ()
    assert not driver.delete(
        tenant_id=resource.tenant_id,
        source_id=resource.source_id,
        resource_id=resource.resource_id,
    )

    client.search_response = _ObjectApiResponse(
        {
            "timed_out": False,
            "_shards": _complete_shards(),
            "hits": {"hits": []},
        },
        status=206,
    )
    with pytest.raises(ElasticsearchOperationError) as captured:
        driver.search(_query())
    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE


def test_elasticsearch_response_and_status_interruption_propagates() -> None:
    class InterruptedResponse:
        @property
        def body(self) -> object:
            raise KeyboardInterrupt

    class InterruptedStatus(Exception):
        @property
        def status_code(self) -> object:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        elasticsearch_module._mapping(InterruptedResponse())
    with pytest.raises(KeyboardInterrupt):
        elasticsearch_module._operation_reason(InterruptedStatus())


def test_elasticsearch_query_transport_timeout_is_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        elasticsearch_module,
        "_transport_error_types",
        lambda: (
            (_ElasticsearchSSLError,),
            (_ElasticsearchConnectionTimeout,),
            (_ElasticsearchConnectionError,),
        ),
    )
    client = RecordingClient()
    driver = ElasticsearchDriver(client, index="af-search")
    client.search_error = _ElasticsearchConnectionTimeout("private-timeout-secret")

    with pytest.raises(ElasticsearchOperationError) as captured:
        driver.search(_query())

    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert "private-timeout-secret" not in str(captured.value)


def test_elasticsearch_mapping_transport_failure_is_sanitized() -> None:
    client = RecordingClient()
    client.indices.error = _TransportError(503)

    with pytest.raises(ElasticsearchOperationError) as captured:
        ElasticsearchDriver(client, index="af-search")

    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE


@pytest.mark.parametrize(
    ("index", "request_timeout"),
    [
        ("AF-SEARCH", 5.0),
        ("af/search", 5.0),
        ("af-search", True),
        ("af-search", 0.0),
        ("af-search", 61.0),
    ],
)
def test_elasticsearch_rejects_invalid_local_configuration(
    index: str,
    request_timeout: object,
) -> None:
    with pytest.raises(ValueError):
        ElasticsearchDriver(
            RecordingClient(),
            index=index,
            request_timeout=request_timeout,  # type: ignore[arg-type]
        )


def test_elasticsearch_rejects_invalid_resource_operations_before_transport() -> None:
    client = RecordingClient()
    resource = _resource("resource-a")
    driver = ElasticsearchDriver(client, index="af-search")

    with pytest.raises(ValueError):
        driver.upsert(())
    with pytest.raises(ValueError):
        driver.upsert((resource, resource))
    with pytest.raises(TypeError):
        driver.search(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        driver.delete(tenant_id="", source_id="source-primary", resource_id="resource-a")

    assert client.bulk_calls == []
    assert client.search_calls == []
    assert client.delete_calls == []


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            {
                "errors": False,
                "items": [{"index": {"_id": "wrong", "status": 201}}],
            },
            PlatformReason.INTEGRITY_FAILURE,
        ),
        (
            {
                "errors": False,
                "items": [{"delete": {"_id": "wrong", "status": 201}}],
            },
            PlatformReason.INTEGRITY_FAILURE,
        ),
        (
            {
                "errors": False,
                "items": [{"index": {"_id": "wrong", "status": True}}],
            },
            PlatformReason.INTEGRITY_FAILURE,
        ),
        (
            {
                "errors": False,
                "items": [{"index": {"_id": "unused", "status": 400}}],
            },
            PlatformReason.INTEGRITY_FAILURE,
        ),
        (
            {
                "errors": True,
                "items": [{"index": {"_id": "unused", "status": 700}}],
            },
            PlatformReason.INTEGRITY_FAILURE,
        ),
    ],
)
def test_elasticsearch_rejects_corrupt_bulk_acknowledgements(
    response: object,
    reason: PlatformReason,
) -> None:
    client = RecordingClient()
    resource = _resource("resource-a")
    if (
        type(response) is dict
        and type(response.get("items")) is list
        and response["items"]
        and type(response["items"][0]) is dict
        and type(response["items"][0].get("index")) is dict
        and response["items"][0]["index"].get("_id") == "unused"
    ):
        response["items"][0]["index"]["_id"] = _document_id(resource)
    client.bulk_response = response
    driver = ElasticsearchDriver(client, index="af-search")

    with pytest.raises(ElasticsearchOperationError) as captured:
        driver.upsert((resource,))

    assert captured.value.reason is reason


def test_elasticsearch_permanent_bulk_partial_failure_is_sanitized() -> None:
    client = RecordingClient()
    resource = _resource("resource-a")
    client.bulk_response = {
        "errors": True,
        "items": [
            {
                "index": {
                    "_id": _document_id(resource),
                    "status": 400,
                    "error": {"reason": "private-permanent-secret"},
                }
            }
        ],
    }
    driver = ElasticsearchDriver(client, index="af-search")

    with pytest.raises(ElasticsearchOperationError) as captured:
        driver.upsert((resource,))

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert "private-permanent-secret" not in str(captured.value)


def test_elasticsearch_operation_transport_failures_are_sanitized() -> None:
    client = RecordingClient()
    driver = ElasticsearchDriver(client, index="af-search")
    resource = _resource("resource-a")

    client.bulk_error = _TransportError(503)
    with pytest.raises(ElasticsearchOperationError) as bulk:
        driver.upsert((resource,))
    assert bulk.value.reason is PlatformReason.TRANSIENT_FAILURE
    client.bulk_error = None

    client.search_error = _TransportError(403)
    with pytest.raises(ElasticsearchOperationError) as search:
        driver.search(_query())
    assert search.value.reason is PlatformReason.AUTHORIZATION_DENIED
    client.search_error = None

    client.delete_error = _TransportError(404)
    with pytest.raises(ElasticsearchOperationError) as delete:
        driver.delete(
            tenant_id="tenant-a",
            source_id="source-primary",
            resource_id="resource-a",
        )
    assert delete.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE


def test_elasticsearch_query_deadline_bounds_lock_queueing() -> None:
    class BlockingClient(RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.entered = Event()
            self.release = Event()

        def bulk(
            self,
            *,
            operations: object,
            index: object = None,
            refresh: object = None,
        ) -> object:
            self.entered.set()
            self.release.wait()
            return super().bulk(operations=operations, index=index, refresh=refresh)

    client = BlockingClient()
    driver = ElasticsearchDriver(client, index="af-search")
    worker_errors: list[BaseException] = []

    def write() -> None:
        try:
            driver.upsert((_resource("resource-blocking"),))
        except BaseException as error:
            worker_errors.append(error)

    worker = Thread(target=write)
    worker.start()
    try:
        assert client.entered.wait(timeout=1.0)
        with pytest.raises(ElasticsearchOperationError) as captured:
            driver.search(_query(deadline_ms=10))
        assert captured.value.reason is PlatformReason.CANCELLED
        assert client.search_calls == []
    finally:
        client.release.set()
        worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert worker_errors == []


def test_elasticsearch_query_releases_lock_after_post_acquisition_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RecordingClient()
    driver = ElasticsearchDriver(client, index="af-search")
    holder_ready = Event()
    release_holder = Event()

    def hold_lock() -> None:
        with driver._lock:
            holder_ready.set()
            release_holder.wait()

    holder = Thread(target=hold_lock)
    holder.start()
    assert holder_ready.wait(timeout=1.0)
    clock_calls = 0

    def interrupt_after_acquisition() -> float:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 2:
            release_holder.set()
            return 100.0
        if clock_calls == 3:
            raise KeyboardInterrupt
        return 100.0

    monkeypatch.setattr(elasticsearch_module, "monotonic", interrupt_after_acquisition)
    try:
        with pytest.raises(KeyboardInterrupt):
            driver.search(_query(deadline_ms=1_000))
    finally:
        release_holder.set()
        holder.join(timeout=2.0)

    assert not holder.is_alive()
    probe_acquired = Event()

    def probe_lock() -> None:
        with driver._lock:
            probe_acquired.set()

    probe = Thread(target=probe_lock, daemon=True)
    probe.start()
    probe.join(timeout=1.0)
    stranded = probe.is_alive()
    if stranded:
        driver._lock.release()
        probe.join(timeout=1.0)
    assert not stranded
    assert probe_acquired.is_set()


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {
            "timed_out": False,
            "_shards": {"total": 1, "successful": 0, "skipped": 0, "failed": 1},
            "hits": {"hits": []},
        },
        {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": []}},
        {
            "timed_out": False,
            "_shards": {"total": 2, "successful": 1, "skipped": 0, "failed": 0},
            "hits": {"hits": []},
        },
        {
            "timed_out": False,
            "_shards": {"total": 1, "successful": 1, "skipped": 2, "failed": 0},
            "hits": {"hits": []},
        },
        {
            "timed_out": False,
            "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
            "hits": {"hits": {}},
        },
        {
            "timed_out": False,
            "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
            "hits": {"hits": [None] * 21},
        },
    ],
)
def test_elasticsearch_rejects_incomplete_or_partial_search_responses(
    response: object,
) -> None:
    client = RecordingClient()
    client.search_response = response
    driver = ElasticsearchDriver(client, index="af-search")

    with pytest.raises(ElasticsearchOperationError) as captured:
        driver.search(_query())

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE


def test_elasticsearch_rejects_hits_outside_requested_deterministic_order() -> None:
    client = RecordingClient()
    first = _resource("resource-a")
    second = _resource("resource-b")
    client.search_response = {
        "timed_out": False,
        "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
        "hits": {
            "hits": [
                {
                    "_id": _document_id(first),
                    "_score": 1.0,
                    "_source": _hit_source(first),
                },
                {
                    "_id": _document_id(second),
                    "_score": 2.0,
                    "_source": _hit_source(second),
                },
            ]
        },
    }
    driver = ElasticsearchDriver(client, index="af-search")

    with pytest.raises(ElasticsearchOperationError) as captured:
        driver.search(_query())

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE


@pytest.mark.parametrize(
    "mutation",
    [
        "shape",
        "id",
        "acl",
        "provenance",
        "projection",
        "locator",
        "score",
        "duplicate",
    ],
)
def test_elasticsearch_rejects_corrupt_hits(mutation: str) -> None:
    client = RecordingClient()
    resource = _resource("resource-a")
    hit: dict[str, object] = {
        "_id": _document_id(resource),
        "_score": 1.0,
        "_source": _hit_source(resource),
    }
    raw_hits: list[object] = [hit]
    if mutation == "shape":
        raw_hits[0] = None
    elif mutation == "id":
        hit["_id"] = "wrong"
    elif mutation == "acl":
        hit["_source"]["acl"] = ["group:private"]  # type: ignore[index]
    elif mutation == "provenance":
        hit["_source"]["provenance"] = ["private-secret"]  # type: ignore[index]
    elif mutation == "projection":
        hit["_source"]["projection_version"] = True  # type: ignore[index]
    elif mutation == "locator":
        hit["_source"]["authoritative_locator"] = ""  # type: ignore[index]
    elif mutation == "score":
        hit["_score"] = float("nan")
    elif mutation == "duplicate":
        raw_hits.append(dict(hit))
    client.search_response = {
        "timed_out": False,
        "_shards": _complete_shards(),
        "hits": {"hits": raw_hits},
    }
    driver = ElasticsearchDriver(client, index="af-search")

    with pytest.raises(ElasticsearchOperationError) as captured:
        driver.search(_query())

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert "private-secret" not in str(captured.value)


def test_elasticsearch_rejects_corrupt_delete_acknowledgement() -> None:
    client = RecordingClient()
    client.delete_response = {"_id": "wrong", "result": "deleted"}
    driver = ElasticsearchDriver(client, index="af-search")

    with pytest.raises(ElasticsearchOperationError) as captured:
        driver.delete(
            tenant_id="tenant-a",
            source_id="source-primary",
            resource_id="resource-a",
        )

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE


def test_elasticsearch_close_failure_preserves_cancellation() -> None:
    client = RecordingClient()
    driver = ElasticsearchDriver(client, index="af-search")
    client.close_error = _TransportError(500)
    cancellation = ElasticsearchOperationError(
        PlatformReason.CANCELLED,
        stage="search.test.cancel",
    )

    with pytest.raises(ElasticsearchOperationError) as captured, driver:
        raise cancellation

    assert captured.value is cancellation
    client.close_error = None
    driver.close()
    with pytest.raises(ElasticsearchOperationError):
        driver.__enter__()
