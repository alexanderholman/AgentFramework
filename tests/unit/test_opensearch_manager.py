from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from af.search.opensearch import OpenSearchDriver, opensearch_index_body
from af.search.opensearch_manager import opensearch_registration

from af.framework.managers import CapabilityBinding, ConnectionRef, HealthStatus
from af.search import opensearch_manager as manager_module
from af.search.manager import SearchFeature

_NOW = datetime(2026, 7, 24, 16, tzinfo=UTC)


class _Indices:
    def get_mapping(self, **kwargs: object) -> object:
        del kwargs
        return {"af-search": {"mappings": opensearch_index_body()["mappings"]}}


class _Client:
    def __init__(self) -> None:
        self.indices = _Indices()
        self.closed = False

    def info(self, **kwargs: object) -> object:
        del kwargs
        return {"version": {"distribution": "opensearch", "number": "3.7.0"}}

    def bulk(self, **kwargs: object) -> object:
        del kwargs
        raise AssertionError("manager test must not write")

    def search(self, **kwargs: object) -> object:
        del kwargs
        raise AssertionError("manager test must not search")

    def delete(self, **kwargs: object) -> object:
        del kwargs
        raise AssertionError("manager test must not delete")

    def close(self) -> None:
        self.closed = True


def _registration(monkeypatch: pytest.MonkeyPatch):
    client = _Client()
    monkeypatch.setattr(
        manager_module,
        "_runtime_identity",
        lambda: ("3.2.0", "a" * 64, "b" * 64),
    )
    monkeypatch.setattr(
        manager_module,
        "_client",
        lambda url, *, request_timeout: client,
    )
    return opensearch_registration(clock=lambda: _NOW), client


def test_opensearch_registration_is_inert_and_binds_real_service_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration, client = _registration(monkeypatch)
    ref = ConnectionRef("search", "primary", "opensearch")
    driver = registration.factory(
        ref,
        {
            "driver": "opensearch",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
            "request_timeout": 2.0,
        },
    )
    binding = CapabilityBinding(
        driver_version="3.2.0",
        artifact_digest="a" * 64,
        dependency_graph_digest="b" * 64,
        configuration_digest=hashlib.sha256(b"config").hexdigest(),
        deployment_profile="integration",
        policy_version="policy-v1",
    )

    evidence = registration.verifier(ref, driver, binding)

    assert isinstance(driver, OpenSearchDriver)
    assert driver.server_version == "3.7.0"
    assert evidence.binding == binding
    assert evidence.health is HealthStatus.HEALTHY
    assert evidence.observed_at == _NOW
    assert evidence.capabilities.supports(
        {
            SearchFeature.POLICY_PUSHDOWN.value,
            SearchFeature.FULL_TEXT.value,
            SearchFeature.INDEXING.value,
            SearchFeature.TOMBSTONES.value,
            SearchFeature.PERSISTENCE.value,
        }
    )
    assert not evidence.capabilities.supports(
        {
            SearchFeature.CANCELLATION.value,
            SearchFeature.HIGHLIGHTING.value,
        }
    )
    assert registration.manifest.capabilities == evidence.capabilities
    disposer = registration.disposer
    assert disposer is not None
    disposer(driver)
    assert client.closed


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"driver": "opensearch", "url": "http://127.0.0.1:9200"},
        {
            "driver": "embedded",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
        },
        {
            "driver": "opensearch",
            "url": "http://search.example:9200",
            "index": "af-search",
        },
        {
            "driver": "opensearch",
            "url": "http://127.0.0.1:9200/private",
            "index": "af-search",
        },
        {
            "driver": "opensearch",
            "url": "http://127.0.0.1:9200",
            "index": "AF-SEARCH",
        },
        {
            "driver": "opensearch",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
            "request_timeout": True,
        },
        {
            "driver": "opensearch",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
            "unknown": "private-secret",
        },
    ],
)
def test_opensearch_registration_rejects_unsafe_configuration(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, object],
) -> None:
    registration, _ = _registration(monkeypatch)
    ref = ConnectionRef("search", "primary", "opensearch")

    with pytest.raises(ValueError):
        registration.factory(ref, config)


def test_opensearch_registration_requires_aware_evidence_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    monkeypatch.setattr(
        manager_module,
        "_runtime_identity",
        lambda: ("3.2.0", "a" * 64, "b" * 64),
    )
    monkeypatch.setattr(
        manager_module,
        "_client",
        lambda url, *, request_timeout: client,
    )
    registration = opensearch_registration(clock=lambda: datetime(2026, 7, 24))
    ref = ConnectionRef("search", "primary", "opensearch")
    driver = registration.factory(
        ref,
        {
            "driver": "opensearch",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
        },
    )
    binding = CapabilityBinding(
        driver_version="3.2.0",
        artifact_digest="a" * 64,
        dependency_graph_digest="b" * 64,
        configuration_digest="c" * 64,
        deployment_profile="test",
        policy_version="policy-v1",
    )

    with pytest.raises(ValueError):
        registration.verifier(ref, driver, binding)

    driver.close()
