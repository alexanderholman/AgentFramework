from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from af.framework.managers import CapabilityBinding, ConnectionRef, HealthStatus
from af.search import elasticsearch_manager as manager_module
from af.search.elasticsearch import (
    ElasticsearchDriver,
    ElasticsearchOperationError,
    elasticsearch_index_body,
)
from af.search.elasticsearch_manager import elasticsearch_registration
from af.search.manager import SearchFeature

_NOW = datetime(2026, 7, 24, 16, tzinfo=UTC)


class _Indices:
    def get_mapping(self, **kwargs: object) -> object:
        del kwargs
        return {"af-search": {"mappings": elasticsearch_index_body()["mappings"]}}


class _Client:
    def __init__(self) -> None:
        self.indices = _Indices()
        self.closed = False

    def options(self, **kwargs: object) -> _Client:
        del kwargs
        return self

    def info(self, **kwargs: object) -> object:
        del kwargs
        return {
            "version": {
                "number": "9.4.4",
                "build_flavor": "default",
                "build_snapshot": False,
            },
            "tagline": "You Know, for Search",
        }

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
        lambda: ("9.4.1", "a" * 64, "b" * 64),
    )
    monkeypatch.setattr(
        manager_module,
        "_client",
        lambda url, *, request_timeout: client,
    )
    return elasticsearch_registration(clock=lambda: _NOW), client


def test_elasticsearch_registration_is_inert_and_binds_real_service_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration, client = _registration(monkeypatch)
    ref = ConnectionRef("search", "primary", "elasticsearch")
    driver = registration.factory(
        ref,
        {
            "driver": "elasticsearch",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
            "request_timeout": 2.0,
        },
    )
    binding = CapabilityBinding(
        driver_version="9.4.1",
        artifact_digest="a" * 64,
        dependency_graph_digest="b" * 64,
        configuration_digest=hashlib.sha256(b"config").hexdigest(),
        deployment_profile="integration",
        policy_version="policy-v1",
    )

    evidence = registration.verifier(ref, driver, binding)

    assert isinstance(driver, ElasticsearchDriver)
    assert driver.server_version == "9.4.4"
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
        {"driver": "elasticsearch", "url": "http://127.0.0.1:9200"},
        {
            "driver": "embedded",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
        },
        {
            "driver": "elasticsearch",
            "url": "http://search.example:9200",
            "index": "af-search",
        },
        {
            "driver": "elasticsearch",
            "url": "http://127.0.0.1:9200/private",
            "index": "af-search",
        },
        {
            "driver": "elasticsearch",
            "url": "http://127.0.0.1:9200",
            "index": "AF-SEARCH",
        },
        {
            "driver": "elasticsearch",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
            "request_timeout": True,
        },
        {
            "driver": "elasticsearch",
            "url": " https://search.example:443",
            "index": "af-search",
        },
        {
            "driver": "elasticsearch",
            "url": "https://search.example:invalid",
            "index": "af-search",
        },
        {
            "driver": "elasticsearch",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
            "unknown": "private-secret",
        },
    ],
)
def test_elasticsearch_registration_rejects_unsafe_configuration(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, object],
) -> None:
    registration, _ = _registration(monkeypatch)
    ref = ConnectionRef("search", "primary", "elasticsearch")

    with pytest.raises(ValueError):
        registration.factory(ref, config)


def test_elasticsearch_registration_requires_aware_evidence_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    monkeypatch.setattr(
        manager_module,
        "_runtime_identity",
        lambda: ("9.4.1", "a" * 64, "b" * 64),
    )
    monkeypatch.setattr(
        manager_module,
        "_client",
        lambda url, *, request_timeout: client,
    )
    registration = elasticsearch_registration(clock=lambda: datetime(2026, 7, 24))
    ref = ConnectionRef("search", "primary", "elasticsearch")
    driver = registration.factory(
        ref,
        {
            "driver": "elasticsearch",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
        },
    )
    binding = CapabilityBinding(
        driver_version="9.4.1",
        artifact_digest="a" * 64,
        dependency_graph_digest="b" * 64,
        configuration_digest="c" * 64,
        deployment_profile="test",
        policy_version="policy-v1",
    )

    with pytest.raises(ValueError):
        registration.verifier(ref, driver, binding)

    driver.close()


