"""Atomic durable replay protection for signed worker measurements."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from af.application.worker_evidence_ingress import (
    WorkerOptimizationMeasurementReplayLedger,
)
from af.infrastructure.database import Database, WorkerMeasurementReplayRow

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_BATCH = 256


class _ReplayDetected(Exception):
    pass


class SqlAlchemyWorkerMeasurementReplayLedger(WorkerOptimizationMeasurementReplayLedger):
    """Consume (issuer, digest) pairs once, durably and atomically per batch."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("worker replay ledger requires Database")
        self._database = database

    def consume_batch(
        self,
        entries: tuple[tuple[str, str, datetime], ...],
        *,
        now: datetime,
    ) -> bool:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("worker replay ledger time must be timezone-aware")
        if not isinstance(entries, tuple) or not 1 <= len(entries) <= _MAX_BATCH:
            raise ValueError("worker replay ledger batch must be bounded and non-empty")
        normalized_now = now.astimezone(UTC)
        normalized: list[tuple[str, str, datetime]] = []
        keys: set[tuple[str, str]] = set()
        for entry in entries:
            if not isinstance(entry, tuple) or len(entry) != 3:
                raise ValueError("worker replay ledger entry is invalid")
            issuer, digest, expires_at = entry
            if type(issuer) is not str or _IDENTIFIER.fullmatch(issuer) is None:
                raise ValueError("worker replay ledger issuer is invalid")
            if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
                raise ValueError("worker replay ledger digest is invalid")
            if (
                not isinstance(expires_at, datetime)
                or expires_at.tzinfo is None
                or expires_at.utcoffset() is None
                or expires_at <= normalized_now
            ):
                raise ValueError("worker replay ledger expiry is invalid")
            key = (issuer, digest)
            if key in keys:
                return False
            keys.add(key)
            normalized.append((issuer, digest, expires_at.astimezone(UTC)))
        try:
            with self._database.sessions.begin() as session:
                for issuer, digest, expires_at in normalized:
                    session.execute(
                        delete(WorkerMeasurementReplayRow).where(
                            WorkerMeasurementReplayRow.issuer == issuer,
                            WorkerMeasurementReplayRow.evidence_digest == digest,
                            WorkerMeasurementReplayRow.expires_at <= normalized_now,
                        )
                    )
                    if session.get(WorkerMeasurementReplayRow, (issuer, digest)) is not None:
                        raise _ReplayDetected
                    session.add(
                        WorkerMeasurementReplayRow(
                            issuer=issuer,
                            evidence_digest=digest,
                            expires_at=expires_at,
                            consumed_at=normalized_now,
                        )
                    )
                    session.flush()
        except (_ReplayDetected, IntegrityError):
            return False
        return True
