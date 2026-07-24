from __future__ import annotations

import hashlib
from collections.abc import Mapping

import pytest

from af.framework.errors import PlatformReason
from af.search.contracts import (
    ResourceEnvelope,
    ResourceKind,
    SearchFilter,
    SearchQuery,
)
from af.search.opensearch import (
    OpenSearchDriver,
    OpenSearchOperationError,
    opensearch_index_body,
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
    def __init__(self) -> None:
        self.mapping: object = {"af-search": {"mappings": opensearch_index_body()["mappings"]}}
        self.error: Exception | None = None

    def get_mapping(
        self,
        *,
        index: object,
        params: object = None,
        headers: object = None,
    ) -> object:
        del index, params, headers
        if self.error is not None:
            raise self.error
        return self.mapping


class RecordingClient:
    def __init__(self) -> None:
        self.indices = _Indices()
        self.info_response: object = {"version": {"distribution": "opensearch", "number": "3.7.0"}}
        self.bulk_response: object | None = None
        self.search_response: object = {
            "timed_out": False,
            "_shards": {"failed": 0},
            "hits": {"hits": []},
        }
        self.delete_response: object = {"_id": "unused", "result": "not_found"}
        self.bulk_calls: list[tuple[object, object, object]] = []
        self.search_calls: list[tuple[object, object, object]] = []
        self.delete_calls: list[tuple[object, object, object]] = []
        self.closed = 0
        self.info_error: Exception | None = None
        self.bulk_error: Exception | None = None
        self.search_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.close_error: Exception | None = None

    def info(self, *, params: object = None, headers: object = None) -> object:
        del params, headers
        if self.info_error is not None:
            raise self.info_error
        return self.info_response

    def bulk(
        self,
        *,
        body: object,
        index: object = None,
        params: object = None,
        headers: object = None,
    ) -> object:
        del headers
        self.bulk_calls.append((index, body, params))
        if self.bulk_error is not None:
            raise self.bulk_error
        if self.bulk_response is not None:
            return self.bulk_response
        assert type(body) is tuple
        return {
            "errors": False,
            "items": [
                {"index": {"_id": action["index"]["_id"], "status": 201}} for action in body[::2]
            ],
        }

    def search(
        self,
        *,
        body: object = None,
        index: object = None,
        params: object = None,
        headers: object = None,
    ) -> object:
        del headers
        self.search_calls.append((index, body, params))
        if self.search_error is not None:
            raise self.search_error
        return self.search_response

    def delete(
        self,
        *,
        index: object,
        id: object,
        params: object = None,
        headers: object = None,
    ) -> object:
        del headers
        self.delete_calls.append((index, id, params))
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


def test_opensearch_bulk_write_uses_stable_identity_and_canonical_document() -> None:
    client = RecordingClient()
    resource = _resource("resource-a")
    driver = OpenSearchDriver(client, index="af-search", request_timeout=5.0)

    assert driver.upsert((resource,)) == 1

    index, body, params = client.bulk_calls[0]
    assert index == "af-search"
    assert params == {"refresh": "wait_for", "request_timeout": 5.0}
    assert body == (
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


def test_opensearch_pushes_tenant_and_acl_filters_before_top_k() -> None:
    client = RecordingClient()
    resource = _resource("resource-a")
    client.search_response = {
        "timed_out": False,
        "_shards": {"failed": 0},
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
    driver = OpenSearchDriver(client, index="af-search", request_timeout=5.0)

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
    index, body, params = client.search_calls[0]
    assert index == "af-search"
    assert params == {
        "allow_partial_search_results": "false",
        "request_timeout": 0.25,
    }
    assert type(body) is dict
    assert body["size"] == 1
    assert body["timeout"] == "250ms"
    assert body["track_total_hits"] is False
    assert body["_source"] == [
        "resource_id",
        "source_id",
        "authoritative_locator",
        "tenant_id",
        "projection_version",
        "acl",
        "provenance",
    ]
    assert body["query"]["bool"]["filter"] == [
        {"terms": {"tenant_id": ["tenant-a", "tenant-b"]}},
        {"terms": {"acl": ["group:research", "user:alice"]}},
    ]
    assert body["sort"] == [
        {"_score": "desc"},
        {"resource_id": "asc"},
        {"source_id": "asc"},
        {"tenant_id": "asc"},
    ]


def test_opensearch_rejects_backend_policy_leaks_and_unknown_filters() -> None:
    client = RecordingClient()
    leaked = _resource("resource-a", tenant_id="tenant-private", acl=("group:private",))
    client.search_response = {
        "timed_out": False,
        "_shards": {"failed": 0},
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
    driver = OpenSearchDriver(client, index="af-search")

    with pytest.raises(OpenSearchOperationError) as captured:
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
    with pytest.raises(OpenSearchOperationError) as unsupported:
        driver.search(query)
    assert unsupported.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE
    assert len(client.search_calls) == 1


def test_opensearch_treats_hostile_query_text_only_as_match_data() -> None:
    client = RecordingClient()
    driver = OpenSearchDriver(client, index="af-search")
    query = SearchQuery(
        text='{"query_string":{"query":"tenant_id:*"}} OR *:*',
        filters=(
            SearchFilter("tenant_id", ("tenant-a",)),
            SearchFilter("acl", ("group:research",)),
        ),
    )

    assert driver.search(query) == ()

    _, body, _ = client.search_calls[0]
    assert type(body) is dict
    assert body["query"]["bool"]["must"] == [
        {
            "match": {
                "text": {
                    "query": '{"query_string":{"query":"tenant_id:*"}} OR *:*',
                    "operator": "and",
                }
            }
        }
    ]
    assert "query_string" not in body["query"]["bool"]
    assert "post_filter" not in body


def test_opensearch_chunks_bulk_writes_by_encoded_bytes() -> None:
    client = RecordingClient()
    driver = OpenSearchDriver(client, index="af-search")
    text = "energy " + "x" * (1_048_576 - len("energy "))
    resources = tuple(_resource(f"resource-{index}", text=text) for index in range(9))

    assert driver.upsert(resources) == 9

    assert len(client.bulk_calls) == 2
    assert [len(call[1]) for call in client.bulk_calls] == [16, 2]


def test_opensearch_rejects_projection_versions_outside_backend_long() -> None:
    client = RecordingClient()
    driver = OpenSearchDriver(client, index="af-search")

    with pytest.raises(OpenSearchOperationError) as captured:
        driver.upsert((_resource("resource-a", projection_version=2**63),))

    assert captured.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE
    assert client.bulk_calls == []


def test_opensearch_bulk_partial_failure_is_exhaustive_and_sanitized() -> None:
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
    driver = OpenSearchDriver(client, index="af-search")

    with pytest.raises(OpenSearchOperationError) as captured:
        driver.upsert((first, second))

    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert "private-index-secret" not in str(captured.value)
    assert "private-index-secret" not in repr(captured.value)

    client.bulk_response = {"errors": False, "items": []}
    with pytest.raises(OpenSearchOperationError) as malformed:
        driver.upsert((first,))
    assert malformed.value.reason is PlatformReason.INTEGRITY_FAILURE


def test_opensearch_delete_is_tenant_scoped_and_idempotent() -> None:
    client = RecordingClient()
    resource = _resource("resource-a", tenant_id="tenant-b")
    expected_id = _document_id(resource)
    client.delete_response = {"_id": expected_id, "result": "deleted"}
    driver = OpenSearchDriver(client, index="af-search", request_timeout=3.0)

    assert driver.delete(
        tenant_id="tenant-b",
        source_id="source-primary",
        resource_id="resource-a",
    )
    assert client.delete_calls == [
        (
            "af-search",
            expected_id,
            {"ignore": 404, "refresh": "wait_for", "request_timeout": 3.0},
        )
    ]

    client.delete_response = {"_id": expected_id, "result": "not_found"}
    assert not driver.delete(
        tenant_id="tenant-b",
        source_id="source-primary",
        resource_id="resource-a",
    )


def test_opensearch_timeouts_cancellation_and_shutdown_are_honest() -> None:
    client = RecordingClient()
    client.search_response = {
        "timed_out": True,
        "_shards": {"failed": 0},
        "hits": {"hits": []},
    }
    driver = OpenSearchDriver(client, index="af-search")

    with pytest.raises(OpenSearchOperationError) as timed_out:
        driver.search(_query())
    assert timed_out.value.reason is PlatformReason.CANCELLED

    with pytest.raises(OpenSearchOperationError) as unsupported:
        driver.cancel()
    assert unsupported.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE

    driver.close()
    driver.close()
    assert client.closed == 1
    with pytest.raises(OpenSearchOperationError) as closed:
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
                    **opensearch_index_body()["mappings"],
                    "dynamic": True,
                }
            }
        },
        {
            "first": {"mappings": opensearch_index_body()["mappings"]},
            "second": {"mappings": opensearch_index_body()["mappings"]},
        },
    ],
)
def test_opensearch_requires_one_preexisting_strict_af_mapping(mapping: object) -> None:
    client = RecordingClient()
    client.indices.mapping = mapping

    with pytest.raises(OpenSearchOperationError) as captured:
        OpenSearchDriver(client, index="af-search")

    assert captured.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE


