from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Thread

from sqlalchemy import func, select

from af.infrastructure.database import Database
from af.infrastructure.database import WorkerMeasurementReplayRow
from af.infrastructure.worker_evidence_replay import (
    SqlAlchemyWorkerMeasurementReplayLedger,
)

_NOW = datetime(2026, 8, 19, tzinfo=UTC)
_ISSUER = "qualification-runner"
_DIGEST = "a" * 64


def test_replay_ledger_persists_consumed_measurements_across_instances(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'replays.sqlite3'}"
    first_db = Database(url)
    first = SqlAlchemyWorkerMeasurementReplayLedger(first_db)
    assert first.consume_batch(
        ((_ISSUER, _DIGEST, _NOW + timedelta(hours=1)),),
        now=_NOW,
    )
    first_db.close()

    second_db = Database(url)
    second = SqlAlchemyWorkerMeasurementReplayLedger(second_db)
    try:
        assert not second.consume_batch(
            ((_ISSUER, _DIGEST, _NOW + timedelta(hours=1)),),
            now=_NOW,
        )
    finally:
        second_db.close()


def test_replay_ledger_consumes_batch_atomically_on_duplicate() -> None:
    database = Database("sqlite://")
    ledger = SqlAlchemyWorkerMeasurementReplayLedger(database)
    first = (_ISSUER, _DIGEST, _NOW + timedelta(hours=1))
    second = (_ISSUER, "b" * 64, _NOW + timedelta(hours=1))
    try:
        assert not ledger.consume_batch((first, first), now=_NOW)
        assert ledger.consume_batch((first,), now=_NOW)
        assert ledger.consume_batch((second,), now=_NOW)
    finally:
        database.close()


def test_replay_ledger_allows_expired_evidence_to_be_consumed_again() -> None:
    database = Database("sqlite://")
    ledger = SqlAlchemyWorkerMeasurementReplayLedger(database)
    old_expiry = _NOW + timedelta(seconds=1)
    try:
        assert ledger.consume_batch(((_ISSUER, _DIGEST, old_expiry),), now=_NOW)
        later = _NOW + timedelta(seconds=2)
        assert ledger.consume_batch(
            ((_ISSUER, _DIGEST, later + timedelta(hours=1)),),
            now=later,
        )
    finally:
        database.close()


def test_replay_ledger_allows_only_one_concurrent_consumer() -> None:
    database = Database("sqlite://")
    ledger = SqlAlchemyWorkerMeasurementReplayLedger(database)
    barrier = Barrier(3)
    outcomes: list[bool] = []

    def consume() -> None:
        barrier.wait()
        outcomes.append(
            ledger.consume_batch(
                ((_ISSUER, _DIGEST, _NOW + timedelta(hours=1)),),
                now=_NOW,
            )
        )

    workers = [Thread(target=consume) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=2)
        assert all(not worker.is_alive() for worker in workers)
        assert sorted(outcomes) == [False, True]
    finally:
        database.close()


def test_replay_ledger_prunes_expired_identities_for_unrelated_batches() -> None:
    database = Database("sqlite://")
    ledger = SqlAlchemyWorkerMeasurementReplayLedger(database)
    old = (_ISSUER, _DIGEST, _NOW + timedelta(seconds=1))
    new = (_ISSUER, "b" * 64, _NOW + timedelta(hours=1))
    try:
        assert ledger.consume_batch((old,), now=_NOW)
        assert ledger.consume_batch((new,), now=_NOW + timedelta(seconds=2))
        with database.sessions() as session:
            count = session.scalar(select(func.count()).select_from(WorkerMeasurementReplayRow))
        assert count == 1
    finally:
        database.close()
