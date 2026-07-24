from __future__ import annotations

from contextlib import AbstractContextManager
from types import TracebackType
from typing import Literal

import pytest

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseOperationError,
    DatabaseStatement,
    DatabaseTransaction,
)
from af.framework.unit_of_work import AfterCommitCallbackError, UnitOfWork
from af.infrastructure.sqlite import SQLiteDriver


def _count(driver: SQLiteDriver) -> int:
    with driver.transaction(read_only=True) as transaction:
        rows = transaction.query(
            DatabaseStatement("SELECT COUNT(*) AS count FROM records", read_only=True)
        )
    value = rows[0]["count"]
    assert isinstance(value, int)
    return value


def test_unit_of_work_commits_before_running_callbacks_in_registration_order() -> None:
    driver = SQLiteDriver(":memory:")
    with driver.transaction() as transaction:
        transaction.execute(DatabaseStatement("CREATE TABLE records (value TEXT)"))

    observed: list[tuple[str, int]] = []
    with UnitOfWork(driver) as unit:
        unit.transaction.execute(
            DatabaseStatement("INSERT INTO records VALUES (:value)", {"value": "committed"})
        )
        unit.after_commit(lambda: observed.append(("first", _count(driver))))
        unit.after_commit(lambda: observed.append(("second", _count(driver))))
        assert observed == []

    assert observed == [("first", 1), ("second", 1)]
    assert _count(driver) == 1
    driver.close()


def test_unit_of_work_rollback_discards_state_and_callbacks() -> None:
    driver = SQLiteDriver(":memory:")
    with driver.transaction() as transaction:
        transaction.execute(DatabaseStatement("CREATE TABLE records (value TEXT)"))

    called: list[str] = []
    with pytest.raises(RuntimeError, match="rollback"), UnitOfWork(driver) as unit:
        unit.transaction.execute(
            DatabaseStatement("INSERT INTO records VALUES (:value)", {"value": "discarded"})
        )
        unit.after_commit(lambda: called.append("published"))
        raise RuntimeError("rollback")

    assert called == []
    assert _count(driver) == 0
    driver.close()


def test_after_commit_failure_is_sanitized_after_all_callbacks_run() -> None:
    driver = SQLiteDriver(":memory:")
    called: list[str] = []

    def fail() -> None:
        called.append("failed")
        raise RuntimeError("secret callback detail")

    with pytest.raises(AfterCommitCallbackError) as captured, UnitOfWork(driver) as unit:
        unit.after_commit(fail)
        unit.after_commit(lambda: called.append("continued"))

    assert called == ["failed", "continued"]
    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured.value.stage == "database.after-commit"
    assert len(captured.value.failures) == 1
    assert "secret callback detail" not in str(captured.value)
    assert "secret callback detail" not in repr(captured.value)
    driver.close()


class _EnterFailureContext(AbstractContextManager[DatabaseTransaction]):
    def __enter__(self) -> DatabaseTransaction:
        raise DatabaseOperationError(
            reason=PlatformReason.TRANSIENT_FAILURE,
            stage="database.transaction",
        )

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error_type, error, traceback
        return False


class _EnterFailureDriver:
    def transaction(self, *, read_only: bool = False) -> _EnterFailureContext:
        del read_only
        return _EnterFailureContext()


class _CommitFailureContext(AbstractContextManager[DatabaseTransaction]):
    def __enter__(self) -> DatabaseTransaction:
        return object()  # type: ignore[return-value]

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error_type, error, traceback
        raise DatabaseOperationError(
            reason=PlatformReason.TRANSIENT_FAILURE,
            stage="database.transaction",
        )


class _CommitFailureDriver:
    def transaction(self, *, read_only: bool = False) -> _CommitFailureContext:
        del read_only
        return _CommitFailureContext()


def test_commit_failure_never_runs_after_commit_callbacks() -> None:
    called: list[str] = []
    with (
        pytest.raises(DatabaseOperationError) as captured,
        UnitOfWork(_CommitFailureDriver()) as unit,  # type: ignore[arg-type]
    ):
        unit.after_commit(lambda: called.append("published"))

    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert called == []


def test_unit_of_work_reports_commit_and_sanitizes_edge_failures() -> None:
    driver = SQLiteDriver(":memory:")
    committed = UnitOfWork(driver)
    assert not committed.committed
    with committed:
        pass
    assert committed.committed

    with pytest.raises(ValueError, match="must not be empty"):
        AfterCommitCallbackError(())
    with pytest.raises(DatabaseOperationError) as enter_failure:
        with UnitOfWork(_EnterFailureDriver()):  # type: ignore[arg-type]
            pass
    assert enter_failure.value.reason is PlatformReason.TRANSIENT_FAILURE

    inactive = UnitOfWork(driver)
    with pytest.raises(DatabaseOperationError) as exit_failure:
        inactive.__exit__(None, None, None)
    assert exit_failure.value.reason is PlatformReason.CONTEXT_UNAVAILABLE
    driver.close()


def test_unit_of_work_rejects_invalid_or_out_of_scope_access() -> None:
    driver = SQLiteDriver(":memory:")
    unit = UnitOfWork(driver)

    with pytest.raises(DatabaseOperationError) as transaction_error:
        _ = unit.transaction
    assert transaction_error.value.reason is PlatformReason.CONTEXT_UNAVAILABLE
    with pytest.raises(DatabaseOperationError) as callback_error:
        unit.after_commit(lambda: None)
    assert callback_error.value.reason is PlatformReason.CONTEXT_UNAVAILABLE
    with pytest.raises(TypeError), UnitOfWork(driver) as active:
        active.after_commit("not callable")  # type: ignore[arg-type]

    with unit:
        with pytest.raises(DatabaseOperationError) as nested:
            unit.__enter__()
        assert nested.value.reason is PlatformReason.TRANSIENT_FAILURE

    with pytest.raises(DatabaseOperationError) as reused:
        unit.__enter__()
    assert reused.value.reason is PlatformReason.CONTEXT_UNAVAILABLE
    with pytest.raises(ValueError):
        UnitOfWork(driver, read_only=1)  # type: ignore[arg-type]
    driver.close()
