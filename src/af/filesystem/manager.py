"""Named filesystem manager with verified capability routing."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from io import BufferedIOBase
from types import MappingProxyType
from typing import Protocol

from af.filesystem.contracts import (
    ByteRange,
    FilesystemEntry,
    FilesystemKey,
    FilesystemMetadata,
    FileVisibility,
    MultipartPart,
    MultipartUpload,
    SignedUrl,
    SignedUrlOperation,
)
from af.framework.container import ProviderRegistrar, Resolver, Scope
from af.framework.managers import (
    CapabilitySet,
    DriverRegistration,
    Manager,
    ManagerConfigurationError,
)
from af.observability.signals import TelemetryPublisher

__all__ = (
    "FilesystemDriver",
    "FilesystemFeature",
    "FilesystemManager",
    "bind_filesystem_manager",
)


class FilesystemFeature(StrEnum):
    """Filesystem capabilities verified independently before use."""

    READ = "read"
    WRITE = "write"
    LIST = "list"
    METADATA = "metadata"
    CHECKSUM = "checksum"
    RANGE = "range"
    COPY = "copy"
    MOVE = "move"
    DELETE = "delete"
    SIGNED_URLS = "signed-urls"
    VISIBILITY = "visibility"
    VERSIONING = "versioning"
    MULTIPART = "multipart"
    STREAMING = "streaming"


class FilesystemDriver(Protocol):
    """Minimum synchronous backend contract owned by FilesystemManager."""

    def read(
        self,
        key: FilesystemKey,
        /,
        *,
        byte_range: ByteRange | None = None,
    ) -> BufferedIOBase: ...

    def write(
        self,
        key: FilesystemKey,
        stream: BufferedIOBase,
        /,
        *,
        visibility: FileVisibility = FileVisibility.PRIVATE,
    ) -> FilesystemMetadata: ...

    def list(self, prefix: FilesystemKey | None = None, /) -> tuple[FilesystemEntry, ...]: ...

    def metadata(self, key: FilesystemKey, /) -> FilesystemMetadata: ...

    def checksum(self, key: FilesystemKey, /) -> str: ...

    def copy(self, source: FilesystemKey, destination: FilesystemKey, /) -> None: ...

    def move(self, source: FilesystemKey, destination: FilesystemKey, /) -> None: ...

    def delete(self, key: FilesystemKey, /) -> bool: ...

    def signed_url(
        self,
        key: FilesystemKey,
        /,
        *,
        expires_at: datetime,
        operation: SignedUrlOperation = SignedUrlOperation.GET,
    ) -> SignedUrl: ...

    def set_visibility(
        self,
        key: FilesystemKey,
        visibility: FileVisibility,
        /,
    ) -> FilesystemMetadata: ...

    def create_multipart_upload(self, key: FilesystemKey, /) -> MultipartUpload: ...

    def upload_part(
        self,
        upload: MultipartUpload,
        part_number: int,
        stream: BufferedIOBase,
        /,
    ) -> MultipartPart: ...

    def complete_multipart_upload(
        self,
        upload: MultipartUpload,
        parts: tuple[MultipartPart, ...],
        /,
    ) -> FilesystemMetadata: ...

    def close(self) -> None: ...


def _capabilities(
    features: CapabilitySet | Iterable[str | FilesystemFeature],
) -> CapabilitySet:
    if isinstance(features, CapabilitySet):
        return features
    if isinstance(features, str):
        return CapabilitySet((features,))
    values: list[str] = []
    for feature in tuple(features):
        if isinstance(feature, FilesystemFeature):
            values.append(feature.value)
        elif type(feature) is str:
            values.append(feature)
        else:
            raise ValueError("filesystem requirements must be capability identifiers")
    return CapabilitySet(values)


class FilesystemManager(Manager[FilesystemDriver]):
    """Application-owned named filesystem connections."""

    def __init__(
        self,
        *,
        connections: Mapping[str, object],
        default: str | None,
        deployment_profile: str,
        policy_version: str,
        clock: Callable[[], datetime] | None = None,
        telemetry: TelemetryPublisher | None = None,
    ) -> None:
        super().__init__(
            manager_id="filesystem",
            connections=connections,
            default=default,
            deployment_profile=deployment_profile,
            policy_version=policy_version,
            clock=clock,
            telemetry=telemetry,
        )

    def install(self, registration: DriverRegistration[FilesystemDriver]) -> None:
        if type(registration) is not DriverRegistration or registration.manager_id != "filesystem":
            raise ManagerConfigurationError(
                protected_cause=ValueError(
                    "driver registration does not target the filesystem manager"
                )
            )
        self.extend(
            registration.driver_name,
            registration.factory,
            manifest=registration.manifest,
            verifier=registration.verifier,
            disposer=registration.disposer,
        )

    def connection(
        self,
        name: str | None = None,
        *,
        requires: CapabilitySet | Iterable[str | FilesystemFeature] = (),
    ) -> FilesystemDriver:
        return super().connection(name, requires=_capabilities(requires))

    def read(
        self,
        key: FilesystemKey,
        *,
        name: str | None = None,
        byte_range: ByteRange | None = None,
    ) -> BufferedIOBase:
        required = [FilesystemFeature.READ]
        if byte_range is not None:
            required.append(FilesystemFeature.RANGE)
        return self.connection(name, requires=required).read(key, byte_range=byte_range)

    def write(
        self,
        key: FilesystemKey,
        stream: BufferedIOBase,
        *,
        name: str | None = None,
        visibility: FileVisibility = FileVisibility.PRIVATE,
    ) -> FilesystemMetadata:
        required = [FilesystemFeature.WRITE]
        if visibility is not FileVisibility.PRIVATE:
            required.append(FilesystemFeature.VISIBILITY)
        return self.connection(name, requires=required).write(key, stream, visibility=visibility)

    def list(
        self,
        prefix: FilesystemKey | None = None,
        *,
        name: str | None = None,
    ) -> tuple[FilesystemEntry, ...]:
        return self.connection(name, requires=(FilesystemFeature.LIST,)).list(prefix)

    def metadata(self, key: FilesystemKey, *, name: str | None = None) -> FilesystemMetadata:
        return self.connection(name, requires=(FilesystemFeature.METADATA,)).metadata(key)

    def checksum(self, key: FilesystemKey, *, name: str | None = None) -> str:
        return self.connection(name, requires=(FilesystemFeature.CHECKSUM,)).checksum(key)

    def copy(
        self,
        source: FilesystemKey,
        destination: FilesystemKey,
        *,
        name: str | None = None,
    ) -> None:
        self.connection(name, requires=(FilesystemFeature.COPY,)).copy(source, destination)

    def move(
        self,
        source: FilesystemKey,
        destination: FilesystemKey,
        *,
        name: str | None = None,
    ) -> None:
        self.connection(name, requires=(FilesystemFeature.MOVE,)).move(source, destination)

    def delete(self, key: FilesystemKey, *, name: str | None = None) -> bool:
        return self.connection(name, requires=(FilesystemFeature.DELETE,)).delete(key)

    def signed_url(
        self,
        key: FilesystemKey,
        *,
        expires_at: datetime,
        operation: SignedUrlOperation = SignedUrlOperation.GET,
        name: str | None = None,
    ) -> SignedUrl:
        return self.connection(name, requires=(FilesystemFeature.SIGNED_URLS,)).signed_url(
            key,
            expires_at=expires_at,
            operation=operation,
        )

    def set_visibility(
        self,
        key: FilesystemKey,
        visibility: FileVisibility,
        *,
        name: str | None = None,
    ) -> FilesystemMetadata:
        return self.connection(name, requires=(FilesystemFeature.VISIBILITY,)).set_visibility(
            key,
            visibility,
        )

    def create_multipart_upload(
        self,
        key: FilesystemKey,
        *,
        name: str | None = None,
    ) -> MultipartUpload:
        driver = self.connection(name, requires=(FilesystemFeature.MULTIPART,))
        return driver.create_multipart_upload(key)

    def upload_part(
        self,
        upload: MultipartUpload,
        part_number: int,
        stream: BufferedIOBase,
        *,
        name: str | None = None,
    ) -> MultipartPart:
        return self.connection(name, requires=(FilesystemFeature.MULTIPART,)).upload_part(
            upload,
            part_number,
            stream,
        )

    def complete_multipart_upload(
        self,
        upload: MultipartUpload,
        parts: tuple[MultipartPart, ...],
        *,
        name: str | None = None,
    ) -> FilesystemMetadata:
        return self.connection(
            name,
            requires=(FilesystemFeature.MULTIPART,),
        ).complete_multipart_upload(upload, parts)


def bind_filesystem_manager(
    registrar: ProviderRegistrar,
    *,
    connections: Mapping[str, object],
    default: str | None,
    deployment_profile: str,
    policy_version: str,
    registrations: Iterable[DriverRegistration[FilesystemDriver]],
    clock: Callable[[], datetime] | None = None,
    telemetry: TelemetryPublisher | None = None,
) -> None:
    """Bind one application-owned FilesystemManager from inert registrations."""

    snapshot = tuple(registrations)
    if (
        not snapshot
        or any(
            type(registration) is not DriverRegistration or registration.manager_id != "filesystem"
            for registration in snapshot
        )
        or len({registration.driver_name for registration in snapshot}) != len(snapshot)
    ):
        raise ManagerConfigurationError(
            protected_cause=ValueError(
                "filesystem registrations must be non-empty, unique, and targeted"
            )
        )

    template = FilesystemManager(
        connections=connections,
        default=default,
        deployment_profile=deployment_profile,
        policy_version=policy_version,
        clock=clock,
    )
    connection_snapshot: Mapping[str, object] = MappingProxyType(
        {name: definition[1] for name, definition in template._definitions.items()}
    )
    template.close()

    def create_manager(_resolver: Resolver) -> FilesystemManager:
        manager = FilesystemManager(
            connections=connection_snapshot,
            default=default,
            deployment_profile=deployment_profile,
            policy_version=policy_version,
            clock=clock,
            telemetry=telemetry,
        )
        for registration in snapshot:
            manager.install(registration)
        return manager

    registrar.bind(
        FilesystemManager,
        create_manager,
        scope=Scope.APPLICATION,
        owned=True,
        replace=False,
    )
