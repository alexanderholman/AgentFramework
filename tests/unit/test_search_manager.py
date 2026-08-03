from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from af.framework.application import Application
from af.framework.container import Container, ProviderRegistrar, ServiceProvider
from af.framework.errors import PlatformReason
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    CapabilityValidationError,
    DriverManifest,
    DriverRegistration,
    DriverResolutionError,
    HealthStatus,
    ManagerConfigurationError,
)
from af.search.contracts import ResourceEnvelope, ResourceKind, SearchFilter, SearchHit, SearchQuery
from af.search.embedded_manager import embedded_search_registration
from af.search.manager import (
    SearchDriver,
    SearchFeature,
    SearchManager,
    bind_search_manager,
)

_NOW = datetime(2026, 7, 24, 13, tzinfo=UTC)


def _resource(resource_id: str, *, tenant_id: str = "tenant-a") -> ResourceEnvelope:
    return ResourceEnvelope(
        resource_id=resource_id,
        kind=ResourceKind.TEXT,
        source_id="source-primary",
        authoritative_locator=f"af://source-primary/{resource_id}",
        tenant_id=tenant_id,
        projection_version=1,
        acl=("group:research",),
        provenance=("sha256:" + "a" * 64,),
        text="bounded searchable energy content",
    )


def _query(*, tenant_id: str = "tenant-a") -> SearchQuery:
    return SearchQuery(
        text="energy",
        filters=(
            SearchFilter("tenant_id", (tenant_id,)),
            SearchFilter("acl", ("group:research",)),
        ),
    )


