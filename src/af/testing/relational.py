"""Reusable real-service conformance cases for relational database drivers."""

from __future__ import annotations

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseDriver,
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseRow,
    DatabaseStatement,
    UnsupportedDatabaseFeature,
)
from af.testing.conformance import ConformanceCase, DriverConformanceSuite

__all__ = ("RelationalDriverConformanceSuite", "relational_driver_cases")

_TABLE = "af_conformance_records"
_REQUIRED_FEATURES = frozenset(
    {
        DatabaseFeature.TRANSACTIONS,
        DatabaseFeature.SAVEPOINTS,
        DatabaseFeature.STREAMING,
    }
)


class _ExpectedRollback(RuntimeError):
    pass


def _trigger_rollback(label: str) -> None:
    raise _ExpectedRollback(label)


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _reset_schema(driver: DatabaseDriver) -> None:
    with driver.transaction() as transaction:
        transaction.execute(DatabaseStatement(f"DROP TABLE IF EXISTS {_TABLE}"))
        transaction.execute(
            DatabaseStatement(
                f"CREATE TABLE {_TABLE} ("
                "id INTEGER PRIMARY KEY, "
                "tenant VARCHAR(64) NOT NULL, "
                "value VARCHAR(128) NOT NULL"
                ")"
            )
        )


def _insert(driver: DatabaseDriver, *, record_id: int, value: str) -> None:
    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                f"INSERT INTO {_TABLE} (id, tenant, value) VALUES (:id, :tenant, :value)",
                {"id": record_id, "tenant": "tenant-a", "value": value},
            )
        )


def _rows(driver: DatabaseDriver) -> tuple[DatabaseRow, ...]:
    with driver.transaction(read_only=True) as transaction:
        return transaction.query(
            DatabaseStatement(
                f"SELECT id, tenant, value FROM {_TABLE} ORDER BY id",
                read_only=True,
            )
        )


def _assert_commit(driver: DatabaseDriver) -> None:
    _reset_schema(driver)
    _insert(driver, record_id=1, value="committed")
    expected = [{"id": 1, "tenant": "tenant-a", "value": "committed"}]
    _check(
        [row.as_mapping() for row in _rows(driver)] == expected,
        "relational driver did not commit transaction work",
    )


def _assert_rollback(driver: DatabaseDriver) -> None:
    _reset_schema(driver)
    rolled_back = False
    try:
        with driver.transaction() as transaction:
            transaction.execute(
                DatabaseStatement(
                    f"INSERT INTO {_TABLE} (id, tenant, value) VALUES (:id, :tenant, :value)",
                    {"id": 1, "tenant": "tenant-a", "value": "discarded"},
                )
            )
            _trigger_rollback("rollback")
    except _ExpectedRollback:
        rolled_back = True
    _check(rolled_back, "relational transaction suppressed the caller failure")
    _check(not _rows(driver), "relational driver committed work from a failed transaction")


def _assert_savepoint(driver: DatabaseDriver) -> None:
    _reset_schema(driver)
    driver.require(DatabaseFeature.SAVEPOINTS)
    rolled_back = False
    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                f"INSERT INTO {_TABLE} (id, tenant, value) VALUES (:id, :tenant, :value)",
                {"id": 1, "tenant": "tenant-a", "value": "kept"},
            )
        )
        try:
            with transaction.savepoint("af_nested"):
                transaction.execute(
                    DatabaseStatement(
                        f"INSERT INTO {_TABLE} (id, tenant, value) VALUES (:id, :tenant, :value)",
                        {"id": 2, "tenant": "tenant-a", "value": "discarded"},
                    )
                )
                _trigger_rollback("savepoint")
        except _ExpectedRollback:
            rolled_back = True
    _check(rolled_back, "relational savepoint suppressed the caller failure")
    _check(
        [row["value"] for row in _rows(driver)] == ["kept"],
        "relational savepoint did not isolate nested rollback",
    )


def _assert_stream_lifetime(driver: DatabaseDriver) -> None:
    _reset_schema(driver)
    _insert(driver, record_id=1, value="first")
    _insert(driver, record_id=2, value="second")
    with driver.transaction(read_only=True) as transaction:
        cursor = transaction.stream(
            DatabaseStatement(
                f"SELECT value FROM {_TABLE} ORDER BY id",
                read_only=True,
            )
        )
        _check(next(cursor)["value"] == "first", "relational stream returned rows out of order")
    closed = False
    try:
        next(cursor)
    except DatabaseOperationError:
        closed = True
    _check(closed, "relational cursor remained usable after its transaction ended")


