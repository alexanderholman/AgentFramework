from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime
from types import TracebackType
from typing import Literal, cast

import pytest

from af.framework.database_manager import DatabaseManager
from af.framework.errors import PlatformReason
from af.framework.relational import DatabaseStatement, DatabaseTransaction
from af.framework.unit_of_work import (
    UnitOfWork,
    UnitOfWorkAfterCommitError,
    UnitOfWorkLifecycleError,
    UnitOfWorkState,
)
from af.infrastructure.sqlite_manager import sqlite_registration

_NOW = datetime(2026, 7, 24, 8, tzinfo=UTC)


def _manager() -> DatabaseManager:
    manager = DatabaseManager(
        connections={"primary": {"driver": "sqlite", "database": ":memory:"}},
        default="primary",
        read="primary",
        write="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(sqlite_registration(clock=lambda: _NOW))
    return manager


def _create_records(manager: DatabaseManager) -> None:
    with manager.transaction() as transaction:
        transaction.execute(DatabaseStatement("CREATE TABLE records (value TEXT)"))


def _record_count(manager: DatabaseManager) -> int:
    with manager.transaction(read_only=True) as transaction:
        value = transaction.query(
            DatabaseStatement("SELECT COUNT(*) AS count FROM records", read_only=True)
        )[0]["count"]
    assert type(value) is int
    return value


def test_unit_of_work_commits_before_running_callbacks() -> None:
    manager = _manager()
    _create_records(manager)
    observed: list[int] = []
    unit = manager.unit_of_work()

    with unit as transaction:
        assert unit.state is UnitOfWorkState.ACTIVE
        assert unit.transaction is transaction
        transaction.execute(
            DatabaseStatement(
                "INSERT INTO records (value) VALUES (:value)",
                {"value": "committed"},
            )
        )
        unit.after_commit(lambda: observed.append(_record_count(manager)))

    assert unit.state is UnitOfWorkState.COMMITTED
    assert observed == [1]
    manager.close()


def test_unit_of_work_rollback_discards_callbacks_and_state_changes() -> None:
    manager = _manager()
    _create_records(manager)
    called = False
    unit = manager.unit_of_work()

    def callback() -> None:
        nonlocal called
        called = True

    with pytest.raises(RuntimeError, match="abort"):
        with unit as transaction:
            transaction.execute(
                DatabaseStatement(
                    "INSERT INTO records (value) VALUES (:value)",
                    {"value": "rolled-back"},
                )
            )
            unit.after_commit(callback)
            raise RuntimeError("abort")

    assert unit.state is UnitOfWorkState.ROLLED_BACK
    assert called is False
    assert _record_count(manager) == 0
    manager.close()


def test_after_commit_failure_is_sanitized_after_authoritative_commit() -> None:
    manager = _manager()
    _create_records(manager)
    later_callback_called = False
    unit = manager.unit_of_work()

    def fail() -> None:
        raise ValueError("secret callback detail")

    def later_callback() -> None:
        nonlocal later_callback_called
        later_callback_called = True

    with pytest.raises(UnitOfWorkAfterCommitError) as captured:
        with unit as transaction:
            transaction.execute(
                DatabaseStatement(
                    "INSERT INTO records (value) VALUES (:value)",
                    {"value": "committed"},
                )
            )
            unit.after_commit(fail)
            unit.after_commit(later_callback)

    assert captured.value.callback_index == 0
    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured.value.stage == "unit-of-work.after-commit"
    assert "secret callback detail" not in str(captured.value)
    assert "secret callback detail" not in repr(captured.value)
    assert unit.state is UnitOfWorkState.AFTER_COMMIT_FAILED
    assert later_callback_called is False
    assert _record_count(manager) == 1
    manager.close()


def test_after_commit_callback_must_return_none() -> None:
    manager = _manager()
    _create_records(manager)
    unit = manager.unit_of_work()

    with pytest.raises(UnitOfWorkAfterCommitError) as captured:
        with unit:
            unit.after_commit(lambda: cast(None, "unexpected"))

    assert isinstance(captured.value.protected_cause, TypeError)
    assert unit.state is UnitOfWorkState.AFTER_COMMIT_FAILED
    manager.close()


@pytest.mark.parametrize("limit", [0, 4_097, True])
def test_unit_of_work_rejects_invalid_callback_limits(limit: object) -> None:
    manager = _manager()
    with pytest.raises(ValueError, match="callback limit"):
        UnitOfWork(
            manager.transaction(),
            max_after_commit_callbacks=cast(int, limit),
        )
    manager.close()


def test_unit_of_work_fails_closed_on_lifecycle_misuse_and_callback_overflow() -> None:
    manager = _manager()
    unit = manager.unit_of_work(max_after_commit_callbacks=1)

    with pytest.raises(UnitOfWorkLifecycleError):
        _ = unit.transaction
    with pytest.raises(UnitOfWorkLifecycleError):
        unit.after_commit(lambda: None)

    with unit:
        with pytest.raises(TypeError, match="must be callable"):
            unit.after_commit(cast(object, None))  # type: ignore[arg-type]
        unit.after_commit(lambda: None)
        with pytest.raises(UnitOfWorkLifecycleError):
            unit.after_commit(lambda: None)

    assert unit.state is UnitOfWorkState.COMMITTED
    with pytest.raises(UnitOfWorkLifecycleError):
        unit.after_commit(lambda: None)
    with pytest.raises(UnitOfWorkLifecycleError):
        _ = unit.transaction
    with pytest.raises(UnitOfWorkLifecycleError):
        unit.__enter__()
    manager.close()


class _SuppressingContext(AbstractContextManager[DatabaseTransaction]):
    def __enter__(self) -> DatabaseTransaction:
        return cast(DatabaseTransaction, object())

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[True]:
        del error_type, error, traceback
        return True


class _FailingEnterContext(AbstractContextManager[DatabaseTransaction]):
    def __enter__(self) -> DatabaseTransaction:
        raise RuntimeError("enter failed")

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error_type, error, traceback
        return False


class _FailingExitContext(AbstractContextManager[DatabaseTransaction]):
    def __enter__(self) -> DatabaseTransaction:
        return cast(DatabaseTransaction, object())

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error_type, error, traceback
        raise RuntimeError("exit failed")


def test_unit_of_work_rejects_suppressed_body_failures() -> None:
    unit = UnitOfWork(_SuppressingContext())

    with pytest.raises(UnitOfWorkLifecycleError) as captured:
        with unit:
            raise RuntimeError("body failed")

    assert isinstance(captured.value.protected_cause, RuntimeError)
    assert unit.state is UnitOfWorkState.FAILED


def test_unit_of_work_records_enter_and_exit_failures() -> None:
    enter = UnitOfWork(_FailingEnterContext())
    with pytest.raises(RuntimeError, match="enter failed"):
        enter.__enter__()
    assert enter.state is UnitOfWorkState.FAILED

    exit_unit = UnitOfWork(_FailingExitContext())
    exit_unit.__enter__()
    with pytest.raises(RuntimeError, match="exit failed"):
        exit_unit.__exit__(None, None, None)
    assert exit_unit.state is UnitOfWorkState.FAILED
