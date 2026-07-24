"""Application provider for immutable migration catalogues."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from itertools import islice

from af.framework.container import ProviderRegistrar, Resolver, Scope, ServiceProvider
from af.framework.database_manager import DatabaseManager
from af.framework.migrations import Migration, MigrationRunner
from af.framework.relational import DatabaseFeature

__all__ = ("MigrationProvider",)

_MAX_CATALOGUE = 10_000


class MigrationProvider(ServiceProvider):
    """Bind one migration catalogue to an explicit named database connection."""

    provider_id = "af.migrations"

    def __init__(
        self,
        *,
        migrations: Iterable[Migration],
        connection: str,
        owner_id: str,
        clock: Callable[[], datetime],
        lock_lease: timedelta = timedelta(minutes=5),
    ) -> None:
        snapshot = tuple(islice(iter(migrations), _MAX_CATALOGUE + 1))
        if len(snapshot) > _MAX_CATALOGUE or any(
            not isinstance(migration, Migration) for migration in snapshot
        ):
            raise ValueError("migration catalogue is invalid or too large")
        if type(connection) is not str or not connection:
            raise ValueError("migration connection must be a named database connection")
        if type(owner_id) is not str or not owner_id:
            raise ValueError("migration owner must be non-empty")
        if not callable(clock):
            raise TypeError("migration clock must be callable")
        if (
            not isinstance(lock_lease, timedelta)
            or lock_lease <= timedelta(0)
            or lock_lease > timedelta(hours=24)
        ):
            raise ValueError("migration lock lease is invalid")
        self._migrations = snapshot
        self._connection = connection
        self._owner_id = owner_id
        self._clock = clock
        self._lock_lease = lock_lease

    def register(self, registrar: ProviderRegistrar) -> None:
        migrations = self._migrations
        connection = self._connection
        owner_id = self._owner_id
        clock = self._clock
        lock_lease = self._lock_lease

        def create_runner(resolver: Resolver) -> MigrationRunner:
            manager = resolver.resolve(DatabaseManager)
            driver = manager.connection(
                connection,
                requires=(
                    DatabaseFeature.TRANSACTIONS,
                    DatabaseFeature.STREAMING,
                ),
            )
            return MigrationRunner(
                driver,
                migrations,
                owner_id=owner_id,
                clock=clock,
                lock_lease=lock_lease,
            )

        registrar.bind(
            MigrationRunner,
            create_runner,
            scope=Scope.APPLICATION,
            owned=False,
        )
