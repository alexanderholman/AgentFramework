from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta, timezone

import pytest

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseCursor,
    DatabaseOperationError,
    DatabaseRow,
    DatabaseStatement,
    DatabaseTransaction,
)
from af.infrastructure.sqlite import SQLiteDriver
from af.messaging import (
    InboxRecord,
    ProjectionCheckpoint,
    ProjectionCheckpointStore,
    ProjectionConflictError,
    ProjectionDecision,
    ProjectionGapError,
    ProjectionIntegrityError,
    TransactionalInbox,
    TransactionalProjectionGuard,
)

_NOW = datetime(2026, 7, 24, 9, tzinfo=UTC)


def _setup() -> tuple[SQLiteDriver, TransactionalProjectionGuard]:
    driver = SQLiteDriver(":memory:")
    guard = TransactionalProjectionGuard()
    with driver.transaction() as transaction:
        transaction.execute(DatabaseStatement("CREATE TABLE projected_values (value TEXT)"))
        guard.install(transaction)
        guard.install(transaction)
    return driver, guard


def _prepare(
    guard: TransactionalProjectionGuard,
    transaction: DatabaseTransaction,
    *,
    message_id: str,
    position: int,
    projection_name: str = "search.records",
    source_name: str = "records.v1.partition-0",
    processed_at: datetime = _NOW,
) -> ProjectionDecision:
    return guard.prepare(
        transaction,
        projection_name=projection_name,
        source_name=source_name,
        message_id=message_id,
        position=position,
        processed_at=processed_at,
    )


class _ConstraintFailureTransaction:
    def __init__(self, inner: DatabaseTransaction) -> None:
        self._inner = inner

    def execute(self, statement: DatabaseStatement, /) -> int:
        if "INSERT INTO af_projection_checkpoints" in statement.sql:
            raise DatabaseOperationError(
                reason=PlatformReason.PERMANENT_FAILURE,
                stage="database.execute",
                protected_cause=sqlite3.IntegrityError("private unique evidence"),
            )
        return self._inner.execute(statement)

    def query(self, statement: DatabaseStatement, /) -> tuple[DatabaseRow, ...]:
        return self._inner.query(statement)

    def stream(self, statement: DatabaseStatement, /) -> DatabaseCursor:
        return self._inner.stream(statement)

    def savepoint(self, name: str, /) -> AbstractContextManager[None]:
        return self._inner.savepoint(name)

    def mark_rollback_only(self) -> None:
        self._inner.mark_rollback_only()

    def claim_operation(self, name: str, /) -> bool:
        return self._inner.claim_operation(name)


def test_projection_guard_classifies_unique_insert_race_as_transient() -> None:
    driver, guard = _setup()
    with (
        pytest.raises(ProjectionConflictError) as captured,
        driver.transaction() as transaction,
    ):
        _prepare(
            guard,
            _ConstraintFailureTransaction(transaction),
            message_id="event-1",
            position=1,
        )

    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert captured.value.stage == "projection.conflict"
    assert "private" not in str(captured.value)
    assert "private" not in repr(captured.value)
    driver.close()


def test_projection_guard_commits_contiguous_state_and_detects_duplicate() -> None:
    driver, guard = _setup()

    with driver.transaction() as transaction:
        assert (
            _prepare(guard, transaction, message_id="event-10", position=10)
            is ProjectionDecision.APPLY
        )
        transaction.execute(DatabaseStatement("INSERT INTO projected_values VALUES ('first')"))

    with driver.transaction() as transaction:
        assert (
            _prepare(guard, transaction, message_id="event-11", position=11)
            is ProjectionDecision.APPLY
        )
        transaction.execute(DatabaseStatement("INSERT INTO projected_values VALUES ('second')"))

    with driver.transaction() as transaction:
        assert (
            _prepare(guard, transaction, message_id="event-11", position=11)
            is ProjectionDecision.DUPLICATE
        )

    with driver.transaction(read_only=True) as transaction:
        values = transaction.query(
            DatabaseStatement("SELECT value FROM projected_values ORDER BY value", read_only=True)
        )
        checkpoint = ProjectionCheckpointStore().load(
            transaction,
            projection_name="search.records",
            source_name="records.v1.partition-0",
        )
        seen = TransactionalInbox().find(
            transaction,
            consumer_name="search.records",
            source_name="records.v1.partition-0",
            message_id="event-10",
        )

    assert tuple(row["value"] for row in values) == ("first", "second")
    assert checkpoint is not None
    assert checkpoint.position == 11
    assert checkpoint.message_id == "event-11"
    assert seen is not None
    assert seen.position == 10
    assert "event-10" not in repr(seen)
    assert "search.records" not in repr(checkpoint)
    driver.close()


