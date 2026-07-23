from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

import pytest

from af.framework.errors import PlatformReason
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    CapabilityValidationError,
    ConnectionQuarantinedError,
    ConnectionRef,
    DriverManifest,
    DriverResolutionError,
    DriverTarget,
    FailoverDriver,
    FallbackDriver,
    HealthStatus,
    LoadBalancingDriver,
    Manager,
    ManagerConfigurationError,
)

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_NOW = datetime(2026, 7, 23, 20, tzinfo=UTC)


@dataclass(slots=True)
class _Driver:
    identity: int


def _manifest(
    driver_name: str = "remote",
    *,
    capabilities: tuple[str, ...] = ("transactions",),
) -> DriverManifest:
    return DriverManifest(
        driver_name=driver_name,
        version="1.0.0",
        artifact_digest=_DIGEST_A,
        dependency_graph_digest=_DIGEST_B,
        capabilities=CapabilitySet(capabilities),
    )


def _evidence(
    binding,
    *,
    capabilities: tuple[str, ...] = ("transactions",),
    health: HealthStatus = HealthStatus.HEALTHY,
    observed_at: datetime = _NOW,
    expires_at: datetime = _NOW + timedelta(minutes=5),
    revoked: bool = False,
    evidence_digest: str = _DIGEST_C,
) -> CapabilityEvidence:
    return CapabilityEvidence(
        binding=binding,
        capabilities=CapabilitySet(capabilities),
        health=health,
        observed_at=observed_at,
        expires_at=expires_at,
        evidence_digest=evidence_digest,
        revoked=revoked,
    )


def _manager(
    *,
    connections=None,
    default: str | None = "primary",
    clock=None,
) -> Manager[_Driver]:
    return Manager(
        manager_id="database",
        default=default,
        connections=connections
        or {
            "primary": {"driver": "remote", "dsn": "postgresql://primary"},
            "analytics": {"driver": "remote", "dsn": "postgresql://analytics"},
        },
        deployment_profile="production",
        policy_version="policy-v1",
        clock=clock or (lambda: _NOW),
    )


def _register_healthy(
    manager: Manager[_Driver],
    *,
    factory=None,
    verifier=None,
    disposer=None,
) -> None:
    manager.extend(
        "remote",
        factory or (lambda _ref, _config: _Driver(1)),
        manifest=_manifest(),
        verifier=verifier or (lambda _ref, _driver, binding: _evidence(binding)),
        disposer=disposer,
    )


def test_default_and_named_connections_are_lazy_deterministic_and_cached() -> None:
    manager = _manager()
    created: list[tuple[ConnectionRef, object]] = []

    def factory(ref, config):
        created.append((ref, config))
        return _Driver(len(created))

    _register_healthy(manager, factory=factory)

    assert created == []
    primary = manager.connection(requires=("transactions",))
    assert manager.connection("primary", requires=("transactions",)) is primary
    analytics = manager.connection("analytics", requires=("transactions",))
    assert analytics is manager.connection("analytics")
    assert analytics is not primary
    assert [ref.connection_name for ref, _ in created] == ["primary", "analytics"]
    assert manager.resolved_connections == ("analytics", "primary")


@pytest.mark.parametrize(
    ("manager", "name", "reason"),
    [
        (_manager(default=None), None, PlatformReason.CONFIGURATION_INVALID),
        (_manager(), "missing", PlatformReason.DEPENDENCY_RESOLUTION_FAILED),
        (
            _manager(connections={"primary": {"driver": "unknown"}}),
            None,
            PlatformReason.DEPENDENCY_RESOLUTION_FAILED,
        ),
    ],
)
def test_missing_default_unknown_connection_and_unknown_driver_fail_closed(
    manager: Manager[_Driver],
    name: str | None,
    reason: PlatformReason,
) -> None:
    with pytest.raises((ManagerConfigurationError, DriverResolutionError)) as captured:
        manager.connection(name)

    assert captured.value.reason is reason
    assert manager.resolved_connections == ()


