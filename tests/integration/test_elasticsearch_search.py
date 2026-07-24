from __future__ import annotations

import hashlib
import importlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import ModuleType
from uuid import uuid4

import pytest

from af.framework.errors import PlatformReason
from af.framework.managers import CapabilityBinding, ConnectionRef, HealthStatus
from af.search import elasticsearch as elasticsearch_module
from af.search.contracts import ResourceEnvelope, ResourceKind, SearchFilter, SearchQuery
from af.search.elasticsearch import (
    ElasticsearchDriver,
    ElasticsearchOperationError,
    elasticsearch_index_body,
)
from af.search.elasticsearch_manager import elasticsearch_registration
from af.search.manager import SearchDriver, SearchFeature
from af.testing import ConformanceEvidence, RealServiceHook, SearchDriverConformanceSuite


def _url() -> str:
    value = os.environ.get("AF_ELASTICSEARCH_TEST_URL")
    if value is None:
        pytest.skip("AF_ELASTICSEARCH_TEST_URL is not configured")
    return value


def _expected_version() -> str:
    value = os.environ.get("AF_ELASTICSEARCH_TEST_VERSION")
    if value is None:
        pytest.skip("AF_ELASTICSEARCH_TEST_VERSION is not configured")
    return value


def _module() -> ModuleType:
    return importlib.import_module("elasticsearch")


def _client() -> object:
    url = _url()
    constructor = _module().Elasticsearch
    return constructor(
        url,
        http_compress=True,
        max_retries=0,
        request_timeout=5.0,
        retry_on_status=(),
        retry_on_timeout=False,
        server_mode="stack",
    )


def _index(prefix: str) -> str:
    return f"af-{prefix}-{uuid4().hex}"


def _create(index: str) -> None:
    client = _client()
    try:
        mappings = elasticsearch_index_body()["mappings"]
        client.indices.create(  # type: ignore[attr-defined]
            index=index,
            mappings=mappings,
            wait_for_active_shards="1",
        )
    finally:
        client.close()  # type: ignore[attr-defined]


def _drop(index: str) -> None:
    client = _client()
    try:
        client.options(ignore_status=(404,)).indices.delete(index=index)  # type: ignore[attr-defined]
    finally:
        client.close()  # type: ignore[attr-defined]


def _resource(resource_id: str) -> ResourceEnvelope:
    return ResourceEnvelope(
        resource_id=resource_id,
        kind=ResourceKind.TEXT,
        source_id="source-primary",
        authoritative_locator=f"af://source-primary/{resource_id}",
        tenant_id="tenant-a",
        projection_version=1,
        acl=("group:research",),
        provenance=("sha256:" + "a" * 64,),
        text="bounded searchable energy content",
    )


def _query() -> SearchQuery:
    return SearchQuery(
        text="energy",
        filters=(
            SearchFilter("tenant_id", ("tenant-a",)),
            SearchFilter("acl", ("group:research",)),
        ),
    )


def test_elasticsearch_passes_shared_real_service_suite() -> None:
    # Load the optional client before the process-state guard so one-time dependency
    # warning registration is not misattributed to an individual driver operation.
    _url()
    _module()

    @contextmanager
    def fixture() -> Iterator[SearchDriver]:
        index = _index("conformance")
        _create(index)
        client = _client()
        driver = ElasticsearchDriver(client, index=index, request_timeout=5.0)
        try:
            yield driver
        finally:
            driver.close()
            _drop(index)

    report = SearchDriverConformanceSuite().run_real(
        RealServiceHook("elasticsearch-9-4", fixture, enabled=True)
    )

    assert report.evidence is ConformanceEvidence.REAL_SERVICE
    assert report.integration == "elasticsearch-9-4"


def test_elasticsearch_9_4_registration_binds_client_and_service_identity() -> None:
    index = _index("registration")
    _create(index)
    observed_at = datetime(2026, 7, 24, 16, tzinfo=UTC)
    registration = elasticsearch_registration(clock=lambda: observed_at)
    ref = ConnectionRef("search", "primary", "elasticsearch")
    driver = registration.factory(
        ref,
        {
            "driver": "elasticsearch",
            "url": _url(),
            "index": index,
            "request_timeout": 5.0,
        },
    )
    binding = CapabilityBinding(
        driver_version=registration.manifest.version,
        artifact_digest=registration.manifest.artifact_digest,
        dependency_graph_digest=registration.manifest.dependency_graph_digest,
        configuration_digest=hashlib.sha256(b"elasticsearch-real-config").hexdigest(),
        deployment_profile="integration",
        policy_version="policy-v1",
    )
    try:
        evidence = registration.verifier(ref, driver, binding)
        assert isinstance(driver, ElasticsearchDriver)
        assert driver.server_version == _expected_version() == "9.4.4"
        assert evidence.binding == binding
        assert evidence.health is HealthStatus.HEALTHY
        assert evidence.observed_at == observed_at
        assert evidence.capabilities.supports(
            {
                SearchFeature.POLICY_PUSHDOWN.value,
                SearchFeature.FULL_TEXT.value,
                SearchFeature.INDEXING.value,
                SearchFeature.TOMBSTONES.value,
                SearchFeature.PERSISTENCE.value,
            }
        )
        assert not evidence.capabilities.supports((SearchFeature.CANCELLATION.value,))
    finally:
        disposer = registration.disposer
        if disposer is not None:
            disposer(driver)
        else:
            driver.close()
        _drop(index)