def test_projection_guard_rollback_discards_checkpoint_inbox_and_projection_state() -> None:
    driver, guard = _setup()

    with (
        pytest.raises(RuntimeError, match="projection failed"),
        driver.transaction() as transaction,
    ):
        assert (
            _prepare(guard, transaction, message_id="event-1", position=1)
            is ProjectionDecision.APPLY
        )
        transaction.execute(DatabaseStatement("INSERT INTO projected_values VALUES ('discarded')"))
        raise RuntimeError("projection failed")

    with driver.transaction(read_only=True) as transaction:
        checkpoint = ProjectionCheckpointStore().load(
            transaction,
            projection_name="search.records",
            source_name="records.v1.partition-0",
        )
        seen = TransactionalInbox().find(
            transaction,
            consumer_name="search.records",
            source_name="records.v1.partition-0",
            message_id="event-1",
        )
        values = transaction.query(
            DatabaseStatement("SELECT value FROM projected_values", read_only=True)
        )

    assert checkpoint is None
    assert seen is None
    assert values == ()
    driver.close()


def test_stale_unseen_delivery_is_recorded_without_regressing_checkpoint() -> None:
    driver, guard = _setup()

    with driver.transaction() as transaction:
        assert (
            _prepare(guard, transaction, message_id="event-5", position=5)
            is ProjectionDecision.APPLY
        )

    with driver.transaction() as transaction:
        assert (
            _prepare(guard, transaction, message_id="event-3", position=3)
            is ProjectionDecision.STALE
        )

    with driver.transaction() as transaction:
        assert (
            _prepare(guard, transaction, message_id="event-3", position=3)
            is ProjectionDecision.DUPLICATE
        )

    with driver.transaction(read_only=True) as transaction:
        checkpoint = ProjectionCheckpointStore().load(
            transaction,
            projection_name="search.records",
            source_name="records.v1.partition-0",
        )
        seen = TransactionalInbox().find(
            transaction,
            consumer_name="search.records",
            source_name="records.v1.partition-0",
            message_id="event-3",
        )

    assert checkpoint is not None
    assert checkpoint.position == 5
    assert seen is not None
    assert seen.position == 3
    driver.close()


def test_projection_guard_can_skip_unbounded_stale_inbox_evidence() -> None:
    driver, guard = _setup()
    with driver.transaction() as transaction:
        assert (
            _prepare(guard, transaction, message_id="event-5", position=5)
            is ProjectionDecision.APPLY
        )
    with driver.transaction() as transaction:
        assert (
            guard.prepare(
                transaction,
                projection_name="search.records",
                source_name="records.v1.partition-0",
                message_id="event-3",
                position=3,
                processed_at=_NOW,
                record_stale=False,
            )
            is ProjectionDecision.STALE
        )
    with driver.transaction(read_only=True) as transaction:
        assert (
            TransactionalInbox().find(
                transaction,
                consumer_name="search.records",
                source_name="records.v1.partition-0",
                message_id="event-3",
            )
            is None
        )
    with pytest.raises(ValueError, match="boolean"), driver.transaction() as transaction:
        guard.prepare(
            transaction,
            projection_name="search.records",
            source_name="records.v1.partition-0",
            message_id="event-3",
            position=3,
            processed_at=_NOW,
            record_stale=1,  # type: ignore[arg-type]
        )
    driver.close()


