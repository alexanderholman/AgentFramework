from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from af.framework.managers import CapabilityBinding, ConnectionRef, HealthStatus
from af.search import opensearch_manager as manager_module
from af.search.manager import SearchFeature
from af.search.opensearch import (
    OpenSearchDriver,
    OpenSearchOperationError,
    opensearch_index_body,
)
from af.search.opensearch_manager import opensearch_registration

_NOW = datetime(2026, 7, 24, 16, tzinfo=UTC)


class _Indices:
    def get_mapping(self, **kwargs: object) -> object:
        del kwargs
        return {"af-search": {"mappings": opensearch_index_body()["mappings"]}}


class _Client:
    def __init__(self) -> None:
        self.indices = _Indices()
        self.closed = False

    def info(self, **kwargs: object) -> object:
        del kwargs
        return {"version": {"distribution": "opensearch", "number": "3.7.0"}}

    def bulk(self, **kwargs: object) -> object:
        del kwargs
        raise AssertionError("manager test must not write")

    def search(self, **kwargs: object) -> object:
        del kwargs
        raise AssertionError("manager test must not search")

    def delete(self, **kwargs: object) -> object:
        del kwargs
        raise AssertionError("manager test must not delete")

    def close(self) -> None:
        self.closed = True


def _registration(monkeypatch: pytest.MonkeyPatch):
    client = _Client()
    monkeypatch.setattr(
        manager_module,
        "_runtime_identity",
        lambda: ("3.2.0", "a" * 64, "b" * 64),
    )
    monkeypatch.setattr(
        manager_module,
        "_client",
        lambda url, *, request_timeout: client,
    )
    return opensearch_registration(clock=lambda: _NOW), client


def test_opensearch_registration_is_inert_and_binds_real_service_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration, client = _registration(monkeypatch)
    ref = ConnectionRef("search", "primary", "opensearch")
    driver = registration.factory(
        ref,
        {
            "driver": "opensearch",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
            "request_timeout": 2.0,
        },
    )
    binding = CapabilityBinding(
        driver_version="3.2.0",
        artifact_digest="a" * 64,
        dependency_graph_digest="b" * 64,
        configuration_digest=hashlib.sha256(b"config").hexdigest(),
        deployment_profile="integration",
        policy_version="policy-v1",
    )

    evidence = registration.verifier(ref, driver, binding)

    assert isinstance(driver, OpenSearchDriver)
    assert driver.server_version == "3.7.0"
    assert evidence.binding == binding
    assert evidence.health is HealthStatus.HEALTHY
    assert evidence.observed_at == _NOW
    assert evidence.capabilities.supports(
        {
            SearchFeature.POLICY_PUSHDOWN.value,
            SearchFeature.FULL_TEXT.value,
            SearchFeature.INDEXING.value,
            SearchFeature.TOMBSTONES.value,
            SearchFeature.PERSISTENCE.value,
        }
    )
    assert not evidence.capabilities.supports(
        {
            SearchFeature.CANCELLATION.value,
            SearchFeature.HIGHLIGHTING.value,
        }
    )
    assert registration.manifest.capabilities == evidence.capabilities
    disposer = registration.disposer
    assert disposer is not None
    disposer(driver)
    assert client.closed


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"driver": "opensearch", "url": "http://127.0.0.1:9200"},
        {
            "driver": "embedded",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
        },
        {
            "driver": "opensearch",
            "url": "http://search.example:9200",
            "index": "af-search",
        },
        {
            "driver": "opensearch",
            "url": "http://127.0.0.1:9200/private",
            "index": "af-search",
        },
        {
            "driver": "opensearch",
            "url": "http://127.0.0.1:9200",
            "index": "AF-SEARCH",
        },
        {
            "driver": "opensearch",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
            "request_timeout": True,
        },
        {
            "driver": "opensearch",
            "url": " https://search.example:443",
            "index": "af-search",
        },
        {
            "driver": "opensearch",
            "url": "https://search.example:invalid",
            "index": "af-search",
        },
        {
            "driver": "opensearch",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
            "unknown": "private-secret",
        },
    ],
)
def test_opensearch_registration_rejects_unsafe_configuration(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, object],
) -> None:
    registration, _ = _registration(monkeypatch)
    ref = ConnectionRef("search", "primary", "opensearch")

    with pytest.raises(ValueError):
        registration.factory(ref, config)


def test_opensearch_registration_requires_aware_evidence_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    monkeypatch.setattr(
        manager_module,
        "_runtime_identity",
        lambda: ("3.2.0", "a" * 64, "b" * 64),
    )
    monkeypatch.setattr(
        manager_module,
        "_client",
        lambda url, *, request_timeout: client,
    )
    registration = opensearch_registration(clock=lambda: datetime(2026, 7, 24))
    ref = ConnectionRef("search", "primary", "opensearch")
    driver = registration.factory(
        ref,
        {
            "driver": "opensearch",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
        },
    )
    binding = CapabilityBinding(
        driver_version="3.2.0",
        artifact_digest="a" * 64,
        dependency_graph_digest="b" * 64,
        configuration_digest="c" * 64,
        deployment_profile="test",
        policy_version="policy-v1",
    )

    with pytest.raises(ValueError):
        registration.verifier(ref, driver, binding)

    driver.close()