@pytest.mark.parametrize(
    "connections",
    [
        {"primary": "not-a-mapping"},
        {"primary": {}},
        {"primary": {"driver": ""}},
        {"primary": {"driver": "remote", "timeout": float("nan")}},
        {"primary": {"driver": "remote", "secret": object()}},
    ],
)
def test_invalid_connection_configuration_is_rejected_before_factory_use(connections) -> None:
    with pytest.raises(ManagerConfigurationError) as captured:
        _manager(connections=connections)

    assert captured.value.reason is PlatformReason.CONFIGURATION_INVALID
    assert "secret" not in str(captured.value)


def test_provider_extension_requires_manifest_and_independent_verifier() -> None:
    manager = _manager()

    with pytest.raises(ManagerConfigurationError):
        manager.extend(
            "remote",
            lambda _ref, _config: _Driver(1),
            manifest=_manifest("different"),
            verifier=lambda _ref, _driver, binding: _evidence(binding),
        )

    _register_healthy(manager)
    with pytest.raises(ManagerConfigurationError):
        _register_healthy(manager)


def test_missing_untrusted_stale_revoked_or_unhealthy_capability_evidence_fails_closed() -> None:
    variants = (
        _evidence,
        lambda binding: _evidence(binding, capabilities=()),
        lambda binding: _evidence(
            binding,
            observed_at=_NOW - timedelta(minutes=10),
            expires_at=_NOW - timedelta(minutes=1),
        ),
        lambda binding: _evidence(binding, revoked=True),
        lambda binding: _evidence(binding, health=HealthStatus.UNHEALTHY),
        lambda binding: _evidence(
            type(binding)(
                driver_version=binding.driver_version,
                artifact_digest=_DIGEST_C,
                dependency_graph_digest=binding.dependency_graph_digest,
                configuration_digest=binding.configuration_digest,
                deployment_profile=binding.deployment_profile,
                policy_version=binding.policy_version,
            )
        ),
    )

    for index, evidence_factory in enumerate(variants[1:], start=1):
        manager = _manager()
        disposed: list[int] = []
        _register_healthy(
            manager,
            factory=lambda _ref, _config, current=index: _Driver(current),
            verifier=lambda _ref, _driver, binding, make=evidence_factory: make(binding),
            disposer=lambda driver, sink=disposed: sink.append(driver.identity),
        )

        with pytest.raises(CapabilityValidationError) as captured:
            manager.connection(requires=("transactions",))

        assert captured.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE
        assert manager.resolved_connections == ()
        assert disposed == [index]


def test_runtime_capability_change_quarantines_cached_connection_until_explicit_purge() -> None:
    manager = _manager()
    evidence = []
    disposed: list[int] = []
    _register_healthy(
        manager,
        verifier=lambda _ref, _driver, _binding: evidence[-1],
        disposer=lambda driver: disposed.append(driver.identity),
    )
    evidence.append(_evidence(manager.binding("primary")))

    driver = manager.connection(requires=("transactions",))
    assert driver.identity == 1

    evidence.append(
        _evidence(
            manager.binding("primary"),
            capabilities=(),
            evidence_digest=_DIGEST_A,
        )
    )
    with pytest.raises(ConnectionQuarantinedError) as captured:
        manager.connection(requires=("transactions",))
    assert captured.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE
    assert manager.quarantined_connections == ("primary",)
    assert disposed == [1]

    with pytest.raises(ConnectionQuarantinedError):
        manager.connection()

    manager.purge("primary")
    assert manager.quarantined_connections == ()
    with pytest.raises(CapabilityValidationError):
        manager.connection(requires=("transactions",))


def test_cached_evidence_is_revalidated_and_safe_renewal_does_not_replace_driver() -> None:
    now = [_NOW]
    manager = _manager(clock=lambda: now[-1])
    evidence = []
    disposed: list[int] = []
    _register_healthy(
        manager,
        verifier=lambda _ref, _driver, _binding: evidence[-1],
        disposer=lambda driver: disposed.append(driver.identity),
    )
    evidence.append(_evidence(manager.binding("primary")))
    driver = manager.connection()

    evidence.append(
        _evidence(
            manager.binding("primary"),
            expires_at=_NOW + timedelta(minutes=10),
            evidence_digest="d" * 64,
        )
    )
    assert manager.connection() is driver
    assert disposed == []

    now.append(_NOW + timedelta(minutes=11))
    with pytest.raises(ConnectionQuarantinedError):
        manager.connection()
    assert disposed == [1]
    assert manager.quarantined_connections == ("primary",)


