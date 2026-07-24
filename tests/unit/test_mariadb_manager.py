from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from types import TracebackType
from typing import Literal

import pytest

from af.framework.database_pool import DatabasePool
from af.framework.managers import CapabilityBinding, ConnectionRef, HealthStatus
from af.framework.relational import (
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


def _registration(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        manager_module,
        "_runtime_identity",
        lambda: ("1.1.2", "a" * 64, "b" * 64),
    )
    monkeypatch.setattr(manager_module, "MariaDBDriver", _VerifiedDriver)
    return mariadb_registration(clock=lambda: datetime(2026, 7, 24, 5, 0, tzinfo=UTC))


def test_mariadb_registration_is_inert_and_verifies_exact_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = _registration(monkeypatch)
    ref = ConnectionRef("database", "primary", "mariadb")
    driver = registration.factory(
        ref,
        {"driver": "mariadb", "url": "mariadb+pymysql://localhost/af"},
    )
    binding = CapabilityBinding(
        driver_version="1.1.2",
        artifact_digest="a" * 64,
        dependency_graph_digest="b" * 64,
        configuration_digest=hashlib.sha256(b"config").hexdigest(),
        deployment_profile="test",
        policy_version="1",
    )
    evidence = registration.verifier(ref, driver, binding)

    assert isinstance(driver, _VerifiedDriver)
    assert registration.manifest.driver_name == "mariadb"
    assert registration.manifest.capabilities.supports(
        {DatabaseFeature.CANCELLATION.value, DatabaseFeature.POOLING.value}
    )
    assert evidence.binding == binding
    assert evidence.health is HealthStatus.HEALTHY
    assert evidence.capabilities.supports(feature.value for feature in _FEATURES)


def test_mariadb_registration_builds_bounded_pool_and_rejects_invalid_config(
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
    pool.close()

    invalid = (
        {},
        {"driver": "mariadb"},
        {"driver": "mysql", "url": "mariadb+pymysql://localhost/af"},
        {
            "driver": "mariadb",
            "url": "mariadb+pymysql://localhost/af",
            "pool_size": 0,
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
