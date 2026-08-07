"""Application boundary for explicit local database setup."""

from __future__ import annotations

from af.framework.database_setup import (
    DatabaseSetupBackend,
    DatabaseSetupRequest,
    DatabaseSetupResult,
)


class DatabaseSetupController:
    def __init__(self, backend: DatabaseSetupBackend) -> None:
        self._backend = backend

    def setup(self, request: DatabaseSetupRequest) -> DatabaseSetupResult:
        return self._backend.setup(request)