def test_configuration_is_snapshotted_and_factory_failures_are_sanitized() -> None:
    original = {
        "primary": {
            "driver": "remote",
            "options": {"application_name": "af"},
        }
    }
    manager = _manager(connections=original)
    original["primary"]["driver"] = "weaker-local"
    original["primary"]["options"]["application_name"] = "changed"

    def factory(_ref, config):
        assert config["driver"] == "remote"
        assert config["options"]["application_name"] == "af"
        raise RuntimeError("dsn=password-secret")

    _register_healthy(manager, factory=factory)
    with pytest.raises(DriverResolutionError) as captured:
        manager.connection()

    assert captured.value.reason is PlatformReason.DEPENDENCY_RESOLUTION_FAILED
    assert isinstance(captured.value.protected_cause, RuntimeError)
    assert "password-secret" not in str(captured.value)


def test_cancellation_from_factory_or_verifier_is_never_sanitized() -> None:
    class _Cancelled(BaseException):
        pass

    manager = _manager()
    _register_healthy(
        manager,
        factory=lambda _ref, _config: (_ for _ in ()).throw(_Cancelled()),
    )
    with pytest.raises(_Cancelled):
        manager.connection()

    manager = _manager()
    _register_healthy(
        manager,
        verifier=lambda _ref, _driver, _binding: (_ for _ in ()).throw(_Cancelled()),
    )
    with pytest.raises(_Cancelled):
        manager.connection()


def test_concurrent_resolution_constructs_and_caches_exactly_once() -> None:
    manager = _manager()
    guard = Lock()
    count = 0

    def factory(_ref, _config):
        nonlocal count
        with guard:
            count += 1
            identity = count
        return _Driver(identity)

    _register_healthy(manager, factory=factory)
    with ThreadPoolExecutor(max_workers=8) as executor:
        resolved = list(executor.map(lambda _index: manager.connection(), range(32)))

    assert count == 1
    assert all(driver is resolved[0] for driver in resolved)


def test_explicit_composites_never_silently_change_backend_and_retain_evidence() -> None:
    primary = DriverTarget(
        ConnectionRef("database", "primary", "remote"),
        _Driver(1),
    )
    secondary = DriverTarget(
        ConnectionRef("database", "secondary", "remote"),
        _Driver(2),
    )
    local = DriverTarget(
        ConnectionRef("database", "local", "sqlite"),
        _Driver(3),
    )

    fallback = FallbackDriver(primary, local, clock=lambda: _NOW)
    assert fallback.current is primary
    assert fallback.evidence == ()
    assert fallback.activate_fallback("operator_approved_degradation") is local
    assert fallback.current is local
    assert fallback.evidence[0].reason_code == "operator_approved_degradation"

    failover = FailoverDriver((primary, secondary), clock=lambda: _NOW)
    assert failover.current is primary
    assert failover.failover("health_probe_failed") is secondary
    assert failover.evidence[0].from_connection == primary.ref
    assert failover.evidence[0].to_connection == secondary.ref

    balanced = LoadBalancingDriver((primary, secondary))
    assert [balanced.next().driver.identity for _ in range(5)] == [1, 2, 1, 2, 1]


