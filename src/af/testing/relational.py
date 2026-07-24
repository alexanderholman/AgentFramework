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


def _fail(message: str) -> None:
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


def _insert(
    driver: DatabaseDriver,
    *,
    record_id: int,
    value: str,
) -> None:
    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                f"INSERT INTO {_TABLE} (id, tenant, value) "
                "VALUES (:id, :tenant, :value)",
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
    rows = _rows(driver)
    expected = [{"id": 1, "tenant": "tenant-a", "value": "committed"}]
    if [row.as_mapping() for row in rows] != expected:
        _fail("relational driver did not commit transaction work")


def _assert_rollback(driver: DatabaseDriver) -> None:
    _reset_schema(driver)
    try:
        with driver.transaction() as transaction:
            transaction.execute(
                DatabaseStatement(
                    f"INSERT INTO {_TABLE} (id, tenant, value) "
                    "VALUES (:id, :tenant, :value)",
                    {"id": 1, "tenant": "tenant-a", "value": "discarded"},
                )
            )
            raise _ExpectedRollback("rollback")
    except _ExpectedRollback:
        pass
    else:
        _fail("relational transaction suppressed the caller failure")
    if _rows(driver):
        _fail("relational driver committed work from a failed transaction")


def _assert_savepoint(driver: DatabaseDriver) -> None:
    _reset_schema(driver)
    driver.require(DatabaseFeature.SAVEPOINTS)
    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                f"INSERT INTO {_TABLE} (id, tenant, value) "
                "VALUES (:id, :tenant, :value)",
                {"id": 1, "tenant": "tenant-a", "value": "kept"},
            )
        )
        try:
            with transaction.savepoint("af_nested"):
                transaction.execute(
                    DatabaseStatement(
                        f"INSERT INTO {_TABLE} (id, tenant, value) "
                        "VALUES (:id, :tenant, :value)",
                        {"id": 2, "tenant": "tenant-a", "value": "discarded"},
                    )
                )
                raise _ExpectedRollback("savepoint")
        except _ExpectedRollback:
            pass
        else:
            _fail("relational savepoint suppressed the caller failure")
    values = [row["value"] for row in _rows(driver)]
    if values != ["kept"]:
        _fail("relational savepoint did not isolate nested rollback")


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
        if next(cursor)["value"] != "first":
            _fail("relational stream returned rows out of order")
    try:
        next(cursor)
    except DatabaseOperationError:
        pass
    else:
        _fail("relational cursor remained usable after its transaction ended")


def _assert_read_only(driver: DatabaseDriver) -> None:
    _reset_schema(driver)
    with driver.transaction(read_only=True) as transaction:
        try:
            transaction.execute(
                DatabaseStatement(
                    f"INSERT INTO {_TABLE} (id, tenant, value) "
                    "VALUES (1, 'tenant-a', 'denied')"
                )
            )
        except DatabaseOperationError as error:
            if error.reason is not PlatformReason.AUTHORIZATION_DENIED:
                _fail("read-only execution failed with the wrong stable reason")
        else:
            _fail("read-only transaction accepted a write statement")

    with driver.transaction(read_only=True) as transaction:
        try:
            transaction.query(
                DatabaseStatement(
                    f"DELETE FROM {_TABLE}",
                    read_only=True,
                )
            )
        except DatabaseOperationError:
            pass
        else:
            _fail("statement metadata bypassed database read-only enforcement")


def _assert_capabilities(driver: DatabaseDriver) -> None:
    features = driver.features
    if type(features) is not frozenset or not _REQUIRED_FEATURES.issubset(features):
        _fail("relational driver capability evidence is incomplete or mutable")
    for feature in DatabaseFeature:
        supported = feature in features
        if driver.supports(feature) is not supported:
            _fail("relational driver capability query disagrees with its evidence")
        if supported:
            driver.require(feature)
            continue
        try:
            driver.require(feature)
        except UnsupportedDatabaseFeature as error:
            if error.feature is not feature:
                _fail("unsupported database feature evidence named the wrong feature")
        else:
            _fail("relational driver silently accepted an unsupported feature")


def _assert_error_redaction(driver: DatabaseDriver) -> None:
    secret = "af_private_secret_relation_9f4d"
    try:
        with driver.transaction(read_only=True) as transaction:
            transaction.query(
                DatabaseStatement(
                    f"SELECT secret_value FROM {secret}",
                    read_only=True,
                )
            )
    except DatabaseOperationError as error:
        if secret in str(error) or secret in repr(error) or "secret_value" in repr(error):
            _fail("relational operation error disclosed statement details")
    else:
        _fail("relational driver unexpectedly queried a missing private relation")


def _assert_shutdown(driver: DatabaseDriver) -> None:
    driver.close()
    driver.close()
    try:
        with driver.transaction():
            pass
    except DatabaseOperationError as error:
        if error.reason is not PlatformReason.SHUTDOWN_FAILED:
            _fail("closed relational driver failed with the wrong stable reason")
    else:
        _fail("closed relational driver accepted a new transaction")


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