def test_gap_fails_before_recording_delivery_or_advancing_checkpoint() -> None:
    driver, guard = _setup()

    with driver.transaction() as transaction:
        assert (
            _prepare(guard, transaction, message_id="event-7", position=7)
            is ProjectionDecision.APPLY
        )

    with pytest.raises(ProjectionGapError) as captured, driver.transaction() as transaction:
        _prepare(guard, transaction, message_id="event-9", position=9)

    assert captured.value.reason is PlatformReason.CONTEXT_UNAVAILABLE
    assert captured.value.stage == "projection.gap"
    with driver.transaction(read_only=True) as transaction:
        checkpoint = ProjectionCheckpointStore().load(
            transaction,
            projection_name="search.records",
            source_name="records.v1.partition-0",
        )
        seen = TransactionalInbox().find(
            transaction,
            consumer_name="search.records",
            source_name="records.v1.partition-0",
            message_id="event-9",
        )

    assert checkpoint is not None
    assert checkpoint.position == 7
    assert seen is None
    driver.close()


def test_conflicting_position_or_reused_message_id_fails_closed() -> None:
    driver, guard = _setup()

    with driver.transaction() as transaction:
        assert (
            _prepare(guard, transaction, message_id="event-2", position=2)
            is ProjectionDecision.APPLY
        )

    with pytest.raises(ProjectionIntegrityError), driver.transaction() as transaction:
        _prepare(guard, transaction, message_id="different-event", position=2)

    with pytest.raises(ProjectionIntegrityError), driver.transaction() as transaction:
        _prepare(guard, transaction, message_id="event-2", position=3)

    driver.close()


def test_projection_sources_and_consumers_are_independent() -> None:
    driver, guard = _setup()

    with driver.transaction() as transaction:
        assert (
            _prepare(guard, transaction, message_id="event-a", position=4)
            is ProjectionDecision.APPLY
        )
        assert (
            _prepare(
                guard,
                transaction,
                message_id="event-a",
                position=4,
                projection_name="analytics.records",
            )
            is ProjectionDecision.APPLY
        )
        assert (
            _prepare(
                guard,
                transaction,
                message_id="event-a",
                position=4,
                source_name="records.v1.partition-1",
            )
            is ProjectionDecision.APPLY
        )

    driver.close()


def test_missing_inbox_evidence_for_current_checkpoint_is_repaired_as_duplicate() -> None:
    driver, guard = _setup()
    checkpoint = ProjectionCheckpoint(
        projection_name="search.records",
        source_name="records.v1.partition-0",
        position=12,
        message_id="event-12",
        updated_at=_NOW,
    )

    with driver.transaction() as transaction:
        ProjectionCheckpointStore().start(transaction, checkpoint)

    with driver.transaction() as transaction:
        assert (
            _prepare(guard, transaction, message_id="event-12", position=12)
            is ProjectionDecision.DUPLICATE
        )

    with driver.transaction(read_only=True) as transaction:
        seen = TransactionalInbox().find(
            transaction,
            consumer_name="search.records",
            source_name="records.v1.partition-0",
            message_id="event-12",
        )
    assert seen is not None
    driver.close()