def test_elasticsearch_projection_persists_across_client_restart() -> None:
    index = _index("persistence")
    _create(index)
    try:
        first = ElasticsearchDriver(_client(), index=index)
        try:
            assert first.upsert((_resource("resource-persistent"),)) == 1
        finally:
            first.close()

        reopened = ElasticsearchDriver(_client(), index=index)
        try:
            hits = reopened.search(_query())
            assert [hit.resource_id for hit in hits] == ["resource-persistent"]
        finally:
            reopened.close()
    finally:
        _drop(index)


def test_elasticsearch_certified_cluster_does_not_recreate_a_deleted_index() -> None:
    index = _index("no-autocreate")
    _create(index)
    driver = ElasticsearchDriver(_client(), index=index)
    _drop(index)
    try:
        with pytest.raises(ElasticsearchOperationError) as captured:
            driver.upsert((_resource("resource-after-delete"),))

        assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
        probe = _client()
        try:
            assert not probe.indices.exists(index=index)  # type: ignore[attr-defined]
        finally:
            probe.close()  # type: ignore[attr-defined]
    finally:
        driver.close()
        _drop(index)


def test_elasticsearch_9_4_uses_pinned_client_exception_types() -> None:
    _url()
    exceptions = importlib.import_module("elasticsearch")
    ssl_errors, timeout_errors, connection_errors = elasticsearch_module._transport_error_types()

    assert ssl_errors == (exceptions.SSLError,)
    assert timeout_errors == (exceptions.ConnectionTimeout,)
    assert connection_errors == (exceptions.ConnectionError,)
    assert (
        elasticsearch_module._operation_reason(exceptions.SSLError("private-tls-secret"))
        is PlatformReason.PERMANENT_FAILURE
    )
    assert (
        elasticsearch_module._operation_reason(
            exceptions.ConnectionTimeout("private-timeout-secret")
        )
        is PlatformReason.TRANSIENT_FAILURE
    )
    assert (
        elasticsearch_module._operation_reason(exceptions.ConnectionError("private-network-secret"))
        is PlatformReason.TRANSIENT_FAILURE
    )


def test_elasticsearch_real_bulk_partial_failure_never_reports_full_success() -> None:
    index = _index("partial")
    pipeline = _index("reject")
    admin = _client()
    mappings = elasticsearch_index_body()["mappings"]
    try:
        admin.ingest.put_pipeline(  # type: ignore[attr-defined]
            id=pipeline,
            body={
                "processors": [
                    {
                        "set": {
                            "if": "ctx.resource_id == 'resource-b'",
                            "field": "projection_version",
                            "value": "private-elasticsearch-rejection",
                        }
                    }
                ]
            },
        )
        admin.indices.create(  # type: ignore[attr-defined]
            index=index,
            mappings=mappings,
            settings={"index.default_pipeline": pipeline},
            wait_for_active_shards="1",
        )
    finally:
        admin.close()  # type: ignore[attr-defined]

    driver = ElasticsearchDriver(_client(), index=index)
    try:
        with pytest.raises(ElasticsearchOperationError) as captured:
            driver.upsert((_resource("resource-a"), _resource("resource-b")))

        assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
        assert "private-elasticsearch-rejection" not in str(captured.value)
        assert [hit.resource_id for hit in driver.search(_query())] == ["resource-a"]
    finally:
        driver.close()
        cleanup = _client()
        try:
            cleanup.options(ignore_status=(404,)).indices.delete(  # type: ignore[attr-defined]
                index=index
            )
            cleanup.options(ignore_status=(404,)).ingest.delete_pipeline(  # type: ignore[attr-defined]
                id=pipeline
            )
        finally:
            cleanup.close()  # type: ignore[attr-defined]
