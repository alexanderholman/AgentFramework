"""Verified persistent file cache with tenant-scoped paths and atomic replacement."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from json import JSONDecodeError
from pathlib import Path
from threading import RLock

from af.cache.contracts import (
    CacheKey,
    CacheNamespace,
    _validated_operation,
    _validated_ttl,
    _validated_value,
)
from af.cache.manager import CacheDriver, CacheFeature
from af.framework.errors import PlatformError, PlatformReason
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
    HealthStatus,
)

__all__ = ("FileCacheDriver", "file_cache_registration")

_CAPABILITIES = CapabilitySet(
    (
        CacheFeature.READ.value,
        CacheFeature.WRITE.value,
        CacheFeature.TTL.value,
        CacheFeature.NAMESPACES.value,
        CacheFeature.FLUSH.value,
    )
)
_ARTIFACT_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_DEPENDENCY_DIGEST = hashlib.sha256(b"python:stdlib:file-cache:v1").hexdigest()
_FRAME_KEYS = {
    "application_id",
    "expires_at",
    "key",
    "namespace",
    "schema",
    "tenant_id",
    "value",
}
_MAX_FRAME_BYTES = 1_500_000


class FileCacheDriver:
    """Persistent cache whose paths are digests of validated tenant identities."""

    def __init__(
        self,
        *,
        root: str | Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        candidate = Path(root)
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("file cache root must be an absolute canonical path")
        candidate.mkdir(parents=True, exist_ok=True)
        resolved = candidate.resolve(strict=True)
        if resolved != candidate or not resolved.is_dir():
            raise ValueError("file cache root must be an absolute canonical directory")
        self._root = resolved
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        if not callable(self._clock):
            raise TypeError("file cache clock must be callable")
        self._lock = RLock()
        self._closed = False

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise PlatformError(
                PlatformReason.PERMANENT_FAILURE,
                stage="cache.file.clock",
            )
        return value.astimezone(UTC)

    def _assert_open(self) -> None:
        if self._closed:
            raise PlatformError(
                PlatformReason.SHUTDOWN_FAILED,
                stage="cache.file.connection",
            )

    @staticmethod
    def _identity(namespace: CacheNamespace) -> str:
        canonical = f"{namespace.application_id}\x00{namespace.tenant_id}\x00{namespace.name}"
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _key_identity(key: CacheKey) -> str:
        return hashlib.sha256(key.value.encode()).hexdigest()

    def _directory(self, namespace: CacheNamespace) -> Path:
        return self._root / self._identity(namespace)

    def _path(self, namespace: CacheNamespace, key: CacheKey) -> Path:
        return self._directory(namespace) / f"{self._key_identity(key)}.afc"

    @staticmethod
    def _frame(
        namespace: CacheNamespace,
        key: CacheKey,
        value: bytes,
        expires_at: datetime | None,
    ) -> bytes:
        payload = {
            "application_id": namespace.application_id,
            "expires_at": None if expires_at is None else expires_at.isoformat(),
            "key": key.value,
            "namespace": namespace.name,
            "schema": 1,
            "tenant_id": namespace.tenant_id,
            "value": base64.b64encode(value).decode("ascii"),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    @staticmethod
    def _matches(
        payload: Mapping[str, object],
        namespace: CacheNamespace,
        key: CacheKey,
    ) -> bool:
        return (
            set(payload) == _FRAME_KEYS
            and payload.get("schema") == 1
            and payload.get("application_id") == namespace.application_id
            and payload.get("tenant_id") == namespace.tenant_id
            and payload.get("namespace") == namespace.name
            and payload.get("key") == key.value
        )

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return

    def _read(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        path: Path,
    ) -> bytes | None:
        try:
            if path.is_symlink() or path.stat().st_size > _MAX_FRAME_BYTES:
                raise ValueError("unsafe file cache entry")
            payload = json.loads(path.read_bytes())
            if not isinstance(payload, dict) or not self._matches(payload, namespace, key):
                raise ValueError("file cache identity mismatch")
            expires_raw = payload["expires_at"]
            if expires_raw is None:
                expires_at = None
            elif type(expires_raw) is str:
                expires_at = datetime.fromisoformat(expires_raw)
                if expires_at.utcoffset() is None:
                    raise ValueError("file cache expiry must be timezone-aware")
                expires_at = expires_at.astimezone(UTC)
            else:
                raise ValueError("file cache expiry is invalid")
            value_raw = payload["value"]
            if type(value_raw) is not str:
                raise ValueError("file cache value is invalid")
            value = _validated_value(base64.b64decode(value_raw, validate=True))
            if expires_at is not None and expires_at <= self._now():
                self._discard(path)
                return None
            return value
        except (
            OSError,
            UnicodeDecodeError,
            JSONDecodeError,
            ValueError,
            TypeError,
            binascii.Error,
        ):
            self._discard(path)
            return None

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        if directory_flag == 0:
            return
        descriptor = os.open(directory, os.O_RDONLY | directory_flag)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def get(self, namespace: CacheNamespace, key: CacheKey, /) -> bytes | None:
        namespace, key = _validated_operation(namespace, key)
        with self._lock:
            self._assert_open()
            path = self._path(namespace, key)
            if not path.exists() and not path.is_symlink():
                return None
            return self._read(namespace, key, path)

    def set(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        value: bytes,
        /,
        *,
        ttl: timedelta | None = None,
    ) -> None:
        namespace, key = _validated_operation(namespace, key)
        value = _validated_value(value)
        ttl = _validated_ttl(ttl)
        with self._lock:
            self._assert_open()
            expires_at = None if ttl is None else self._now() + ttl
            frame = self._frame(namespace, key, value, expires_at)
            directory = self._directory(namespace)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink() or directory.resolve(strict=True) != directory:
                raise PlatformError(
                    PlatformReason.PERMANENT_FAILURE,
                    stage="cache.file.path",
                )
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".af-cache-",
                suffix=".tmp",
                dir=directory,
            )
            temporary = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(frame)
                    stream.flush()
                    os.fsync(stream.fileno())
                path = self._path(namespace, key)
                os.replace(temporary, path)
                path.chmod(0o600)
                self._sync_directory(directory)
            finally:
                self._discard(temporary)

    def delete(self, namespace: CacheNamespace, key: CacheKey, /) -> bool:
        namespace, key = _validated_operation(namespace, key)
        with self._lock:
            self._assert_open()
            path = self._path(namespace, key)
            if self._read(namespace, key, path) is None:
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            return True

    def flush(self, namespace: CacheNamespace, /) -> int:
        if not isinstance(namespace, CacheNamespace):
            raise TypeError("cache flush requires a validated namespace")
        with self._lock:
            self._assert_open()
            directory = self._directory(namespace)
            if not directory.exists() or directory.is_symlink():
                return 0
            paths = tuple(directory.glob("*.afc"))
            removed = 0
            for path in paths:
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                removed += 1
            with suppress(OSError):
                directory.rmdir()
            return removed

    def acquire_lock(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise NotImplementedError("file cache does not advertise lock capability")

    def renew_lock(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise NotImplementedError("file cache does not advertise lock capability")

    def release_lock(self, *args: object, **kwargs: object) -> bool:
        del args, kwargs
        raise NotImplementedError("file cache does not advertise lock capability")

    def close(self) -> None:
        with self._lock:
            self._closed = True


def _evidence_digest(
    binding: CapabilityBinding,
    capabilities: CapabilitySet,
    observed_at: datetime,
) -> str:
    payload = {
        "artifact": binding.artifact_digest,
        "capabilities": tuple(capabilities),
        "configuration": binding.configuration_digest,
        "dependencies": binding.dependency_graph_digest,
        "observed_at": observed_at.isoformat(),
        "policy": binding.policy_version,
        "profile": binding.deployment_profile,
        "version": binding.driver_version,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_cache_registration(
    *,
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[CacheDriver]:
    """Return an inert file-cache declaration with runtime evidence."""

    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="file",
        version="1",
        artifact_digest=_ARTIFACT_DIGEST,
        dependency_graph_digest=_DEPENDENCY_DIGEST,
        capabilities=_CAPABILITIES,
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> CacheDriver:
        path = config.get("path")
        if (
            ref.manager_id != "cache"
            or ref.driver_name != "file"
            or set(config) != {"driver", "path"}
            or config.get("driver") != "file"
            or type(path) is not str
            or not path
        ):
            raise ValueError("file cache connection configuration is invalid")
        return FileCacheDriver(root=path, clock=evidence_clock)

    def verify(
        ref: ConnectionRef,
        driver: CacheDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if (
            ref.manager_id != "cache"
            or ref.driver_name != "file"
            or type(driver) is not FileCacheDriver
        ):
            raise ValueError("file cache runtime capability verification failed")
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("file cache evidence clock must be timezone-aware")
        observed_at = observed_at.astimezone(UTC)
        return CapabilityEvidence(
            binding=expected,
            capabilities=_CAPABILITIES,
            health=HealthStatus.HEALTHY,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(minutes=1),
            evidence_digest=_evidence_digest(expected, _CAPABILITIES, observed_at),
        )

    return DriverRegistration(
        manager_id="cache",
        driver_name="file",
        factory=factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
