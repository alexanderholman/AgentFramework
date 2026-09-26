"""Explicit MongoDB registration for named document stores.

Evidence describes the connected server's observed capabilities; it is not a
claim of real-service conformance certification.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pymongo

from af.domain.document.contracts import DocumentDriver, DocumentFeature
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
    HealthStatus,
)
from af.infrastructure.mongo.driver import MongoDBDriver


def mongo_document_registration(
    *,
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[DocumentDriver]:
    now = clock if clock is not None else lambda: datetime.now(UTC)
    manifest = DriverManifest(
        driver_name="mongodb",
        version="1",
        artifact_digest=hashlib.sha256(
            Path(__file__).with_name("driver.py").read_bytes()
        ).hexdigest(),
        dependency_graph_digest=hashlib.sha256(pymongo.version.encode()).hexdigest(),
        capabilities=CapabilitySet(feature.value for feature in DocumentFeature),
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> DocumentDriver:
        if ref.manager_id != "document" or ref.driver_name != "mongodb":
            raise ValueError("MongoDB registration targets document connections")
        if set(config) - {"driver", "url", "database", "replica_set"}:
            raise ValueError("Unknown MongoDB connection option")
        url = config.get("url")
        database = config.get("database", "af_default")
        replica_set = config.get("replica_set")
        if (
            type(url) is not str
            or not url
            or type(database) is not str
            or not database
            or (replica_set is not None and type(replica_set) is not str)
        ):
            raise ValueError("Invalid MongoDB connection identity")
        return MongoDBDriver(url, database, replica_set=replica_set)

    def verify(
        ref: ConnectionRef,
        driver: DocumentDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if ref.manager_id != "document" or not isinstance(driver, MongoDBDriver):
            raise ValueError("Invalid MongoDB runtime driver")
        features = driver.verify_capabilities()
        observed = now()
        capabilities = CapabilitySet(feature.value for feature in features)
        digest = hashlib.sha256(
            repr((expected, tuple(capabilities), observed.isoformat())).encode()
        ).hexdigest()
        return CapabilityEvidence(
            binding=expected,
            capabilities=capabilities,
            health=HealthStatus.HEALTHY,
            observed_at=observed,
            expires_at=observed + timedelta(minutes=1),
            evidence_digest=digest,
        )

    return DriverRegistration(
        manager_id="document",
        driver_name="mongodb",
        factory=factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