def test_opensearch_runtime_identity_binds_exact_client_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    client_package = tmp_path / "opensearchpy"
    client_package.mkdir()
    client_module = client_package / "__init__.py"
    transport_module = client_package / "transport.py"
    client_module.write_bytes(b"verified client bytes")
    transport_module.write_bytes(b"verified transport bytes")
    versions = {"opensearch-py": "3.2.0", "urllib3": "2.6.3"}

    class Distribution:
        files = (
            PurePosixPath("../outside.py"),
            PurePosixPath("opensearchpy/__init__.py"),
            PurePosixPath("opensearchpy/transport.py"),
        )
        requires = ("urllib3>=2.0",)

        @staticmethod
        def locate_file(path: PurePosixPath) -> Path:
            return tmp_path / path

    monkeypatch.setattr(
        manager_module.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin=str(client_module)),
    )
    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "distribution",
        lambda name: Distribution(),
    )

    def installed_version(name: str) -> str:
        try:
            return versions[name]
        except KeyError:
            raise manager_module.importlib.metadata.PackageNotFoundError(name) from None

    monkeypatch.setattr(manager_module.importlib.metadata, "version", installed_version)

    version, artifact, dependencies = manager_module._runtime_identity()

    assert version == "3.2.0"
    assert len(artifact) == len(dependencies) == 64

    transport_module.write_bytes(b"modified transport bytes")
    _, modified_artifact, unchanged_dependencies = manager_module._runtime_identity()
    assert modified_artifact != artifact
    assert unchanged_dependencies == dependencies

    versions["urllib3"] = "2.7.0"
    _, _, modified_dependencies = manager_module._runtime_identity()
    assert modified_dependencies != dependencies

    versions.pop("urllib3")
    _, _, missing_dependency = manager_module._runtime_identity()
    assert missing_dependency != modified_dependencies

    monkeypatch.setattr(manager_module.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ValueError):
        manager_module._runtime_identity()


def test_opensearch_runtime_identity_rejects_shadowed_client_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installed_package = tmp_path / "installed" / "opensearchpy"
    installed_package.mkdir(parents=True)
    installed_module = installed_package / "__init__.py"
    installed_module.write_bytes(b"installed client bytes")
    shadow_package = tmp_path / "shadow" / "opensearchpy"
    shadow_package.mkdir(parents=True)
    shadow_module = shadow_package / "__init__.py"
    shadow_module.write_bytes(b"shadow client bytes")

    class Distribution:
        files = (PurePosixPath("opensearchpy/__init__.py"),)
        requires: tuple[str, ...] = ()

        @staticmethod
        def locate_file(path: PurePosixPath) -> Path:
            return tmp_path / "installed" / path

    monkeypatch.setattr(
        manager_module.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin=str(shadow_module)),
    )
    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "distribution",
        lambda name: Distribution(),
    )
    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "version",
        lambda name: "3.2.0",
    )

    with pytest.raises(ValueError):
        manager_module._runtime_identity()

    monkeypatch.setattr(
        manager_module.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin=str(installed_module)),
    )
    assert manager_module._runtime_identity()[0] == "3.2.0"


def test_opensearch_runtime_identity_enforces_file_count_before_hashing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client_package = tmp_path / "opensearchpy"
    client_package.mkdir()
    client_module = client_package / "__init__.py"
    client_module.write_bytes(b"verified client bytes")
    transport_module = client_package / "transport.py"
    transport_module.write_bytes(b"verified transport bytes")

    class Distribution:
        files = (
            PurePosixPath("opensearchpy/__init__.py"),
            PurePosixPath("opensearchpy/transport.py"),
        )
        requires: tuple[str, ...] = ()

        @staticmethod
        def locate_file(path: PurePosixPath) -> Path:
            return tmp_path / path

    monkeypatch.setattr(
        manager_module.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin=str(client_module)),
    )
    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "distribution",
        lambda name: Distribution(),
    )
    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "version",
        lambda name: "3.2.0",
    )
    monkeypatch.setattr(manager_module, "_MAX_RUNTIME_FILES", 1)

    with pytest.raises(ValueError):
        manager_module._runtime_identity()


def test_opensearch_runtime_file_hash_is_bounded_and_detects_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_bytes(b"verified runtime bytes")
    digest = hashlib.sha256()

    assert manager_module._hash_file(
        digest,
        label="opensearchpy/runtime.py",
        path=runtime_file,
        remaining_bytes=len(b"verified runtime bytes"),
    ) == len(b"verified runtime bytes")
    with pytest.raises(ValueError):
        manager_module._hash_file(
            hashlib.sha256(),
            label="opensearchpy/runtime.py",
            path=runtime_file,
            remaining_bytes=len(b"verified runtime bytes") - 1,
        )

    original_fstat = manager_module.os.fstat
    calls = 0

    def changing_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        observed = original_fstat(descriptor)
        if calls == 1:
            return observed
        changed = list(observed)
        changed[8] = observed.st_mtime + 1
        return manager_module.os.stat_result(changed)

    monkeypatch.setattr(manager_module.os, "fstat", changing_fstat)
    with pytest.raises(ValueError):
        manager_module._hash_file(
            hashlib.sha256(),
            label="opensearchpy/runtime.py",
            path=runtime_file,
            remaining_bytes=len(b"verified runtime bytes"),
        )