def test_elasticsearch_runtime_identity_binds_exact_client_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client_package = tmp_path / "elasticsearch"
    transport_package = tmp_path / "elastic_transport"
    client_package.mkdir()
    transport_package.mkdir()
    client_module = client_package / "__init__.py"
    client_api = client_package / "client.py"
    transport_module = transport_package / "__init__.py"
    transport_api = transport_package / "_transport.py"
    client_module.write_bytes(b"verified client entrypoint")
    client_api.write_bytes(b"verified client bytes")
    transport_module.write_bytes(b"verified transport entrypoint")
    transport_api.write_bytes(b"verified transport bytes")
    versions = {
        "elasticsearch": "9.4.1",
        "elastic-transport": "9.4.1",
        "urllib3": "2.6.3",
    }

    class ClientDistribution:
        files = (
            PurePosixPath("../outside.py"),
            PurePosixPath("elasticsearch/__init__.py"),
            PurePosixPath("elasticsearch/client.py"),
        )
        requires = ("elastic-transport>=9.4.1,<10",)

        @staticmethod
        def locate_file(path: PurePosixPath) -> Path:
            return tmp_path / path

    class TransportDistribution:
        files = (
            PurePosixPath("elastic_transport/__init__.py"),
            PurePosixPath("elastic_transport/_transport.py"),
        )
        requires = ("urllib3>=2,<3",)

        @staticmethod
        def locate_file(path: PurePosixPath) -> Path:
            return tmp_path / path

    distributions = {
        "elasticsearch": ClientDistribution(),
        "elastic-transport": TransportDistribution(),
    }
    origins = {
        "elasticsearch": client_module,
        "elastic_transport": transport_module,
    }
    monkeypatch.setattr(
        manager_module.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin=str(origins[name])) if name in origins else None,
    )
    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "distribution",
        lambda name: distributions[name],
    )

    def installed_version(name: str) -> str:
        try:
            return versions[name]
        except KeyError:
            raise manager_module.importlib.metadata.PackageNotFoundError(name) from None

    monkeypatch.setattr(manager_module.importlib.metadata, "version", installed_version)

    version, artifact, dependencies = manager_module._runtime_identity()

    assert version == "9.4.1"
    assert len(artifact) == len(dependencies) == 64

    transport_api.write_bytes(b"modified transport bytes")
    _, modified_artifact, unchanged_dependencies = manager_module._runtime_identity()
    assert modified_artifact != artifact
    assert unchanged_dependencies == dependencies

    versions["urllib3"] = "2.7.0"
    _, _, modified_dependencies = manager_module._runtime_identity()
    assert modified_dependencies != dependencies

    versions.pop("urllib3")
    _, _, missing_dependency = manager_module._runtime_identity()
    assert missing_dependency != modified_dependencies

    origins.pop("elastic_transport")
    with pytest.raises(ValueError):
        manager_module._runtime_identity()


