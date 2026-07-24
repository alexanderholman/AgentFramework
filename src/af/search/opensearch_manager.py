"""Verified OpenSearch registration for SearchManager."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import stat
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Protocol, cast
from urllib.parse import urlsplit

from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
    HealthStatus,
)
from af.search.manager import SearchDriver, SearchFeature
from af.search.opensearch import OpenSearchClient, OpenSearchDriver

__all__ = ("opensearch_registration",)

_CAPABILITIES = CapabilitySet(
    (
        SearchFeature.POLICY_PUSHDOWN.value,
        SearchFeature.FULL_TEXT.value,
        SearchFeature.INDEXING.value,
        SearchFeature.TOMBSTONES.value,
        SearchFeature.PERSISTENCE.value,
    )
)
_UNSAFE_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?")
_MAX_RUNTIME_FILES = 4_096
_MAX_RUNTIME_BYTES = 64 * 1_024 * 1_024
_RUNTIME_READ_BYTES = 64 * 1_024


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


def _hash_file(
    digest: _Digest,
    *,
    label: str,
    path: Path,
    remaining_bytes: int,
) -> int:
    if remaining_bytes < 0 or path.is_symlink():
        raise ValueError("OpenSearch runtime identity is unavailable")
    encoded_label = label.encode()
    digest.update(len(encoded_label).to_bytes(4, "big"))
    digest.update(encoded_label)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            before = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size < 0
                or before.st_size > remaining_bytes
            ):
                raise ValueError("OpenSearch runtime identity is unavailable")
            digest.update(before.st_size.to_bytes(8, "big"))
            pending = before.st_size
            observed = 0
            while pending:
                chunk = handle.read(min(_RUNTIME_READ_BYTES, pending))
                if not chunk:
                    raise ValueError("OpenSearch runtime identity is unavailable")
                digest.update(chunk)
                observed += len(chunk)
                pending -= len(chunk)
            if handle.read(1):
                raise ValueError("OpenSearch runtime identity is unavailable")
            after = os.fstat(handle.fileno())
    except OSError as error:
        raise ValueError("OpenSearch runtime identity is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    before_state = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    )
    after_state = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    )
    if observed != before.st_size or after_state != before_state:
        raise ValueError("OpenSearch runtime identity is unavailable")
    return observed


def _runtime_identity() -> tuple[str, str, str]:
    spec = importlib.util.find_spec("opensearchpy")
    if spec is None or spec.origin is None:
        raise ValueError("OpenSearch registration requires the opensearch-py runtime")
    try:
        client_version = importlib.metadata.version("opensearch-py")
        distribution = importlib.metadata.distribution("opensearch-py")
        distribution_files = distribution.files
        requirements = tuple(distribution.requires or ())
    except (OSError, importlib.metadata.PackageNotFoundError) as error:
        raise ValueError("OpenSearch runtime identity is unavailable") from error

    client_files: list[tuple[str, Path]] = []
    client_labels: set[str] = set()
    for item in distribution_files or ():
        relative = PurePosixPath(str(item))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or relative.parts[0] != "opensearchpy"
            or "__pycache__" in relative.parts
            or relative.suffix == ".pyc"
        ):
            continue
        label = relative.as_posix()
        if label in client_labels or len(client_files) >= _MAX_RUNTIME_FILES:
            raise ValueError("OpenSearch runtime identity is unavailable")
        client_labels.add(label)
        client_files.append((label, Path(str(distribution.locate_file(item)))))
    client_files.sort(key=lambda item: item[0])
    if not client_files:
        raise ValueError("OpenSearch runtime identity is unavailable")
    client_entrypoint = next(
        (path for label, path in client_files if label == "opensearchpy/__init__.py"),
        None,
    )
    import_origin = Path(spec.origin)
    try:
        if (
            client_entrypoint is None
            or not import_origin.is_absolute()
            or import_origin.is_symlink()
            or client_entrypoint.is_symlink()
            or not import_origin.samefile(client_entrypoint)
        ):
            raise ValueError("OpenSearch runtime identity is unavailable")
    except OSError as error:
        raise ValueError("OpenSearch runtime identity is unavailable") from error

    artifact = hashlib.sha256()
    total_bytes = 0
    source_files = (
        ("af/search/opensearch.py", Path(__file__).with_name("opensearch.py")),
        ("af/search/opensearch_manager.py", Path(__file__)),
        *client_files,
    )
    for label, path in source_files:
        total_bytes += _hash_file(
            artifact,
            label=label,
            path=path,
            remaining_bytes=_MAX_RUNTIME_BYTES - total_bytes,
        )

    dependency_records = [
        f"{platform.python_implementation()}:{platform.python_version()}",
        f"opensearch-py={client_version}",
    ]
    if len(requirements) > 512:
        raise ValueError("OpenSearch runtime identity is unavailable")
    for requirement in sorted(requirements):
        if (
            type(requirement) is not str
            or not requirement
            or len(requirement) > 2_048
            or _UNSAFE_TEXT.search(requirement) is not None
        ):
            raise ValueError("OpenSearch runtime identity is unavailable")
        matched = _REQUIREMENT_NAME.match(requirement)
        if matched is None:
            raise ValueError("OpenSearch runtime identity is unavailable")
        dependency_name = matched.group(0)
        try:
            resolved_version = importlib.metadata.version(dependency_name)
        except importlib.metadata.PackageNotFoundError:
            resolved_version = "<absent>"
        dependency_records.append(f"{requirement}\0{dependency_name}={resolved_version}")

    artifact_digest = artifact.hexdigest()
    dependency_digest = hashlib.sha256("\0".join(dependency_records).encode()).hexdigest()
    return client_version, artifact_digest, dependency_digest


def _url(value: object) -> tuple[str, bool]:
    if (
        type(value) is not str
        or not value
        or len(value) > 2_048
        or value != value.strip()
        or _UNSAFE_TEXT.search(value) is not None
    ):
        raise ValueError("OpenSearch connection URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("OpenSearch connection URL is invalid") from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"})
    ):
        raise ValueError("OpenSearch connection URL is invalid")
    return value, parsed.scheme == "https"


def _request_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= 60.0
    ):
        raise ValueError("OpenSearch request timeout is invalid")
    return float(value)


def _client(url: str, *, request_timeout: float) -> OpenSearchClient:
    url, verify_certs = _url(url)
    module = importlib.import_module("opensearchpy")
    constructor = vars(module).get("OpenSearch")
    if not callable(constructor):
        raise ValueError("OpenSearch client runtime is invalid")
    instance = constructor(
        [url],
        http_compress=True,
        max_retries=0,
        retry_on_timeout=False,
        timeout=request_timeout,
        verify_certs=verify_certs,
    )
    return cast(OpenSearchClient, instance)


def _factory(ref: ConnectionRef, config: Mapping[str, object]) -> SearchDriver:
    if ref.manager_id != "search" or ref.driver_name != "opensearch":
        raise ValueError("OpenSearch registration received an invalid connection identity")
    allowed = {"driver", "url", "index", "request_timeout"}
    if (
        set(config) - allowed
        or not {"driver", "url", "index"} <= set(config)
        or config.get("driver") != "opensearch"
    ):
        raise ValueError("OpenSearch connection configuration is invalid")
    url, _ = _url(config.get("url"))
    index = config.get("index")
    if type(index) is not str:
        raise ValueError("OpenSearch index name is invalid")
    request_timeout = _request_timeout(config.get("request_timeout", 5.0))
    client = _client(url, request_timeout=request_timeout)
    try:
        return OpenSearchDriver(
            client,
            index=index,
            request_timeout=request_timeout,
        )
    except BaseException:
        with suppress(Exception):
            client.close()
        raise


def _evidence_digest(
    binding: CapabilityBinding,
    capabilities: CapabilitySet,
    observed_at: datetime,
    *,
    server_version: str,
) -> str:
    payload = {
        "artifact": binding.artifact_digest,
        "capabilities": tuple(capabilities),
        "configuration": binding.configuration_digest,
        "dependencies": binding.dependency_graph_digest,
        "observed_at": observed_at.isoformat(),
        "policy": binding.policy_version,
        "profile": binding.deployment_profile,
        "server": server_version,
        "version": binding.driver_version,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def opensearch_registration(
    *,
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[SearchDriver]:
    """Return an inert OpenSearch declaration with verified runtime evidence."""

    client_version, artifact_digest, dependency_digest = _runtime_identity()
    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="opensearch",
        version=client_version,
        artifact_digest=artifact_digest,
        dependency_graph_digest=dependency_digest,
        capabilities=_CAPABILITIES,
    )

    def verify(
        ref: ConnectionRef,
        driver: SearchDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if (
            ref.manager_id != "search"
            or ref.driver_name != "opensearch"
            or type(driver) is not OpenSearchDriver
        ):
            raise ValueError("OpenSearch runtime capability verification failed")
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("OpenSearch evidence clock must be timezone-aware")
        observed_at = observed_at.astimezone(UTC)
        return CapabilityEvidence(
            binding=expected,
            capabilities=_CAPABILITIES,
            health=HealthStatus.HEALTHY,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(minutes=1),
            evidence_digest=_evidence_digest(
                expected,
                _CAPABILITIES,
                observed_at,
                server_version=driver.server_version,
            ),
        )

    return DriverRegistration(
        manager_id="search",
        driver_name="opensearch",
        factory=_factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
