"""Application boundary for AF version-control initialization."""

from __future__ import annotations

from af.framework.version_control import (
    VersionControlBackend,
    VersionControlInitialization,
    VersionControlRequest,
)


class VersionControlController:
    def __init__(self, backend: VersionControlBackend) -> None:
        self._backend = backend

    def initialize(self, request: VersionControlRequest) -> VersionControlInitialization:
        return self._backend.initialize(request)
