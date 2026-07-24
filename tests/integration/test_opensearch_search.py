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
from af.search.contracts import ResourceEnvelope, ResourceKind, SearchFilter, SearchQuery
from af.search.manager import SearchDriver, SearchFeature
from af.search.opensearch import (
    OpenSearchDriver,
    OpenSearchOperationError,
    opensearch_index_body,
)
from af.search.opensearch_manager import opensearch_registration
from af.testing import ConformanceEvidence, RealServiceHook, SearchDriverConformanceSuite


def _url() -> str:
    value = os.environ.get("AF_OPENSEARCH_TEST_URL")
    if value is None:
        pytest.skip("AF_OPENSEARCH_TEST_URL is not configured")
    return value


def _expected_version() -> str:
    value = os.environ.get("AF_OPENSEARCH_TEST_VERSION")
    if value is None:
        pytest.skip("AF_OPENSEARCH_TEST_VERSION is not configured")
    return value


def _module() -> ModuleType:
    return importlib.import_module("opensearchpy")


def _client() -> object:
    url = _url()
    constructor = _module().OpenSearch
    return constructor(
        [url],
        http_compress=True,
        timeout=5.0,
        max_retries=0,
        retry_on_timeout=False,
    )


def _index(prefix: str) -> str:
    return f"af-{prefix}-{uuid4().hex}"


def _create(index: str) -> None:
    client = _client()
    try:
        client.indices.create(  # type: ignore[attr-defined]
            index=index,
            body=opensearch_index_body(),
            params={"wait_for_active_shards": "1"},
        )
    finally:
        client.close()  # type: ignore[attr-defined]


def _drop(index: str) -> None:
    client = _client()
    try:
        client.indices.delete(  # type: ignore[attr-defined]
            index=index,
            params={"ignore": 404},
        )
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


def test_opensearch_passes_shared_real_service_suite() -> None:
    # Load the optional client before the process-state guard so one-time dependency
    # warning registration is not misattributed to an individual driver operation.
    _url()
    _module()

    @contextmanager
    def fixture() -> Iterator[SearchDriver]:
        index = _index("conformance")
        _create(index)
        client = _client()
        driver = OpenSearchDriver(client, index=index, request_timeout=5.0)
        try:
            yield driver
        finally:
            driver.close()
            _drop(index)

    report = SearchDriverConformanceSuite().run_real(
        RealServiceHook("opensearch-3-7", fixture, enabled=True)
    )

    assert report.evidence is ConformanceEvidence.REAL_SERVICE
    assert report.integration == "opensearch-3-7"


def test_opensearch_3_7_registration_binds_client_and_service_identity() -> None:
    index = _index("registration")
    _create(index)
    observed_at = datetime(2026, 7, 24, 16, tzinfo=UTC)
    registration = opensearch_registration(clock=lambda: observed_at)
    ref = ConnectionRef("search", "primary", "opensearch")
    driver = registration.factory(
        ref,
        {
            "driver": "opensearch",
            "url": _url(),
            "index": index,
            "request_timeout": 5.0,
        },
    )
    binding = CapabilityBinding(
        driver_version=registration.manifest.version,
        artifact_digest=registration.manifest.artifact_digest,
        dependency_graph_digest=registration.manifest.dependency_graph_digest,
        configuration_digest=hashlib.sha256(b"opensearch-real-config").hexdigest(),
        deployment_profile="integration",
        policy_version="policy-v1",
    )
    try:
        evidence = registration.verifier(ref, driver, binding)
        assert isinstance(driver, OpenSearchDriver)
        assert driver.server_version == _expected_version() == "3.7.0"
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


def test_opensearch_real_bulk_partial_failure_never_reports_full_success() -> None:
    index = _index("partial")
    pipeline = _index("reject")
    admin = _client()
    body = opensearch_index_body()
    body["settings"] = {"index.default_pipeline": pipeline}
    try:
        admin.ingest.put_pipeline(  # type: ignore[attr-defined]
            id=pipeline,
            body={
                "processors": [
                    {
                        "set": {
                            "if": "ctx.resource_id == 'resource-b'",
                            "field": "projection_version",
                            "value": "private-opensearch-rejection",
                        }
                    }
                ]
            },
        )
        admin.indices.create(  # type: ignore[attr-defined]
            index=index,
            body=body,
            params={"wait_for_active_shards": "1"},
        )
    finally:
        admin.close()  # type: ignore[attr-defined]

    driver = OpenSearchDriver(_client(), index=index)
    try:
        with pytest.raises(OpenSearchOperationError) as captured:
            driver.upsert((_resource("resource-a"), _resource("resource-b")))

        assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
        assert "private-opensearch-rejection" not in str(captured.value)
        assert [hit.resource_id for hit in driver.search(_query())] == ["resource-a"]
    finally:
        driver.close()
        cleanup = _client()
        try:
            cleanup.indices.delete(  # type: ignore[attr-defined]
                index=index,
                params={"ignore": 404},
            )
            cleanup.ingest.delete_pipeline(  # type: ignore[attr-defined]
                id=pipeline,
                params={"ignore": 404},
            )
        finally:
            cleanup.close()  # type: ignore[attr-defined]