def _assert_read_only(driver: DatabaseDriver) -> None:
    _reset_schema(driver)
    execution_error: DatabaseOperationError | None = None
    with driver.transaction(read_only=True) as transaction:
        try:
            transaction.execute(
                DatabaseStatement(
                    f"INSERT INTO {_TABLE} (id, tenant, value) VALUES (1, 'tenant-a', 'denied')"
                )
            )
        except DatabaseOperationError as error:
            execution_error = error
    _check(execution_error is not None, "read-only transaction accepted a write statement")
    if execution_error is not None:
        _check(
            execution_error.reason is PlatformReason.AUTHORIZATION_DENIED,
            "read-only execution failed with the wrong stable reason",
        )

    query_error: DatabaseOperationError | None = None
    with driver.transaction(read_only=True) as transaction:
        try:
            transaction.query(
                DatabaseStatement(
                    f"DELETE FROM {_TABLE}",
                    read_only=True,
                )
            )
        except DatabaseOperationError as error:
            query_error = error
    _check(query_error is not None, "statement metadata bypassed database read-only enforcement")


def _assert_capabilities(driver: DatabaseDriver) -> None:
    features = driver.features
    _check(type(features) is frozenset, "relational driver capability evidence is mutable")
    _check(
        _REQUIRED_FEATURES.issubset(features),
        "relational driver capability evidence is incomplete",
    )
    for feature in DatabaseFeature:
        supported = feature in features
        _check(
            driver.supports(feature) is supported,
            "relational driver capability query disagrees with its evidence",
        )
        if supported:
            driver.require(feature)
            continue
        captured: UnsupportedDatabaseFeature | None = None
        try:
            driver.require(feature)
        except UnsupportedDatabaseFeature as error:
            captured = error
        _check(captured is not None, "relational driver silently accepted an unsupported feature")
        if captured is not None:
            _check(
                captured.feature is feature,
                "unsupported database feature evidence named the wrong feature",
            )


def _assert_error_redaction(driver: DatabaseDriver) -> None:
    secret = "af_private_secret_relation_9f4d"
    captured: DatabaseOperationError | None = None
    try:
        with driver.transaction(read_only=True) as transaction:
            transaction.query(
                DatabaseStatement(
                    f"SELECT secret_value FROM {secret}",
                    read_only=True,
                )
            )
    except DatabaseOperationError as error:
        captured = error
    _check(captured is not None, "relational driver unexpectedly queried a missing relation")
    if captured is not None:
        _check(
            secret not in str(captured)
            and secret not in repr(captured)
            and "secret_value" not in repr(captured),
            "relational operation error disclosed statement details",
        )


def _assert_shutdown(driver: DatabaseDriver) -> None:
    driver.close()
    driver.close()
    captured: DatabaseOperationError | None = None
    try:
        with driver.transaction():
            pass
    except DatabaseOperationError as error:
        captured = error
    _check(captured is not None, "closed relational driver accepted a new transaction")
    if captured is not None:
        _check(
            captured.reason is PlatformReason.SHUTDOWN_FAILED,
            "closed relational driver failed with the wrong stable reason",
        )


def relational_driver_cases() -> tuple[ConformanceCase[DatabaseDriver], ...]:
    """Return the shared, driver-neutral relational conformance cases."""

    return (
        ConformanceCase("capabilities", _assert_capabilities),
        ConformanceCase("commit", _assert_commit),
        ConformanceCase("rollback", _assert_rollback),
        ConformanceCase("savepoint", _assert_savepoint),
        ConformanceCase("stream-lifetime", _assert_stream_lifetime),
        ConformanceCase("read-only", _assert_read_only),
        ConformanceCase("error-redaction", _assert_error_redaction),
        ConformanceCase("shutdown", _assert_shutdown),
    )


class RelationalDriverConformanceSuite(DriverConformanceSuite[DatabaseDriver]):
    """Certify one real relational driver against the shared AF contract."""

    def __init__(self, name: str = "relational-driver") -> None:
        super().__init__(name, relational_driver_cases())