def test_capability_and_connection_value_objects_validate_and_project() -> None:
    capabilities = CapabilitySet(("zeta", "alpha"))
    assert tuple(capabilities) == ("alpha", "zeta")
    assert "alpha" in capabilities
    assert capabilities.missing(("alpha", "beta")) == CapabilitySet(("beta",))
    assert not capabilities.supports(("beta",))

    ref = ConnectionRef("database", "primary", "remote")
    assert ref.name == "primary"
    assert ref.driver == "remote"

    with pytest.raises(ValueError):
        CapabilitySet(("INVALID",))
    with pytest.raises(ValueError):
        ConnectionRef("database", "bad name", "remote")
    with pytest.raises(ValueError):
        _manifest(capabilities=()).__class__(
            driver_name="remote",
            version="1.0.0",
            artifact_digest="invalid",
            dependency_graph_digest=_DIGEST_B,
            capabilities=CapabilitySet(),
        )
    with pytest.raises(ValueError):
        DriverManifest(
            driver_name="remote",
            version="1.0.0",
            artifact_digest=_DIGEST_A,
            dependency_graph_digest=_DIGEST_B,
            capabilities=frozenset(),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"binding": object()},
        {"capabilities": frozenset()},
        {"health": "healthy"},
        {"observed_at": datetime(2026, 7, 23, 20)},
        {"expires_at": _NOW},
        {"evidence_digest": "invalid"},
        {"revoked": 1},
    ],
)
def test_capability_evidence_rejects_malformed_contract_fields(change) -> None:
    values = {
        "binding": CapabilityBinding(
            driver_version="1.0.0",
            artifact_digest=_DIGEST_A,
            dependency_graph_digest=_DIGEST_B,
            configuration_digest=_DIGEST_C,
            deployment_profile="production",
            policy_version="policy-v1",
        ),
        "capabilities": CapabilitySet(),
        "health": HealthStatus.HEALTHY,
        "observed_at": _NOW,
        "expires_at": _NOW + timedelta(minutes=1),
        "evidence_digest": _DIGEST_A,
        "revoked": False,
    }
    values.update(change)
    with pytest.raises(ValueError):
        CapabilityEvidence(**values)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"manager_id": "INVALID"},
        {"default": "bad name"},
        {"deployment_profile": "bad profile"},
        {"policy_version": ""},
        {"connections": []},
        {"connections": {"primary": {1: "value", "driver": "remote"}}},
    ],
)
def test_manager_rejects_invalid_top_level_configuration(kwargs) -> None:
    values = {
        "manager_id": "database",
        "connections": {"primary": {"driver": "remote"}},
        "default": "primary",
        "deployment_profile": "production",
        "policy_version": "policy-v1",
        "clock": lambda: _NOW,
    }
    values.update(kwargs)
    with pytest.raises(ManagerConfigurationError):
        Manager(**values)


def test_manager_rejects_excessively_deep_or_large_configuration() -> None:
    nested: object = "leaf"
    for _ in range(26):
        nested = {"next": nested}
    with pytest.raises(ManagerConfigurationError):
        _manager(connections={"primary": {"driver": "remote", "nested": nested}})

    with pytest.raises(ManagerConfigurationError):
        _manager(
            connections={
                "primary": {
                    "driver": "remote",
                    "items": list(range(10_001)),
                }
            }
        )


def test_manager_exposes_verified_health_and_rejects_invalid_extensions() -> None:
    manager = _manager()
    assert manager.manager_id == "database"
    with pytest.raises(DriverResolutionError):
        manager.evidence()
    with pytest.raises(DriverResolutionError):
        manager.binding()
    with pytest.raises(ManagerConfigurationError):
        manager.extend(
            "remote",
            object(),
            manifest=_manifest(),
            verifier=lambda _ref, _driver, binding: _evidence(binding),
        )
    with pytest.raises(ManagerConfigurationError):
        manager.extend(
            "remote",
            lambda _ref, _config: _Driver(1),
            manifest=_manifest(),
            verifier=lambda _ref, _driver, binding: _evidence(binding),
            disposer=object(),
        )

    _register_healthy(manager)
    manager.connection()
    assert manager.health() is HealthStatus.HEALTHY
    assert manager.evidence().capabilities == CapabilitySet(("transactions",))


def test_invalid_required_capability_and_recursive_factory_fail_closed() -> None:
    manager = _manager()
    _register_healthy(manager)
    with pytest.raises(CapabilityValidationError):
        manager.connection(requires=(object(),))

    recursive = _manager()
    _register_healthy(
        recursive,
        factory=lambda _ref, _config: recursive.connection(),
    )
    with pytest.raises(DriverResolutionError):
        recursive.connection()


def test_verifier_exception_or_invalid_result_disposes_unadmitted_driver() -> None:
    for verifier in (
        lambda _ref, _driver, _binding: (_ for _ in ()).throw(RuntimeError("private")),
        lambda _ref, _driver, _binding: object(),
    ):
        manager = _manager()
        disposed: list[int] = []
        _register_healthy(
            manager,
            verifier=verifier,
            disposer=lambda driver, sink=disposed: sink.append(driver.identity),
        )
        with pytest.raises(CapabilityValidationError):
            manager.connection()
        assert disposed == [1]