def test_elasticsearch_runtime_identity_rejects_shadowed_client_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installed_package = tmp_path / "installed" / "elasticsearch"
    installed_package.mkdir(parents=True)
    installed_module = installed_package / "__init__.py"
    installed_module.write_bytes(b"installed client bytes")
    installed_transport = tmp_path / "installed" / "elastic_transport"
    installed_transport.mkdir()
    transport_module = installed_transport / "__init__.py"
    transport_module.write_bytes(b"installed transport bytes")
    shadow_package = tmp_path / "shadow" / "elasticsearch"
    shadow_package.mkdir(parents=True)
    shadow_module = shadow_package / "__init__.py"
    shadow_module.write_bytes(b"shadow client bytes")

    class ClientDistribution:
        files = (PurePosixPath("elasticsearch/__init__.py"),)
        requires: tuple[str, ...] = ()

        @staticmethod
        def locate_file(path: PurePosixPath) -> Path:
            return tmp_path / "installed" / path

    class TransportDistribution:
        files = (PurePosixPath("elastic_transport/__init__.py"),)
        requires: tuple[str, ...] = ()

        @staticmethod
        def locate_file(path: PurePosixPath) -> Path:
            return tmp_path / "installed" / path

    origins = {
        "elasticsearch": shadow_module,
        "elastic_transport": transport_module,
    }
    monkeypatch.setattr(
        manager_module.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin=str(origins[name])),
    )
    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "distribution",
        lambda name: ClientDistribution() if name == "elasticsearch" else TransportDistribution(),
    )
    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "version",
        lambda name: "9.4.1",
    )

    with pytest.raises(ValueError):
        manager_module._runtime_identity()

    origins["elasticsearch"] = installed_module
    assert manager_module._runtime_identity()[0] == "9.4.1"


def test_elasticsearch_runtime_identity_enforces_file_count_before_hashing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    packages: dict[str, Path] = {}
    for root in ("elasticsearch", "elastic_transport"):
        package = tmp_path / root
        package.mkdir()
        module = package / "__init__.py"
        module.write_bytes(b"verified entrypoint")
        packages[root] = module

    class Distribution:
        requires: tuple[str, ...] = ()

        def __init__(self, root: str) -> None:
            self.files = (PurePosixPath(f"{root}/__init__.py"),)

        @staticmethod
        def locate_file(path: PurePosixPath) -> Path:
            return tmp_path / path

    monkeypatch.setattr(
        manager_module.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin=str(packages[name])),
    )
    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "distribution",
        lambda name: Distribution(
            "elasticsearch" if name == "elasticsearch" else "elastic_transport"
        ),
    )
    monkeypatch.setattr(manager_module.importlib.metadata, "version", lambda name: "9.4.1")
    monkeypatch.setattr(manager_module, "_MAX_RUNTIME_FILES", 1)

    with pytest.raises(ValueError):
        manager_module._runtime_identity()


def test_elasticsearch_runtime_file_hash_is_bounded_and_detects_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_bytes(b"verified runtime bytes")
    digest = hashlib.sha256()

    assert manager_module._hash_file(
        digest,
        label="elasticsearch/runtime.py",
        path=runtime_file,
        remaining_bytes=len(b"verified runtime bytes"),
    ) == len(b"verified runtime bytes")
    with pytest.raises(ValueError):
        manager_module._hash_file(
            hashlib.sha256(),
            label="elasticsearch/runtime.py",
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
            label="elasticsearch/runtime.py",
            path=runtime_file,
            remaining_bytes=len(b"verified runtime bytes"),
        )


def test_elasticsearch_runtime_file_hash_rejects_symlink(tmp_path: Path) -> None:
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_bytes(b"verified runtime bytes")
    linked_file = tmp_path / "linked.py"
    linked_file.symlink_to(runtime_file)

    with pytest.raises(ValueError):
        manager_module._hash_file(
            hashlib.sha256(),
            label="elasticsearch/runtime.py",
            path=linked_file,
            remaining_bytes=1_024,
        )


