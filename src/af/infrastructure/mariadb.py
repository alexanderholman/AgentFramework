"""MariaDB relational driver over the verified PyMySQL transport."""

from __future__ import annotations

from threading import Event, RLock

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from af.framework.errors import PlatformReason
from af.framework.relational import DatabaseFeature, DatabaseOperationError
from af.infrastructure.mysql import MySQLDriver

__all__ = ("MariaDBDriver",)

_CORE_FEATURES = frozenset(
    {
        DatabaseFeature.TRANSACTIONS,
        DatabaseFeature.SAVEPOINTS,
        DatabaseFeature.STREAMING,
    }
)


def _connect_failure(error: BaseException) -> DatabaseOperationError:
    return DatabaseOperationError(
        reason=PlatformReason.TRANSIENT_FAILURE,
        stage="database.connect",
        protected_cause=error,
    )


class MariaDBDriver(MySQLDriver):
    """One MariaDB connection using the shared MySQL-family transaction core."""

    def __init__(self, url: str) -> None:
        if type(url) is not str or not url:
            raise ValueError("MariaDB URL must be non-empty")
        try:
            parsed = make_url(url)
        except (TypeError, ValueError):
            raise ValueError("MariaDB URL is invalid") from None
        if parsed.drivername != "mariadb+pymysql":
            raise ValueError("MariaDB driver requires a mariadb+pymysql URL")
        try:
            engine = create_engine(url, poolclass=StaticPool)
        except (ImportError, SQLAlchemyError) as error:
            raise _connect_failure(error) from None
        try:
            connection = engine.connect()
        except SQLAlchemyError as error:
            engine.dispose()
            raise _connect_failure(error) from None

        self._engine: Engine = engine
        self._connection = connection
        self._lock = RLock()
        self._cancellation = Event()
        self._transaction_active = False
        self._closed = False
        self._url = url

        raw = self._connection.connection.driver_connection
        thread_id = getattr(raw, "thread_id", None)
        resolved_thread_id = thread_id() if callable(thread_id) else None
        self._thread_id = (
            resolved_thread_id
            if type(resolved_thread_id) is int and resolved_thread_id > 0
            else None
        )
        features = set(_CORE_FEATURES)
        if self._thread_id is not None:
            features.add(DatabaseFeature.CANCELLATION)
        self._features = frozenset(features)

    def __enter__(self) -> MariaDBDriver:
        self._assert_open()
        return self
