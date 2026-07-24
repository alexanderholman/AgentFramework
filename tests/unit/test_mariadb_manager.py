from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from types import SimpleNamespace, TracebackType
from typing import Literal, cast

import pytest

from af.framework.database_pool import DatabasePool
from af.framework.managers import CapabilityBinding, ConnectionRef, HealthStatus
from af.framework.relational import (
    DatabaseDriver,
    DatabaseFeature,
    DatabaseTransaction,
    UnsupportedDatabaseFeature,
)
from af.infrastructure import mariadb_manager as manager_module
from af.infrastructure.mariadb_manager import mariadb_registration

_FEATURES = frozenset(
    {
        DatabaseFeature.TRANSACTIONS,
        DatabaseFeature.SAVEPOINTS,
        DatabaseFeature.STREAMING,
        DatabaseFeature.CANCELLATION,
    }
)


class _FakeTransactionContext(AbstractContextManager[DatabaseTransaction]):
    def __enter__(self) -> DatabaseTransaction:
        raise AssertionError("fake MariaDB transaction must not execute")

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error_type, error, traceback
        return False


class _VerifiedDriver:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False

    @property
    def features(self) -> frozenset[DatabaseFeature]:
        return _FEATURES

    def supports(self, feature: DatabaseFeature, /) -> bool:
        return feature in self.features

    def require(self, feature: DatabaseFeature, /) -> None:
        if not self.supports(feature):
            raise UnsupportedDatabaseFeature(feature)

    def transaction(
        self,
        *,
        read_only: bool = False,
    ) -> AbstractContextManager[DatabaseTransaction]:
        del read_only
        return _FakeTransactionContext()

    def cancel(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _WrongFeaturesDriver(_VerifiedDriver):
    @property
    def features(self) -> frozenset[DatabaseFeature]:
        return frozenset({DatabaseFeature.TRANSACTIONS})


def _binding() -> CapabilityBinding:
    return CapabilityBinding(
        driver_version="1.1.2",
        artifact_digest="a" * 64,
        dependency_graph_digest="b" * 64,
        configuration_digest=hashlib.sha256(b"config").hexdigest(),
        deployment_profile="test",
        policy_version="1",
    )


def _registration(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        manager_module,
        "_runtime_identity",
        lambda: ("1.1.2", "a" * 64, "b" * 64),
    )
    monkeypatch.setattr(manager_module, "MariaDBDriver", _VerifiedDriver)
    return mariadb_registration(clock=lambda: datetime(2026, 7, 24, 5, 0, tzinfo=UTC))


def test_mariadb_runtime_identity_binds_runtime_and_artifact_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        manager_module.importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(origin="/runtime/pymysql/__init__.py"),
    )
    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "version",
        lambda name: {"pymysql": "1.1.2", "SQLAlchemy": "2.0.40"}[name],
    )
    monkeypatch.setattr(manager_module.Path, "read_bytes", lambda path: str(path).encode())

    version, artifact_digest, dependency_digest = manager_module._runtime_identity()

    assert version == "1.1.2"
    assert len(artifact_digest) == 64
    assert len(dependency_digest) == 64
    assert artifact_digest != dependency_digest


def test_mariadb_runtime_identity_fails_closed_when_runtime_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager_module.importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(ValueError, match="requires the pymysql runtime"):
        manager_module._runtime_identity()

    monkeypatch.setattr(
        manager_module.importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(origin="/runtime/pymysql/__init__.py"),
    )

    def missing_version(_name: str) -> str:
        raise manager_module.importlib.metadata.PackageNotFoundError("pymysql")

    monkeypatch.setattr(manager_module.importlib.metadata, "version", missing_version)
    with pytest.raises(ValueError, match="runtime identity is unavailable"):
        manager_module._runtime_identity()


