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
from af.infrastructure import mysql_manager as manager_module
from af.infrastructure.mysql_manager import mysql_registration

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
        raise AssertionError("fake MySQL transaction must not execute")

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
    monkeypatch.setattr(manager_module, "MySQLDriver", _VerifiedDriver)
    return mysql_registration(clock=lambda: datetime(2026, 7, 24, 4, 0, tzinfo=UTC))


def test_mysql_registration_is_inert_and_verifies_exact_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = _registration(monkeypatch)
    ref = ConnectionRef("database", "primary", "mysql")
    driver = registration.factory(
        ref,
        {"driver": "mysql", "url": "mysql+pymysql://localhost/af"},
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
    assert registration.manifest.capabilities.supports(
        {DatabaseFeature.CANCELLATION.value, DatabaseFeature.POOLING.value}
    )
    assert not registration.manifest.capabilities.supports(
        (DatabaseFeature.TRANSACTIONAL_DDL.value,)
    )
    assert evidence.binding == binding
    assert evidence.health is HealthStatus.HEALTHY
    assert evidence.capabilities.supports(feature.value for feature in _FEATURES)
    assert not evidence.capabilities.supports((DatabaseFeature.TRANSACTIONAL_DDL.value,))


def test_mysql_registration_builds_bounded_pool_and_rejects_invalid_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = _registration(monkeypatch)
    ref = ConnectionRef("database", "primary", "mysql")
    pool = registration.factory(
        ref,
        {
            "driver": "mysql",
            "url": "mysql+pymysql://localhost/af",
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
        {"driver": "mysql"},
        {"driver": "sqlite", "url": "mysql+pymysql://localhost/af"},
        {
            "driver": "mysql",
            "url": "mysql+pymysql://localhost/af",
            "pool_size": 0,
        },
        {
            "driver": "mysql",
            "url": "mysql+pymysql://localhost/af",
            "unknown": True,
        },
    )
    for config in invalid:
        with pytest.raises(ValueError):
            registration.factory(ref, config)
