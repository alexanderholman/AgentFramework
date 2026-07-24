"""Verified OpenSearch registration for SearchManager."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import platform
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
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


def _runtime_identity() -> tuple[str, str, str]:
    spec = importlib.util.find_spec("opensearchpy")
    if spec is None or spec.origin is None:
        raise ValueError("OpenSearch registration requires the opensearch-py runtime")
    try:
        client_version = importlib.metadata.version("opensearch-py")
        client_bytes = Path(spec.origin).read_bytes()
        driver_bytes = Path(__file__).with_name("opensearch.py").read_bytes()
    except (OSError, importlib.metadata.PackageNotFoundError) as error:
        raise ValueError("OpenSearch runtime identity is unavailable") from error
    artifact_digest = hashlib.sha256(driver_bytes + b"\0" + client_bytes).hexdigest()
    dependency_digest = hashlib.sha256(
        (
            f"{platform.python_implementation()}:{platform.python_version()}:"
            f"opensearch-py={client_version}"
        ).encode()
    ).hexdigest()
    return client_version, artifact_digest, dependency_digest


def _url(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 2_048
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
    return value


def _request_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= 60.0
    ):
        raise ValueError("OpenSearch request timeout is invalid")
    return float(value)


def _client(url: str, *, request_timeout: float) -> OpenSearchClient:
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
        verify_certs=url.startswith("https://"),
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
    url = _url(config.get("url"))
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