@pytest.mark.parametrize(
    "info",
    [
        {},
        {"version": {"distribution": "elasticsearch", "number": "9.1.0"}},
        {"version": {"distribution": "opensearch", "number": "2.19.6"}},
        {"version": {"distribution": "opensearch", "number": "private\nsecret"}},
    ],
)
def test_opensearch_rejects_unverified_service_identity(info: Mapping[str, object]) -> None:
    client = RecordingClient()
    client.info_response = info

    with pytest.raises(OpenSearchOperationError) as captured:
        OpenSearchDriver(client, index="af-search")

    assert captured.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE


class _TransportError(RuntimeError):
    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__("private-transport-secret")


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
def test_opensearch_connection_failures_are_typed_and_sanitized(
    status: int | None,
    reason: PlatformReason,
) -> None:
    client = RecordingClient()
    client.info_error = _TransportError(status)

    with pytest.raises(OpenSearchOperationError) as captured:
        OpenSearchDriver(client, index="af-search")

    assert captured.value.reason is reason
    assert "private-transport-secret" not in str(captured.value)
    assert "private-transport-secret" not in repr(captured.value)


def test_opensearch_mapping_transport_failure_is_sanitized() -> None:
    client = RecordingClient()
    client.indices.error = _TransportError(503)

    with pytest.raises(OpenSearchOperationError) as captured:
        OpenSearchDriver(client, index="af-search")

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
def test_opensearch_rejects_invalid_local_configuration(
    index: str,
    request_timeout: object,
) -> None:
    with pytest.raises(ValueError):
        OpenSearchDriver(
            RecordingClient(),
            index=index,
            request_timeout=request_timeout,  # type: ignore[arg-type]
        )


