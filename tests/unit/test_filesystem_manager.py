from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest

from af.filesystem.contracts import (
    ByteRange,
    FilesystemKey,
    FilesystemOperationError,
    FileVisibility,
    MultipartPart,
    MultipartUpload,
)
from af.filesystem.local import local_filesystem_registration
from af.filesystem.manager import (
    FilesystemFeature,
    FilesystemManager,
    bind_filesystem_manager,
)
from af.filesystem.memory import memory_filesystem_registration
from af.framework.application import Application
from af.framework.container import Container, ProviderRegistrar, ServiceProvider
from af.framework.errors import PlatformReason
from af.framework.managers import (
    CapabilitySet,
    CapabilityValidationError,
    DriverRegistration,
    DriverResolutionError,
    ManagerConfigurationError,
)

_NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = _NOW

    def __call__(self) -> datetime:
        return self.now


def _read_all(manager: FilesystemManager, key: FilesystemKey, *, name: str | None = None) -> bytes:
    with manager.read(key, name=name) as stream:
        return stream.read()


def test_memory_filesystem_round_trips_and_reports_unsupported_operations() -> None:
    clock = Clock()
    manager = FilesystemManager(
        connections={"primary": {"driver": "memory"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    manager.install(memory_filesystem_registration(clock=clock))
    key = FilesystemKey("docs/report.txt")
    copy_key = FilesystemKey("docs/report-copy.txt")
    moved_key = FilesystemKey("archive/report.txt")

    written = manager.write(key, BytesIO(b"hello world"))

    assert written.size == 11
    assert written.checksum_sha256 == hashlib.sha256(b"hello world").hexdigest()
    assert _read_all(manager, key) == b"hello world"
    with manager.read(key, byte_range=ByteRange(start=6)) as stream:
        assert stream.read() == b"world"
    assert manager.checksum(key) == hashlib.sha256(b"hello world").hexdigest()
    assert [entry.key for entry in manager.list(FilesystemKey("docs"))] == [key]

    manager.copy(key, copy_key)
    manager.move(copy_key, moved_key)

    assert _read_all(manager, moved_key) == b"hello world"
    assert not manager.delete(copy_key)
    assert manager.delete(moved_key)

    with pytest.raises(CapabilityValidationError) as signed_url_error:
        manager.signed_url(key, expires_at=_NOW + timedelta(minutes=5))
    assert signed_url_error.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE

    with pytest.raises(CapabilityValidationError) as visibility_error:
        manager.set_visibility(key, FileVisibility.PUBLIC)
    assert visibility_error.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE

    manager.close()


def test_local_filesystem_persists_to_root_and_blocks_symlink_escape(tmp_path: Path) -> None:
    clock = Clock()
    root = tmp_path / "storage"
    manager = FilesystemManager(
        connections={"primary": {"driver": "local", "root": str(root)}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    manager.install(local_filesystem_registration(clock=clock))
    key = FilesystemKey("nested/report.bin")

    manager.write(key, BytesIO(b"abcdef"))

    assert (root / "nested" / "report.bin").read_bytes() == b"abcdef"
    with manager.read(key, byte_range=ByteRange(start=2, end=4)) as stream:
        assert stream.read() == b"cde"

    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "evil").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FilesystemOperationError) as captured:
        manager.write(FilesystemKey("evil/escape.bin"), BytesIO(b"x"))
    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE

    manager.close()


def test_filesystem_manager_routes_named_memory_and_local_connections(tmp_path: Path) -> None:
    clock = Clock()
    manager = FilesystemManager(
        connections={
            "memory": {"driver": "memory"},
            "archive": {"driver": "local", "root": str(tmp_path / "archive")},
        },
        default="memory",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    manager.install(memory_filesystem_registration(clock=clock))
    manager.install(local_filesystem_registration(clock=clock))
    key = FilesystemKey("shared/report.txt")

    manager.write(key, BytesIO(b"memory"), name="memory")
    manager.write(key, BytesIO(b"archive"), name="archive")

    assert _read_all(manager, key, name="memory") == b"memory"
    assert _read_all(manager, key, name="archive") == b"archive"
    assert (
        manager.connection(requires=CapabilitySet((FilesystemFeature.READ.value,)))
        is manager.connection()
    )
    assert manager.connection(requires=FilesystemFeature.RANGE.value) is manager.connection()

    manager.close()


def test_filesystem_manager_rejects_untargeted_registration() -> None:
    clock = Clock()
    manager = FilesystemManager(
        connections={"primary": {"driver": "memory"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    registration = memory_filesystem_registration(clock=clock)
    untargeted = DriverRegistration(
        manager_id="cache",
        driver_name=registration.driver_name,
        factory=registration.factory,
        manifest=registration.manifest,
        verifier=registration.verifier,
        disposer=registration.disposer,
    )

    with pytest.raises(ManagerConfigurationError):
        manager.install(untargeted)
    manager.close()


def test_filesystem_manager_rejects_invalid_capability_requests_and_unsupported_multipart() -> None:
    clock = Clock()
    manager = FilesystemManager(
        connections={"primary": {"driver": "memory"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    manager.install(memory_filesystem_registration(clock=clock))
    key = FilesystemKey("docs/report.txt")
    upload = MultipartUpload(key=key, upload_id="upload-1")
    part = MultipartPart(part_number=1, checksum_sha256="a" * 64)

    with pytest.raises(ValueError):
        manager.connection(requires=(object(),))  # type: ignore[arg-type]
    with pytest.raises(CapabilityValidationError):
        manager.write(key, BytesIO(b"x"), visibility=FileVisibility.PUBLIC)
    with pytest.raises(CapabilityValidationError):
        manager.create_multipart_upload(key)
    with pytest.raises(CapabilityValidationError):
        manager.upload_part(upload, 1, BytesIO(b"x"))
    with pytest.raises(CapabilityValidationError):
        manager.complete_multipart_upload(upload, (part,))

    manager.close()


def test_filesystem_manager_is_application_owned_and_snapshots_configuration(
    tmp_path: Path,
) -> None:
    clock = Clock()
    connections = {"primary": {"driver": "local", "root": str(tmp_path / "primary")}}

    class FilesystemProvider(ServiceProvider):
        provider_id = "filesystem"

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_filesystem_manager(
                registrar,
                connections=connections,
                default="primary",
                deployment_profile="test",
                policy_version="policy-v1",
                registrations=(local_filesystem_registration(clock=clock),),
                clock=clock,
            )

    application = Application(Container(), (FilesystemProvider(),))
    application.register()
    connections["primary"]["root"] = str(tmp_path / "mutated")
    application.boot()
    application.serve()

    manager = application.container.resolve(FilesystemManager)
    key = FilesystemKey("docs/report.txt")
    manager.write(key, BytesIO(b"application-owned"))
    assert _read_all(manager, key) == b"application-owned"

    application.shutdown()
    with pytest.raises(DriverResolutionError) as captured:
        manager.metadata(key)
    assert captured.value.reason is PlatformReason.DEPENDENCY_RESOLUTION_FAILED


def test_filesystem_provider_rejects_missing_or_duplicate_registrations() -> None:
    class Registrar:
        def bind(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            del args, kwargs

    clock = Clock()
    registration = memory_filesystem_registration(clock=clock)
    common = {
        "connections": {"primary": {"driver": "memory"}},
        "default": "primary",
        "deployment_profile": "test",
        "policy_version": "policy-v1",
        "clock": clock,
    }
    with pytest.raises(ManagerConfigurationError):
        bind_filesystem_manager(
            Registrar(),
            registrations=(),
            **common,  # type: ignore[arg-type]
        )
    with pytest.raises(ManagerConfigurationError):
        bind_filesystem_manager(
            Registrar(),  # type: ignore[arg-type]
            registrations=(registration, registration),
            **common,
        )