def _manager(database: Path) -> SearchManager:
    manager = SearchManager(
        connections={
            "primary": {
                "driver": "embedded",
                "database": str(database),
            }
        },
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(embedded_search_registration(clock=lambda: _NOW))
    return manager


def test_search_manager_routes_index_query_and_tombstone_operations(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "search.sqlite3")
    resource = _resource("resource-a")

    assert manager.upsert((resource,)) == 1
    assert manager.search(_query())[0].resource_id == "resource-a"
    assert manager.delete(
        tenant_id="tenant-a",
        source_id="source-primary",
        resource_id="resource-a",
    )
    assert manager.search(_query()) == ()
    assert manager.connection(requires=(SearchFeature.POLICY_PUSHDOWN,)) is manager.connection()

    manager.close()

    with pytest.raises(DriverResolutionError):
        manager.search(_query())


def test_search_manager_isolates_named_connections(tmp_path: Path) -> None:
    manager = SearchManager(
        connections={
            "alpha": {"driver": "embedded", "database": str(tmp_path / "alpha.sqlite3")},
            "beta": {"driver": "embedded", "database": str(tmp_path / "beta.sqlite3")},
        },
        default="alpha",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(embedded_search_registration(clock=lambda: _NOW))

    manager.upsert((_resource("resource-a"),), name="alpha")
    manager.upsert((_resource("resource-b"),), name="beta")

    assert [hit.resource_id for hit in manager.search(_query(), name="alpha")] == ["resource-a"]
    assert [hit.resource_id for hit in manager.search(_query(), name="beta")] == ["resource-b"]

    manager.close()


def test_search_manager_rejects_untargeted_registration(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "search.sqlite3")
    registration = embedded_search_registration(clock=lambda: _NOW)
    untargeted = DriverRegistration(
        manager_id="database",
        driver_name=registration.driver_name,
        factory=registration.factory,
        manifest=registration.manifest,
        verifier=registration.verifier,
        disposer=registration.disposer,
    )

    with pytest.raises(ManagerConfigurationError):
        manager.install(untargeted)

    manager.close()


@pytest.mark.parametrize(
    ("operation", "required"),
    [
        ("search", SearchFeature.POLICY_PUSHDOWN),
        ("search", SearchFeature.FULL_TEXT),
        ("upsert", SearchFeature.INDEXING),
        ("delete", SearchFeature.TOMBSTONES),
    ],
)
def test_search_manager_operations_require_verified_capabilities(
    tmp_path: Path,
    operation: str,
    required: SearchFeature,
) -> None:
    manager = _manager(tmp_path / "search.sqlite3")

    assert manager.connection(requires=(required,)) is manager.connection()
    assert getattr(manager, operation)
    manager.close()


def test_search_manager_preserves_generic_capability_inputs(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "search.sqlite3")

    driver = manager.connection(requires=CapabilitySet((SearchFeature.FULL_TEXT.value,)))
    assert manager.connection(requires=SearchFeature.INDEXING.value) is driver
    with pytest.raises(ValueError):
        manager.connection(requires=(object(),))  # type: ignore[arg-type]
    manager.close()


def test_search_manager_is_application_owned_and_snapshots_configuration(
    tmp_path: Path,
) -> None:
    connections = {
        "primary": {
            "driver": "embedded",
            "database": str(tmp_path / "search.sqlite3"),
        }
    }

    class SearchProvider(ServiceProvider):
        provider_id = "search"

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_search_manager(
                registrar,
                connections=connections,
                default="primary",
                deployment_profile="test",
                policy_version="policy-v1",
                registrations=(embedded_search_registration(clock=lambda: _NOW),),
                clock=lambda: _NOW,
            )

    application = Application(Container(), (SearchProvider(),))
    application.register()
    connections["primary"]["database"] = ""
    application.boot()
    application.serve()
    manager = application.container.resolve(SearchManager)
    manager.upsert((_resource("resource-a"),))

    application.shutdown()

    with pytest.raises(DriverResolutionError) as captured:
        manager.search(_query())
    assert captured.value.reason is PlatformReason.DEPENDENCY_RESOLUTION_FAILED


def test_search_provider_rejects_missing_or_duplicate_registrations() -> None:
    class Registrar:
        def bind(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            del args, kwargs

    registration = embedded_search_registration(clock=lambda: _NOW)
    common = {
        "connections": {"primary": {"driver": "embedded", "database": ":memory:"}},
        "default": "primary",
        "deployment_profile": "test",
        "policy_version": "policy-v1",
    }
    with pytest.raises(ManagerConfigurationError):
        bind_search_manager(
            Registrar(),
            registrations=(),
            **common,  # type: ignore[arg-type]
        )
    with pytest.raises(ManagerConfigurationError):
        bind_search_manager(
            Registrar(),  # type: ignore[arg-type]
            registrations=(registration, registration),
            **common,
        )


@pytest.mark.parametrize(
    "connection",
    [
        {"driver": "embedded"},
        {"driver": "embedded", "database": ":memory:", "extra": True},
        {"driver": "other", "database": ":memory:"},
    ],
)
def test_embedded_registration_rejects_invalid_configuration(
    connection: dict[str, object],
) -> None:
    manager = SearchManager(
        connections={"primary": connection},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(embedded_search_registration(clock=lambda: _NOW))

    with pytest.raises(DriverResolutionError):
        manager.connection()
    manager.close()


def test_embedded_registration_rejects_invalid_runtime_evidence_time(
    tmp_path: Path,
) -> None:
    manager = SearchManager(
        connections={
            "primary": {
                "driver": "embedded",
                "database": str(tmp_path / "search.sqlite3"),
            }
        },
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(embedded_search_registration(clock=lambda: datetime(2026, 7, 24)))

    with pytest.raises(CapabilityValidationError):
        manager.connection()
    manager.close()


def test_search_manager_refuses_query_to_source_without_policy_pushdown(
    tmp_path: Path,
) -> None:
    """A source lacking POLICY_PUSHDOWN is ineligible: the query never reaches the driver."""

    search_was_called = [False]

    class _PolicyAbsentDriver:
        def upsert(self, resources: tuple[ResourceEnvelope, ...], /) -> int:
            return len(resources)

        def search(self, query: SearchQuery, /) -> tuple[SearchHit, ...]:
            search_was_called[0] = True
            return ()

        def delete(self, *, tenant_id: str, source_id: str, resource_id: str) -> bool:
            return False

        def cancel(self) -> None:
            pass

        def close(self) -> None:
            pass

    _artifact = hashlib.sha256(b"policy-absent-artifact").hexdigest()
    _deps = hashlib.sha256(b"policy-absent-deps").hexdigest()
    _limited_caps = CapabilitySet([SearchFeature.FULL_TEXT.value])

    def _factory(ref: object, config: object) -> SearchDriver:
        return _PolicyAbsentDriver()

    def _verifier(
        ref: object,
        driver: object,
        expected_binding: CapabilityBinding,
    ) -> CapabilityEvidence:
        payload = {
            "caps": tuple(_limited_caps),
            "version": expected_binding.driver_version,
        }
        evidence_digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        return CapabilityEvidence(
            binding=expected_binding,
            capabilities=_limited_caps,
            health=HealthStatus.HEALTHY,
            observed_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
            evidence_digest=evidence_digest,
        )

    limited_manifest = DriverManifest(
        driver_name="policy-absent",
        version="1.0.0",
        artifact_digest=_artifact,
        dependency_graph_digest=_deps,
        capabilities=_limited_caps,
    )
    registration = DriverRegistration(
        manager_id="search",
        driver_name="policy-absent",
        factory=_factory,
        manifest=limited_manifest,
        verifier=_verifier,
        disposer=None,
    )

    manager = SearchManager(
        connections={"primary": {"driver": "policy-absent"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(registration)

    with pytest.raises(CapabilityValidationError):
        manager.search(_query())

    assert not search_was_called[0], "driver.search() must not be called for an ineligible source"
    manager.close()
