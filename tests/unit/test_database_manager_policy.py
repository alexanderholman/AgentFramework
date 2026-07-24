from __future__ import annotations

from datetime import UTC, datetime

import pytest

from af.framework.application import Application
from af.framework.container import Container, ProviderRegistrar, ServiceProvider
from af.framework.database_manager import DatabaseManager, bind_database_manager
from af.framework.errors import PlatformReason
from af.framework.managers import (
    CapabilitySet,
    CapabilityValidationError,
    DriverRegistration,
    DriverResolutionError,
    ManagerConfigurationError,
)
from af.framework.query_policy import SealedQueryPolicy
from af.framework.relational import (
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseStatement,
)
from af.infrastructure.sqlite_manager import sqlite_registration

_NOW = datetime(2026, 7, 24, 3, tzinfo=UTC)


def _manager() -> DatabaseManager:
    manager = DatabaseManager(
        connections={
            "primary": {"driver": "sqlite", "database": ":memory:"},
            "replica": {"driver": "sqlite", "database": ":memory:"},
        },
        default="primary",
        read="replica",
        write="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(sqlite_registration(clock=lambda: _NOW))
    return manager


def test_database_manager_routes_named_read_and_write_connections() -> None:
    manager = _manager()

    with manager.transaction() as transaction:
        transaction.execute(DatabaseStatement("CREATE TABLE records (value TEXT)"))
    with manager.transaction(read_only=True) as transaction:
        assert (
            transaction.query(DatabaseStatement("SELECT 1 AS value", read_only=True))[0]["value"]
            == 1
        )

    assert manager.resolved_connections == ("primary", "replica")
    assert manager.evidence("primary").capabilities.supports((DatabaseFeature.TRANSACTIONS.value,))
    manager.close()


def test_database_manager_fails_closed_for_unverified_features() -> None:
    manager = _manager()

    with pytest.raises(CapabilityValidationError) as captured:
        manager.connection(requires=(DatabaseFeature.ADVISORY_LOCKS,))

    assert captured.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE
    assert manager.resolved_connections == ()
    manager.close()


def test_database_manager_preserves_generic_capability_inputs_and_explicit_route() -> None:
    manager = _manager()

    driver = manager.connection(
        "primary",
        requires=CapabilitySet((DatabaseFeature.TRANSACTIONS.value,)),
    )
    assert driver.supports(DatabaseFeature.TRANSACTIONS)
    assert manager.connection("primary", requires=DatabaseFeature.STREAMING.value) is driver
    with manager.transaction("primary", requires=(DatabaseFeature.SAVEPOINTS,)):
        pass
    with pytest.raises(ValueError):
        manager.connection(requires=(object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        manager.transaction(read_only=1)  # type: ignore[arg-type]
    manager.close()


def test_database_manager_rejects_unknown_routes_and_foreign_registrations() -> None:
    with pytest.raises(ManagerConfigurationError):
        DatabaseManager(
            connections={"primary": {"driver": "sqlite", "database": ":memory:"}},
            default="primary",
            read="missing",
            write="primary",
            deployment_profile="test",
            policy_version="policy-v1",
        )

    manager = _manager()
    registration = sqlite_registration(clock=lambda: _NOW)
    foreign = DriverRegistration(
        manager_id="other",
        driver_name=registration.driver_name,
        factory=registration.factory,
        manifest=registration.manifest,
        verifier=registration.verifier,
        disposer=registration.disposer,
    )
    with pytest.raises(ManagerConfigurationError):
        manager.install(foreign)
    manager.close()


def test_database_manager_is_application_owned_and_closed_on_shutdown() -> None:
    connections = {"primary": {"driver": "sqlite", "database": ":memory:"}}

    class DatabaseProvider(ServiceProvider):
        provider_id = "database"

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_database_manager(
                registrar,
                connections=connections,
                default="primary",
                read="primary",
                write="primary",
                deployment_profile="test",
                policy_version="policy-v1",
                registrations=(sqlite_registration(clock=lambda: _NOW),),
                clock=lambda: _NOW,
            )

    application = Application(Container(), (DatabaseProvider(),))
    application.register()
    connections["primary"]["database"] = ""
    application.boot()
    application.serve()
    manager = application.container.resolve(DatabaseManager)
    with manager.transaction() as transaction:
        transaction.execute(DatabaseStatement("CREATE TABLE records (value TEXT)"))

    application.shutdown()

    with pytest.raises(DriverResolutionError) as captured:
        manager.transaction()
    assert captured.value.reason is PlatformReason.DEPENDENCY_RESOLUTION_FAILED


def test_database_provider_rejects_missing_or_duplicate_registrations() -> None:
    class Registrar:
        def bind(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            del args, kwargs

    registration = sqlite_registration(clock=lambda: _NOW)
    common = {
        "connections": {"primary": {"driver": "sqlite", "database": ":memory:"}},
        "default": "primary",
        "read": "primary",
        "write": "primary",
        "deployment_profile": "test",
        "policy_version": "policy-v1",
    }
    with pytest.raises(ManagerConfigurationError):
        bind_database_manager(Registrar(), registrations=(), **common)  # type: ignore[arg-type]
    with pytest.raises(ManagerConfigurationError):
        bind_database_manager(
            Registrar(),  # type: ignore[arg-type]
            registrations=(registration, registration),
            **common,
        )


def test_sealed_policy_filters_rows_and_caller_cannot_override_tenant() -> None:
    policy = SealedQueryPolicy({"tenant_id": "tenant-a", "visible": True})
    manager = _manager()
    with manager.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                "CREATE TABLE records (tenant_id TEXT, visible INTEGER, category TEXT, value TEXT)"
            )
        )
        for tenant, visible, category, value in (
            ("tenant-a", True, "news", "allowed"),
            ("tenant-a", False, "news", "hidden"),
            ("tenant-b", True, "news", "other-tenant"),
            ("tenant-a", True, "private", "other-category"),
        ):
            transaction.execute(
                DatabaseStatement(
                    "INSERT INTO records VALUES (:tenant, :visible, :category, :value)",
                    {
                        "tenant": tenant,
                        "visible": visible,
                        "category": category,
                        "value": value,
                    },
                )
            )

        statement = policy.select(
            table="records",
            columns=("value",),
            where={"category": "news"},
            order_by=("value",),
            limit=10,
        )
        rows = transaction.query(statement)

    assert [row["value"] for row in rows] == ["allowed"]
    assert "tenant-a" not in repr(statement)
    assert "tenant-a" not in repr(policy)
    same_tenant = policy.select(
        table="records",
        columns=("value",),
        where={"tenant_id": "tenant-a"},
    )
    assert same_tenant.read_only is True
    with pytest.raises(DatabaseOperationError) as captured:
        policy.select(
            table="records",
            columns=("value",),
            where={"tenant_id": "tenant-b"},
        )
    assert captured.value.reason is PlatformReason.AUTHORIZATION_DENIED
    manager.close()


@pytest.mark.parametrize(
    "call",
    [
        lambda policy: policy.select(table="not valid", columns=("value",)),
        lambda policy: policy.select(table="records", columns=()),
        lambda policy: policy.select(table="records", columns=("value",), order_by=("missing",)),
        lambda policy: policy.select(table="records", columns=("value",), limit=0),
        lambda policy: policy.select(table="records", columns=("value",), offset=-1),
    ],
)
def test_sealed_policy_rejects_ambiguous_or_unbounded_query_inputs(call) -> None:  # type: ignore[no-untyped-def]
    policy = SealedQueryPolicy({"tenant_id": "tenant-a"})

    with pytest.raises(ValueError):
        call(policy)


def test_sealed_policy_requires_at_least_one_repository_predicate() -> None:
    with pytest.raises(ValueError):
        SealedQueryPolicy({})


@pytest.mark.parametrize(
    "connection",
    [
        {"driver": "sqlite"},
        {"driver": "sqlite", "database": ":memory:", "extra": True},
    ],
)
def test_sqlite_registration_rejects_unrecognized_configuration(connection) -> None:  # type: ignore[no-untyped-def]
    manager = DatabaseManager(
        connections={"primary": connection},
        default="primary",
        read=None,
        write=None,
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(sqlite_registration(clock=lambda: _NOW))

    with pytest.raises(DriverResolutionError):
        manager.connection()
    manager.close()


def test_sqlite_registration_rejects_invalid_runtime_evidence_time() -> None:
    manager = DatabaseManager(
        connections={"primary": {"driver": "sqlite", "database": ":memory:"}},
        default="primary",
        read=None,
        write=None,
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(
        sqlite_registration(clock=lambda: datetime(2026, 7, 24)),
    )

    with pytest.raises(CapabilityValidationError):
        manager.connection()
    manager.close()