def test_elasticsearch_runtime_identity_rejects_incomplete_distribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client_package = tmp_path / "elasticsearch"
    transport_package = tmp_path / "elastic_transport"
    client_package.mkdir()
    transport_package.mkdir()
    client_module = client_package / "__init__.py"
    transport_module = transport_package / "__init__.py"
    client_module.write_bytes(b"verified client bytes")
    transport_module.write_bytes(b"verified transport bytes")

    class ClientDistribution:
        files: tuple[PurePosixPath, ...] = ()
        requires: tuple[str, ...] = ()

        @staticmethod
        def locate_file(path: PurePosixPath) -> Path:
            return tmp_path / path

    class TransportDistribution:
        files = (PurePosixPath("elastic_transport/__init__.py"),)
        requires: tuple[str, ...] = ()

        @staticmethod
        def locate_file(path: PurePosixPath) -> Path:
            return tmp_path / path

    monkeypatch.setattr(
        manager_module.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(
            origin=str(client_module if name == "elasticsearch" else transport_module)
        ),
    )
    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "distribution",
        lambda name: ClientDistribution() if name == "elasticsearch" else TransportDistribution(),
    )
    monkeypatch.setattr(
        manager_module.importlib.metadata,
        "version",
        lambda name: "9.4.1",
    )

    with pytest.raises(ValueError):
        manager_module._runtime_identity()

    ClientDistribution.files = (PurePosixPath("elasticsearch/__init__.py"),)
    ClientDistribution.requires = (" invalid requirement",)
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


def test_elasticsearch_client_builder_preserves_tls_verification_and_bounds(
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
        lambda name: SimpleNamespace(Elasticsearch=constructor),
    )

    built = manager_module._client(
        "HTTPS://search.example:443",
        request_timeout=4.0,
    )

    assert built is client
    assert calls == [
        (
            "HTTPS://search.example:443",
            {
                "http_compress": True,
                "max_retries": 0,
                "request_timeout": 4.0,
                "retry_on_status": (),
                "retry_on_timeout": False,
                "server_mode": "stack",
                "verify_certs": True,
            },
        )
    ]

    monkeypatch.setattr(
        manager_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(Elasticsearch=None),
    )
    with pytest.raises(ValueError):
        manager_module._client(
            "http://127.0.0.1:9200",
            request_timeout=4.0,
        )


def test_elasticsearch_factory_derives_tls_verification_from_parsed_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        manager_module,
        "_runtime_identity",
        lambda: ("9.4.1", "a" * 64, "b" * 64),
    )

    def constructor(hosts: object, **kwargs: object) -> _Client:
        calls.append((hosts, kwargs))
        return client

    monkeypatch.setattr(
        manager_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(Elasticsearch=constructor),
    )
    registration = elasticsearch_registration(clock=lambda: _NOW)

    driver = registration.factory(
        ConnectionRef("search", "primary", "elasticsearch"),
        {
            "driver": "elasticsearch",
            "url": "HTTPS://search.example:443",
            "index": "af-search",
        },
    )

    assert calls == [
        (
            "HTTPS://search.example:443",
            {
                "http_compress": True,
                "max_retries": 0,
                "request_timeout": 5.0,
                "retry_on_status": (),
                "retry_on_timeout": False,
                "server_mode": "stack",
                "verify_certs": True,
            },
        )
    ]
    driver.close()


def test_elasticsearch_factory_closes_client_after_mapping_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration, client = _registration(monkeypatch)
    client.indices.get_mapping = lambda **kwargs: {}  # type: ignore[method-assign]
    ref = ConnectionRef("search", "primary", "elasticsearch")

    with pytest.raises(ElasticsearchOperationError):
        registration.factory(
            ref,
            {
                "driver": "elasticsearch",
                "url": "https://search.example:443",
                "index": "af-search",
            },
        )

    assert client.closed


def test_elasticsearch_verifier_rejects_wrong_registration_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration, _ = _registration(monkeypatch)
    ref = ConnectionRef("search", "primary", "elasticsearch")
    driver = registration.factory(
        ref,
        {
            "driver": "elasticsearch",
            "url": "http://127.0.0.1:9200",
            "index": "af-search",
        },
    )
    binding = CapabilityBinding(
        driver_version="9.4.1",
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
            ConnectionRef("database", "primary", "elasticsearch"),
            {
                "driver": "elasticsearch",
                "url": "http://127.0.0.1:9200",
                "index": "af-search",
            },
        )

    driver.close()