def test_opensearch_runtime_file_hash_rejects_symlink(tmp_path: Path) -> None:
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_bytes(b"verified runtime bytes")
    linked_file = tmp_path / "linked.py"
    linked_file.symlink_to(runtime_file)

    with pytest.raises(ValueError):
        manager_module._hash_file(
            hashlib.sha256(),
            label="opensearchpy/runtime.py",
            path=linked_file,
            remaining_bytes=1_024,
        )


def test_opensearch_runtime_identity_rejects_incomplete_distribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client_package = tmp_path / "opensearchpy"
    client_package.mkdir()
    client_module = client_package / "__init__.py"
    client_module.write_bytes(b"verified client bytes")

    class Distribution:
        files: tuple[PurePosixPath, ...] = ()
        requires: tuple[str, ...] = ()

        @staticmethod
        def locate_file(path: PurePosixPath) -> Path:
            return tmp_path / path

    monkeypatch.setattr(
        manager_module.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin=str(client_module)),
    )
    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "distribution",
        lambda name: Distribution(),
    )
    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "version",
        lambda name: "3.2.0",
    )

    with pytest.raises(ValueError):
        manager_module._runtime_identity()

    Distribution.files = (PurePosixPath("opensearchpy/__init__.py"),)
    Distribution.requires = (" invalid requirement",)
    with pytest.raises(ValueError):
        manager_module._runtime_identity()

    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "distribution",
        lambda name: (_ for _ in ()).throw(
            manager_module.importlib.metadata.PackageNotFoundError(name)
        ),
    )
    with pytest.raises(ValueError):
        manager_module._runtime_identity()


def test_opensearch_client_builder_preserves_tls_verification_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []
    client = object()

    def constructor(hosts: object, **kwargs: object) -> object:
        calls.append((hosts, kwargs))
        return client

    monkeypatch.setattr(
        manager_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(OpenSearch=constructor),
    )

    built = manager_module._client(
        "HTTPS://search.example:443",
        request_timeout=4.0,
    )

    assert built is client
    assert calls == [
        (
            ["HTTPS://search.example:443"],
            {
                "http_compress": True,
                "max_retries": 0,
                "retry_on_timeout": False,
                "timeout": 4.0,
                "verify_certs": True,
            },
        )
    ]

    monkeypatch.setattr(
        manager_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(OpenSearch=None),
    )
    with pytest.raises(ValueError):
        manager_module._client(
            "http://127.0.0.1:9200",
            request_timeout=4.0,
        )


def test_opensearch_factory_derives_tls_verification_from_parsed_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        manager_module,
        "_runtime_identity",
        lambda: ("3.2.0", "a" * 64, "b" * 64),
    )

    def constructor(hosts: object, **kwargs: object) -> _Client:
        calls.append((hosts, kwargs))
        return client

    monkeypatch.setattr(
        manager_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(OpenSearch=constructor),
    )
    registration = opensearch_registration(clock=lambda: _NOW)

    driver = registration.factory(
        ConnectionRef("search", "primary", "opensearch"),
        {
            "driver": "opensearch",
            "url": "HTTPS://search.example:443",
            "index": "af-search",
        },
    )

    assert calls == [
        (
            ["HTTPS://search.example:443"],
            {
                "http_compress": True,
                "max_retries": 0,
                "retry_on_timeout": False,
                "timeout": 5.0,
                "verify_certs": True,
            },
        )
    ]
    driver.close()


def test_opensearch_factory_closes_client_after_mapping_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration, client = _registration(monkeypatch)
    client.indices.get_mapping = lambda **kwargs: {}  # type: ignore[method-assign]
    ref = ConnectionRef("search", "primary", "opensearch")

    with pytest.raises(OpenSearchOperationError):
        registration.factory(
            ref,
            {
                "driver": "opensearch",
                "url": "https://search.example:443",
                "index": "af-search",
            },
        )

    assert client.closed


def test_opensearch_verifier_rejects_wrong_registration_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration, _ = _registration(monkeypatch)
    ref = ConnectionRef("search", "primary", "opensearch")
    driver = registration.factory(
        ref,
        {
            "driver": "opensearch",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
        },
    )
    binding = CapabilityBinding(
        driver_version="3.2.0",
        artifact_digest="a" * 64,
        dependency_graph_digest="b" * 64,
        configuration_digest="c" * 64,
        deployment_profile="test",
        policy_version="policy-v1",
    )

    with pytest.raises(ValueError):
        registration.verifier(
            ConnectionRef("search", "primary", "embedded"),
            driver,
            binding,
        )
    with pytest.raises(ValueError):
        registration.factory(
            ConnectionRef("database", "primary", "opensearch"),
            {
                "driver": "opensearch",
                "url": "http://127.0.0.1:9200",
                "index": "af-search",
            },
        )

    driver.close()