def test_opensearch_rejects_invalid_resource_operations_before_transport() -> None:
    client = RecordingClient()
    resource = _resource("resource-a")
    driver = OpenSearchDriver(client, index="af-search")

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
    ],
)
def test_opensearch_rejects_corrupt_bulk_acknowledgements(
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
    driver = OpenSearchDriver(client, index="af-search")

    with pytest.raises(OpenSearchOperationError) as captured:
        driver.upsert((resource,))

    assert captured.value.reason is reason


def test_opensearch_permanent_bulk_partial_failure_is_sanitized() -> None:
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
    driver = OpenSearchDriver(client, index="af-search")

    with pytest.raises(OpenSearchOperationError) as captured:
        driver.upsert((resource,))

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert "private-permanent-secret" not in str(captured.value)


def test_opensearch_operation_transport_failures_are_sanitized() -> None:
    client = RecordingClient()
    driver = OpenSearchDriver(client, index="af-search")
    resource = _resource("resource-a")

    client.bulk_error = _TransportError(503)
    with pytest.raises(OpenSearchOperationError) as bulk:
        driver.upsert((resource,))
    assert bulk.value.reason is PlatformReason.TRANSIENT_FAILURE
    client.bulk_error = None

    client.search_error = _TransportError(403)
    with pytest.raises(OpenSearchOperationError) as search:
        driver.search(_query())
    assert search.value.reason is PlatformReason.AUTHORIZATION_DENIED
    client.search_error = None

    client.delete_error = _TransportError(404)
    with pytest.raises(OpenSearchOperationError) as delete:
        driver.delete(
            tenant_id="tenant-a",
            source_id="source-primary",
            resource_id="resource-a",
        )
    assert delete.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"timed_out": False, "_shards": {"failed": 1}, "hits": {"hits": []}},
        {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": {}}},
        {
            "timed_out": False,
            "_shards": {"failed": 0},
            "hits": {"hits": [None] * 21},
        },
    ],
)
def test_opensearch_rejects_incomplete_or_partial_search_responses(
    response: object,
) -> None:
    client = RecordingClient()
    client.search_response = response
    driver = OpenSearchDriver(client, index="af-search")

    with pytest.raises(OpenSearchOperationError) as captured:
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
def test_opensearch_rejects_corrupt_hits(mutation: str) -> None:
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
        "_shards": {"failed": 0},
        "hits": {"hits": raw_hits},
    }
    driver = OpenSearchDriver(client, index="af-search")

    with pytest.raises(OpenSearchOperationError) as captured:
        driver.search(_query())

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert "private-secret" not in str(captured.value)


def test_opensearch_rejects_corrupt_delete_acknowledgement() -> None:
    client = RecordingClient()
    client.delete_response = {"_id": "wrong", "result": "deleted"}
    driver = OpenSearchDriver(client, index="af-search")

    with pytest.raises(OpenSearchOperationError) as captured:
        driver.delete(
            tenant_id="tenant-a",
            source_id="source-primary",
            resource_id="resource-a",
        )

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE


def test_opensearch_close_failure_preserves_cancellation() -> None:
    client = RecordingClient()
    driver = OpenSearchDriver(client, index="af-search")
    client.close_error = _TransportError(500)
    cancellation = OpenSearchOperationError(
        PlatformReason.CANCELLED,
        stage="search.test.cancel",
    )

    with pytest.raises(OpenSearchOperationError) as captured, driver:
        raise cancellation

    assert captured.value is cancellation
    client.close_error = None
    driver.close()
    with pytest.raises(OpenSearchOperationError):
        driver.__enter__()
