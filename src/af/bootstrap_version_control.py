"""Lightweight composition root for repository-local version control."""

from af.application.version_control import VersionControlController
from af.infrastructure.git_version_control import GitVersionControlBackend


def create_version_control_controller() -> VersionControlController:
    """Compose version control without booting AF's application/database lifecycle."""
    return VersionControlController(GitVersionControlBackend())
