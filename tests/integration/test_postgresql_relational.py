from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Event, Thread
from time import sleep

import pytest
from sqlalchemy.engine import make_url

from af.framework.database_pool import DatabasePool
from af.framework.errors import PlatformReason
from af.framework.managers import CapabilityBinding, ConnectionRef, HealthStatus
from af.framework.relational import (
    DatabaseDriver,
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseStatement,
)
from af.infrastructure.postgresql import PostgreSQLDriver
from af.infrastructure.postgresql_manager import postgresql_registration
from af.testing import ConformanceEvidence, RealServiceHook, RelationalDriverConformanceSuite


def _url() -> str:
    value = os.environ.get("AF_POSTGRESQL_TEST_URL")
    if value is None:
        pytest.skip("AF_POSTGRESQL_TEST_URL is not configured")
    return value


def test_postgresql_passes_shared_real_service_suite() -> None:
    url = _url()

    @contextmanager
    def fixture() -> Iterator[DatabaseDriver]:
        with PostgreSQLDriver(url) as driver:
            yield driver

    report = RelationalDriverConformanceSuite().run_real(
        RealServiceHook("postgresql", fixture, enabled=True)
    )

    assert report.evidence is ConformanceEvidence.REAL_SERVICE
    assert report.integration == "postgresql"


def test_postgresql_cancellation_is_typed_and_connection_recovers() -> None:
    driver = PostgreSQLDriver(_url())
    started = Event()
    captured: list[DatabaseOperationError] = []

    def run_query() -> None:
        try:
            with driver.transaction(read_only=True) as transaction:
                started.set()
                transaction.query(DatabaseStatement("SELECT pg_sleep(30)", read_only=True))
        except DatabaseOperationError as error:
            captured.append(error)

    worker = Thread(target=run_query, daemon=True)
    worker.start()
    assert started.wait(5)
    sleep(0.5)
    driver.cancel()
    worker.join(10)

    assert not worker.is_alive()
    assert len(captured) == 1
    assert captured[0].reason is PlatformReason.CANCELLED
    with driver.transaction(read_only=True) as transaction:
        rows = transaction.query(DatabaseStatement("SELECT 1 AS value", read_only=True))
    assert rows[0]["value"] == 1
    driver.close()


def test_postgresql_pool_drains_and_exposes_verified_capabilities() -> None:
    url = _url()
    with DatabasePool(
        lambda: PostgreSQLDriver(url),
        size=2,
        acquire_timeout=2.0,
    ) as pool:
        assert pool.features == frozenset(
            {
                DatabaseFeature.TRANSACTIONS,
                DatabaseFeature.SAVEPOINTS,
                DatabaseFeature.STREAMING,
                DatabaseFeature.CANCELLATION,
                DatabaseFeature.POOLING,
            }
        )
        with pool.transaction(read_only=True) as transaction:
            rows = transaction.query(DatabaseStatement("SELECT 1 AS value", read_only=True))
        assert rows[0]["value"] == 1
    with pytest.raises(DatabaseOperationError) as captured:
        pool.transaction()
    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_postgresql_registration_binds_runtime_identity_and_evidence() -> None:
    url = _url()
    observed_at = datetime(2026, 7, 24, 4, 0, tzinfo=UTC)
    registration = postgresql_registration(clock=lambda: observed_at)
    ref = ConnectionRef("database", "primary", "postgresql")
    driver = registration.factory(
        ref,
        {"driver": "postgresql", "url": url},
    )
    binding = CapabilityBinding(
        driver_version=registration.manifest.version,
        artifact_digest=registration.manifest.artifact_digest,
        dependency_graph_digest=registration.manifest.dependency_graph_digest,
        configuration_digest=hashlib.sha256(b"postgresql-test-config").hexdigest(),
        deployment_profile="integration",
        policy_version="1",
    )
    evidence = registration.verifier(ref, driver, binding)

    assert evidence.binding == binding
    assert evidence.health is HealthStatus.HEALTHY
    assert evidence.observed_at == observed_at
    assert evidence.capabilities.supports(
        {
            DatabaseFeature.TRANSACTIONS.value,
            DatabaseFeature.SAVEPOINTS.value,
            DatabaseFeature.STREAMING.value,
            DatabaseFeature.CANCELLATION.value,
        }
    )
    disposer = registration.disposer
    if disposer is not None:
        disposer(driver)
    else:
        driver.close()


def test_postgresql_connection_failure_is_sanitized() -> None:
    parsed = make_url(_url())
    bad = parsed.set(port=1, query={"connect_timeout": "1"}).render_as_string(hide_password=False)
    password = parsed.password
    with pytest.raises(DatabaseOperationError) as captured:
        PostgreSQLDriver(bad)
    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    if password is not None:
        assert password not in str(captured.value)
        assert password not in repr(captured.value)