def test_mariadb_registration_is_inert_and_verifies_exact_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = _registration(monkeypatch)
    ref = ConnectionRef("database", "primary", "mariadb")
    driver = registration.factory(
        ref,
        {"driver": "mariadb", "url": "mariadb+pymysql://localhost/af"},
    )
    evidence = registration.verifier(ref, driver, _binding())

    assert isinstance(driver, _VerifiedDriver)
    assert registration.manifest.driver_name == "mariadb"
    assert registration.manifest.capabilities.supports(
        {DatabaseFeature.CANCELLATION.value, DatabaseFeature.POOLING.value}
    )
    assert not registration.manifest.capabilities.supports(
        (DatabaseFeature.TRANSACTIONAL_DDL.value,)
    )
    assert evidence.binding == _binding()
    assert evidence.health is HealthStatus.HEALTHY
    assert evidence.capabilities.supports(feature.value for feature in _FEATURES)
    assert not evidence.capabilities.supports((DatabaseFeature.TRANSACTIONAL_DDL.value,))
    assert evidence.expires_at > evidence.observed_at
    disposer = registration.disposer
    assert disposer is not None
    disposer(driver)
    assert driver.closed


def test_mariadb_registration_builds_and_verifies_bounded_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = _registration(monkeypatch)
    ref = ConnectionRef("database", "primary", "mariadb")
    pool = registration.factory(
        ref,
        {
            "driver": "mariadb",
            "url": "mariadb+pymysql://localhost/af",
            "pool_size": 2,
            "pool_timeout": 1.0,
        },
    )

    assert isinstance(pool, DatabasePool)
    assert pool.supports(DatabaseFeature.POOLING)
    assert pool.available == 2
    evidence = registration.verifier(ref, pool, _binding())
    assert evidence.capabilities.supports({DatabaseFeature.POOLING.value})
    pool.close()


def test_mariadb_registration_rejects_invalid_identity_and_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = _registration(monkeypatch)
    ref = ConnectionRef("database", "primary", "mariadb")
    with pytest.raises(ValueError, match="invalid connection identity"):
        registration.factory(
            ConnectionRef("cache", "primary", "mariadb"),
            {"driver": "mariadb", "url": "mariadb+pymysql://localhost/af"},
        )

    invalid = (
        {},
        {"driver": "mariadb"},
        {"driver": "mysql", "url": "mariadb+pymysql://localhost/af"},
        {"driver": "mariadb", "url": ""},
        {
            "driver": "mariadb",
            "url": "mariadb+pymysql://localhost/af",
            "pool_size": True,
        },
        {
            "driver": "mariadb",
            "url": "mariadb+pymysql://localhost/af",
            "pool_size": 257,
        },
        {
            "driver": "mariadb",
            "url": "mariadb+pymysql://localhost/af",
            "pool_timeout": True,
        },
        {
            "driver": "mariadb",
            "url": "mariadb+pymysql://localhost/af",
            "pool_timeout": 0,
        },
        {
            "driver": "mariadb",
            "url": "mariadb+pymysql://localhost/af",
            "pool_timeout": 3_601,
        },
        {
            "driver": "mariadb",
            "url": "mariadb+pymysql://localhost/af",
            "unknown": True,
        },
    )
    for config in invalid:
        with pytest.raises(ValueError):
            registration.factory(ref, config)


def test_mariadb_verifier_rejects_wrong_identity_type_features_and_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = _registration(monkeypatch)
    ref = ConnectionRef("database", "primary", "mariadb")
    driver = registration.factory(
        ref,
        {"driver": "mariadb", "url": "mariadb+pymysql://localhost/af"},
    )
    binding = _binding()

    with pytest.raises(ValueError, match="capability verification failed"):
        registration.verifier(ConnectionRef("database", "primary", "mysql"), driver, binding)
    with pytest.raises(ValueError, match="capability verification failed"):
        registration.verifier(ref, cast(DatabaseDriver, object()), binding)

    monkeypatch.setattr(manager_module, "MariaDBDriver", _WrongFeaturesDriver)
    with pytest.raises(ValueError, match="capability verification failed"):
        registration.verifier(ref, _WrongFeaturesDriver("mariadb+pymysql://localhost/af"), binding)

    monkeypatch.setattr(manager_module, "MariaDBDriver", _VerifiedDriver)
    naive = mariadb_registration(clock=lambda: datetime(2026, 7, 24, 5, 0))
    naive_driver = naive.factory(
        ref,
        {"driver": "mariadb", "url": "mariadb+pymysql://localhost/af"},
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        naive.verifier(ref, naive_driver, binding)