def test_future_or_manifest_exceeding_evidence_is_rejected() -> None:
    evidence_factories = (
        lambda binding: _evidence(
            binding,
            observed_at=_NOW + timedelta(seconds=1),
            expires_at=_NOW + timedelta(minutes=1),
        ),
        lambda binding: _evidence(binding, capabilities=("transactions", "locks")),
    )
    for make in evidence_factories:
        manager = _manager()
        _register_healthy(
            manager,
            verifier=lambda _ref, _driver, binding, current=make: current(binding),
        )
        with pytest.raises(CapabilityValidationError):
            manager.connection()


def test_cached_verifier_failure_quarantines_and_close_purges_in_reverse_order() -> None:
    manager = _manager()
    failing = [False]
    disposed: list[int] = []

    def factory(ref, _config):
        return _Driver(1 if ref.connection_name == "primary" else 2)

    def verifier(_ref, _driver, binding):
        if failing[-1]:
            raise RuntimeError("probe unavailable")
        return _evidence(binding)

    _register_healthy(
        manager,
        factory=factory,
        verifier=verifier,
        disposer=lambda driver: disposed.append(driver.identity),
    )
    manager.connection()
    failing.append(True)
    with pytest.raises(ConnectionQuarantinedError):
        manager.connection()
    assert disposed == [1]

    manager.purge()
    failing.append(False)
    manager.connection("analytics")
    manager.purge("analytics")
    assert disposed == [1, 2]
    manager.close()


def test_disposal_failure_is_sanitized_after_cache_is_invalidated() -> None:
    manager = _manager()
    _register_healthy(
        manager,
        disposer=lambda _driver: (_ for _ in ()).throw(RuntimeError("private cleanup")),
    )
    manager.connection()
    with pytest.raises(DriverResolutionError) as captured:
        manager.purge()
    assert "private cleanup" not in str(captured.value)
    assert manager.resolved_connections == ()


def test_quarantine_retains_cleanup_failure_as_protected_evidence() -> None:
    manager = _manager()
    evidence = []
    _register_healthy(
        manager,
        verifier=lambda _ref, _driver, _binding: evidence[-1],
        disposer=lambda _driver: (_ for _ in ()).throw(RuntimeError("private cleanup")),
    )
    evidence.append(_evidence(manager.binding()))
    manager.connection()
    evidence.append(
        _evidence(
            manager.binding(),
            capabilities=(),
            evidence_digest="d" * 64,
        )
    )

    with pytest.raises(ConnectionQuarantinedError) as captured:
        manager.connection()
    assert len(captured.value.cleanup_failures) == 1
    assert "private cleanup" not in str(captured.value)
    assert manager.quarantined_connections == ("primary",)


def test_composite_contracts_reject_invalid_shapes_and_are_idempotent_at_fallback() -> None:
    primary = DriverTarget(ConnectionRef("database", "primary", "remote"), _Driver(1))
    secondary = DriverTarget(ConnectionRef("database", "secondary", "remote"), _Driver(2))

    with pytest.raises(ValueError):
        DriverTarget(object(), _Driver(0))
    with pytest.raises(ValueError):
        FallbackDriver(primary, primary)
    fallback = FallbackDriver(primary, secondary, clock=lambda: _NOW)
    fallback.activate_fallback("approved")
    fallback.activate_fallback("ignored_second_reason")
    assert len(fallback.evidence) == 1

    with pytest.raises(ValueError):
        FailoverDriver((primary,))
    with pytest.raises(ValueError):
        FailoverDriver((primary, primary))
    failover = FailoverDriver((primary, secondary), clock=lambda: _NOW)
    failover.failover("unhealthy")
    with pytest.raises(DriverResolutionError):
        failover.failover("exhausted")

    with pytest.raises(ValueError):
        LoadBalancingDriver(())
    with pytest.raises(ValueError):
        LoadBalancingDriver((primary, primary))
    balanced = LoadBalancingDriver((primary,))
    assert balanced.targets == (primary,)
