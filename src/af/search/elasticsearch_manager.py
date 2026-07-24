"""Verified Elasticsearch registration for SearchManager."""

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
from af.search.elasticsearch import ElasticsearchClient, ElasticsearchDriver
from af.search.manager import SearchDriver, SearchFeature

__all__ = ("elasticsearch_registration",)

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
_RUNTIME_DISTRIBUTIONS = (
    ("elasticsearch", "elasticsearch"),
    ("elastic-transport", "elastic_transport"),
)


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
        raise ValueError("Elasticsearch runtime identity is unavailable")
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
                raise ValueError("Elasticsearch runtime identity is unavailable")
            digest.update(before.st_size.to_bytes(8, "big"))
            pending = before.st_size
            observed = 0
            while pending:
                chunk = handle.read(min(_RUNTIME_READ_BYTES, pending))
                if not chunk:
                    raise ValueError("Elasticsearch runtime identity is unavailable")
                digest.update(chunk)
                observed += len(chunk)
                pending -= len(chunk)
            if handle.read(1):
                raise ValueError("Elasticsearch runtime identity is unavailable")
            after = os.fstat(handle.fileno())
    except OSError as error:
        raise ValueError("Elasticsearch runtime identity is unavailable") from error
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
        raise ValueError("Elasticsearch runtime identity is unavailable")
    return observed


def _runtime_identity() -> tuple[str, str, str]:
    runtime_files: list[tuple[str, Path]] = []
    runtime_labels: set[str] = set()
    requirements: list[tuple[str, str]] = []
    versions: dict[str, str] = {}
    try:
        for distribution_name, import_root in _RUNTIME_DISTRIBUTIONS:
            spec = importlib.util.find_spec(import_root)
            if spec is None or spec.origin is None:
                raise ValueError("Elasticsearch registration requires the official client runtime")
            version = importlib.metadata.version(distribution_name)
            distribution = importlib.metadata.distribution(distribution_name)
            versions[distribution_name] = version
            distribution_files = distribution.files
            distribution_requirements = tuple(distribution.requires or ())
            if len(distribution_requirements) > 512:
                raise ValueError("Elasticsearch runtime identity is unavailable")
            requirements.extend(
                (distribution_name, requirement) for requirement in distribution_requirements
            )

            package_files: list[tuple[str, Path]] = []
            for item in distribution_files or ():
                relative = PurePosixPath(str(item))
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or not relative.parts
                    or relative.parts[0] != import_root
                    or "__pycache__" in relative.parts
                    or relative.suffix == ".pyc"
                ):
                    continue
                label = relative.as_posix()
                if label in runtime_labels or len(runtime_files) >= _MAX_RUNTIME_FILES:
                    raise ValueError("Elasticsearch runtime identity is unavailable")
                runtime_labels.add(label)
                path = Path(str(distribution.locate_file(item)))
                package_files.append((label, path))
                runtime_files.append((label, path))
            if not package_files:
                raise ValueError("Elasticsearch runtime identity is unavailable")
            entrypoint = next(
                (path for label, path in package_files if label == f"{import_root}/__init__.py"),
                None,
            )
            import_origin = Path(spec.origin)
            if (
                entrypoint is None
                or not import_origin.is_absolute()
                or import_origin.is_symlink()
                or entrypoint.is_symlink()
                or not import_origin.samefile(entrypoint)
            ):
                raise ValueError("Elasticsearch runtime identity is unavailable")
    except (OSError, importlib.metadata.PackageNotFoundError) as error:
        raise ValueError("Elasticsearch runtime identity is unavailable") from error

    runtime_files.sort(key=lambda item: item[0])
    client_version = versions["elasticsearch"]

    artifact = hashlib.sha256()
    total_bytes = 0
    source_files = (
        ("af/search/elasticsearch.py", Path(__file__).with_name("elasticsearch.py")),
        ("af/search/elasticsearch_manager.py", Path(__file__)),
        *runtime_files,
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
        *(f"{name}={versions[name]}" for name, _ in _RUNTIME_DISTRIBUTIONS),
    ]
    if len(requirements) > 1_024:
        raise ValueError("Elasticsearch runtime identity is unavailable")
    for owner, requirement in sorted(requirements):
        if (
            type(requirement) is not str
            or not requirement
            or len(requirement) > 2_048
            or _UNSAFE_TEXT.search(requirement) is not None
        ):
            raise ValueError("Elasticsearch runtime identity is unavailable")
        matched = _REQUIREMENT_NAME.match(requirement)
        if matched is None:
            raise ValueError("Elasticsearch runtime identity is unavailable")
        dependency_name = matched.group(0)
        try:
            resolved_version = importlib.metadata.version(dependency_name)
        except importlib.metadata.PackageNotFoundError:
            resolved_version = "<absent>"
        dependency_records.append(f"{owner}\0{requirement}\0{dependency_name}={resolved_version}")

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
        raise ValueError("Elasticsearch connection URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Elasticsearch connection URL is invalid") from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"})
    ):
        raise ValueError("Elasticsearch connection URL is invalid")
    return value, parsed.scheme == "https"


def _request_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= 60.0
    ):
        raise ValueError("Elasticsearch request timeout is invalid")
    return float(value)


def _client(url: str, *, request_timeout: float) -> ElasticsearchClient:
    url, verify_certs = _url(url)
    module = importlib.import_module("elasticsearch")
    constructor = vars(module).get("Elasticsearch")
    if not callable(constructor):
        raise ValueError("Elasticsearch client runtime is invalid")
    instance = constructor(
        url,
        http_compress=True,
        max_retries=0,
        request_timeout=request_timeout,
        retry_on_status=(),
        retry_on_timeout=False,
        server_mode="stack",
        verify_certs=verify_certs,
    )
    return cast(ElasticsearchClient, instance)


def _factory(ref: ConnectionRef, config: Mapping[str, object]) -> SearchDriver:
    if ref.manager_id != "search" or ref.driver_name != "elasticsearch":
        raise ValueError("Elasticsearch registration received an invalid connection identity")
    allowed = {"driver", "url", "index", "request_timeout"}
    if (
        set(config) - allowed
        or not {"driver", "url", "index"} <= set(config)
        or config.get("driver") != "elasticsearch"
    ):
        raise ValueError("Elasticsearch connection configuration is invalid")
    url, _ = _url(config.get("url"))
    index = config.get("index")
    if type(index) is not str:
        raise ValueError("Elasticsearch index name is invalid")
    request_timeout = _request_timeout(config.get("request_timeout", 5.0))
    client = _client(url, request_timeout=request_timeout)
    try:
        return ElasticsearchDriver(
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


def elasticsearch_registration(
    *,
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[SearchDriver]:
    """Return an inert Elasticsearch declaration with verified runtime evidence."""

    client_version, artifact_digest, dependency_digest = _runtime_identity()
    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="elasticsearch",
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
            or ref.driver_name != "elasticsearch"
            or type(driver) is not ElasticsearchDriver
        ):
            raise ValueError("Elasticsearch runtime capability verification failed")
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("Elasticsearch evidence clock must be timezone-aware")
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
        driver_name="elasticsearch",
        factory=_factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