def test_stores_reject_invalid_contracts_and_noncontiguous_direct_transition() -> None:
    driver, guard = _setup()
    inbox = TransactionalInbox()
    checkpoints = ProjectionCheckpointStore()

    with pytest.raises(TypeError, match="InboxRecord"), driver.transaction() as transaction:
        inbox.record(transaction, object())  # type: ignore[arg-type]

    with (
        pytest.raises(TypeError, match="ProjectionCheckpoint"),
        driver.transaction() as transaction,
    ):
        checkpoints.start(transaction, object())  # type: ignore[arg-type]

    first = ProjectionCheckpoint(
        projection_name="search.records",
        source_name="records.v1.partition-0",
        position=1,
        message_id="event-1",
        updated_at=_NOW,
    )
    gap = ProjectionCheckpoint(
        projection_name="search.records",
        source_name="records.v1.partition-0",
        position=3,
        message_id="event-3",
        updated_at=_NOW,
    )
    with pytest.raises(ValueError, match="contiguous"), driver.transaction() as transaction:
        checkpoints.advance(transaction, first, gap)

    with pytest.raises(TypeError, match="transition"), driver.transaction() as transaction:
        checkpoints.advance(transaction, object(), gap)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="TransactionalInbox"):
        TransactionalProjectionGuard(inbox=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ProjectionCheckpointStore"):
        TransactionalProjectionGuard(checkpoints=object())  # type: ignore[arg-type]

    del guard
    driver.close()


@pytest.mark.parametrize(
    ("projection_name", "source_name", "message_id", "position", "processed_at"),
    [
        ("INVALID NAME", "records.v1", "event-1", 1, _NOW),
        ("search.records", "INVALID NAME", "event-1", 1, _NOW),
        ("search.records", "records.v1", "", 1, _NOW),
        ("search.records", "records.v1", "event-1", -1, _NOW),
        ("search.records", "records.v1", "event-1", 1 << 63, _NOW),
        ("search.records", "records.v1", "event-1", 1, datetime(2026, 7, 24, 9)),
    ],
)
def test_projection_guard_rejects_invalid_delivery_identity(
    projection_name: str,
    source_name: str,
    message_id: str,
    position: int,
    processed_at: datetime,
) -> None:
    driver, guard = _setup()

    with pytest.raises(ValueError), driver.transaction() as transaction:
        guard.prepare(
            transaction,
            projection_name=projection_name,
            source_name=source_name,
            message_id=message_id,
            position=position,
            processed_at=processed_at,
        )
    driver.close()


def test_records_normalize_aware_times_to_utc() -> None:
    local_time = datetime(2026, 7, 24, 10, tzinfo=timezone(timedelta(hours=1)))
    inbox = InboxRecord("search.records", "records.v1", "event-1", 1, local_time)
    checkpoint = ProjectionCheckpoint("search.records", "records.v1", 1, "event-1", local_time)

    assert inbox.processed_at == _NOW
    assert checkpoint.updated_at == _NOW


def test_corrupt_inbox_and_checkpoint_rows_fail_closed_without_exposing_values() -> None:
    driver, guard = _setup()

    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                """INSERT INTO af_inbox VALUES (
                    'search.records',
                    'records.v1.partition-0',
                    'secret-event',
                    -1,
                    'not-a-timestamp'
                )"""
            )
        )
        transaction.execute(
            DatabaseStatement(
                """INSERT INTO af_projection_checkpoints VALUES (
                    'search.corrupt',
                    'records.v1.partition-0',
                    -1,
                    'secret-checkpoint',
                    'not-a-timestamp'
                )"""
            )
        )

    with (
        pytest.raises(ProjectionIntegrityError) as inbox_error,
        driver.transaction(read_only=True) as transaction,
    ):
        TransactionalInbox().find(
            transaction,
            consumer_name="search.records",
            source_name="records.v1.partition-0",
            message_id="secret-event",
        )
    with (
        pytest.raises(ProjectionIntegrityError) as checkpoint_error,
        driver.transaction(read_only=True) as transaction,
    ):
        ProjectionCheckpointStore().load(
            transaction,
            projection_name="search.corrupt",
            source_name="records.v1.partition-0",
        )

    for captured in (inbox_error, checkpoint_error):
        assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
        assert "secret" not in str(captured.value)
        assert "secret" not in repr(captured.value)
    del guard
    driver.close()


def test_direct_checkpoint_advance_detects_stale_optimistic_state() -> None:
    driver, guard = _setup()
    checkpoints = ProjectionCheckpointStore()
    first = ProjectionCheckpoint("search.records", "records.v1", 1, "event-1", _NOW)
    second = ProjectionCheckpoint("search.records", "records.v1", 2, "event-2", _NOW)

    with driver.transaction() as transaction:
        checkpoints.start(transaction, first)

    with driver.transaction() as transaction:
        checkpoints.advance(transaction, first, second)

    with pytest.raises(ProjectionConflictError) as captured, driver.transaction() as transaction:
        checkpoints.advance(transaction, first, second)

    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    del guard
    driver.close()


def test_duplicate_inbox_insert_rolls_back_caller_transaction() -> None:
    driver, guard = _setup()
    inbox = TransactionalInbox()
    record = InboxRecord("search.records", "records.v1", "event-1", 1, _NOW)

    with driver.transaction() as transaction:
        inbox.record(transaction, record)

    with pytest.raises(DatabaseOperationError), driver.transaction() as transaction:
        transaction.execute(DatabaseStatement("INSERT INTO projected_values VALUES ('discarded')"))
        inbox.record(transaction, record)

    with driver.transaction(read_only=True) as transaction:
        values = transaction.query(
            DatabaseStatement("SELECT value FROM projected_values", read_only=True)
        )
    assert values == ()
    del guard
    driver.close()
