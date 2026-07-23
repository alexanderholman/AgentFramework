from __future__ import annotations

import pytest

from af.bootstrap import create_application
from af.config import ConfigSnapshot, MappingConfigSource, Settings, load_settings
from af.framework.errors import InvalidBinding


def test_application_preserves_explicit_settings_and_snapshot_identity() -> None:
    settings = Settings(port=8111)
    explicit = create_application(settings=settings)
    assert explicit.settings is settings
    assert explicit.config.settings is settings
    assert explicit.container.resolve(ConfigSnapshot) is explicit.config

    snapshot = load_settings(MappingConfigSource("file", {"port": 8222}))
    configured = create_application(config=snapshot)
    assert configured.config is snapshot
    assert configured.settings is snapshot.settings


def test_application_rejects_settings_and_snapshot_together() -> None:
    with pytest.raises(ValueError, match="settings or config"):
        create_application(settings=Settings(), config=ConfigSnapshot.explicit(Settings()))


def test_application_does_not_capture_invocation_context() -> None:
    from af.framework.context import ExecutionContext, maybe_current_context, use_context

    with use_context(ExecutionContext(tenant_id="tenant-private")):
        application = create_application()
    assert maybe_current_context() is None
    assert "tenant-private" not in repr(application.config)


def test_application_freezes_container_before_provider_boot() -> None:
    application = create_application()

    with pytest.raises(InvalidBinding, match="frozen"):
        application.container.instance(Settings, Settings(port=8999), replace=True)
