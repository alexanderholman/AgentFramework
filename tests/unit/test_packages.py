from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from af.bootstrap import create_application
from af.config import Settings
from af.framework import packages as package_framework
from af.framework.container import Container, ProviderRegistrar, Scope, ServiceKey, ServiceProvider
from af.framework.packages import (
    DiscoveredPackage,
    InvalidPackageManifest,
    ManifestProviderAdapter,
    PackageApproval,
    PackageAuthorizationError,
    PackageCompatibilityError,
    PackageDependency,
    PackageDependencyError,
    PackageDiscoveryPolicy,
    PackageImportError,
    PackageManifest,
    PackagePlanner,
    PackageProviderDeclaration,
    PackageResourceDeclaration,
    PackageResources,
)
from af.infrastructure import packages as package_infrastructure
from af.infrastructure.packages import InstalledPackageDiscoverer, PackageLoader


def _install_distribution(
    site: Path,
    *,
    distribution_id: str = "af-fixture",
    version: str = "1.0.0",
    module_name: str = "af_fixture",
    provider_id: str = "fixture.provider",
    package_dependencies: tuple[tuple[str, str], ...] = (),
    provider_dependencies: tuple[str, ...] = (),
    requires_af: str = ">=0.1.0a0,<0.2.0",
    requires_python: str = ">=3.12.0",
) -> tuple[Path, Path]:
    package = site / module_name
    distribution = site / f"{module_name}-{version}.dist-info"
    package.mkdir(parents=True)
    distribution.mkdir()
    sentinel = site / f"{module_name}.imported"
    resource = package / "message.txt"
    resource.write_text(f"hello from {distribution_id}", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "provider.py").write_text(
        "\n".join(
            (
                "import os",
                "from pathlib import Path",
                "from af.framework.container import Scope, ServiceProvider",
                "from af.framework.packages import PackageResources",
                f'Path(os.environ["AF_PACKAGE_SENTINEL_{module_name.upper()}"]).write_text('
                '"imported", encoding="utf-8")',
                'STATE = {"booted": False, "shutdown": False}',
                "class FixtureProvider(ServiceProvider):",
                "    def register(self, container):",
                '        container.bind("fixture.state", lambda _: STATE, '
                "scope=Scope.APPLICATION, owned=False)",
                '        container.bind("fixture.message", lambda current: '
                f'current.resolve_token(PackageResources, qualifier="{distribution_id}")'
                '.read_text("fixture.message"), scope=Scope.APPLICATION, owned=False)',
                "    def boot(self, container, resources):",
                '        del container, resources; STATE["booted"] = True',
                "    def shutdown(self, container):",
                '        del container; STATE["shutdown"] = True',
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (distribution / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {distribution_id}\nVersion: {version}\n",
        encoding="utf-8",
    )
    manifest = {
        "schema": 1,
        "id": distribution_id,
        "version": version,
        "requires_af": requires_af,
        "requires_python": requires_python,
        "dependencies": [
            {"id": package_id, "version": constraint}
            for package_id, constraint in package_dependencies
        ],
        "providers": [
            {
                "id": provider_id,
                "object": f"{module_name}.provider:FixtureProvider",
                "dependencies": list(provider_dependencies),
                "deferred": False,
            }
        ],
        "resources": [
            {
                "id": "fixture.message",
                "path": f"{module_name}/message.txt",
                "sha256": hashlib.sha256(resource.read_bytes()).hexdigest(),
            }
        ],
    }
    (distribution / "af-package.json").write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )
    _write_record(site, distribution)
    return sentinel, distribution


def _write_record(site: Path, distribution: Path) -> None:
    record = distribution / "RECORD"
    paths = sorted(path for path in site.rglob("*") if path.is_file() and path != record)
    rows: list[str] = []
    for path in paths:
        data = path.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        rows.append(f"{path.relative_to(site).as_posix()},sha256={digest},{len(data)}")
    rows.append(f"{record.relative_to(site).as_posix()},,")
    record.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _discover(site: Path):
    return InstalledPackageDiscoverer((site,)).discover(PackageDiscoveryPolicy(enabled=True))


def _plan(packages):
    return PackagePlanner().plan(
        packages,
        af_version="0.1.0a0",
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )


def test_discovery_is_inert_and_global_or_per_package_opt_out_is_honoured(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "not-created"
    discoverer = InstalledPackageDiscoverer((missing,))
    assert discoverer.discover(PackageDiscoveryPolicy(enabled=False)) == ()

    site = tmp_path / "site"
    sentinel, distribution = _install_distribution(site)
    manifest = distribution / "af-package.json"
    manifest.write_text("{ definitely not JSON", encoding="utf-8")
    (distribution / "METADATA").unlink()

    disabled = PackageDiscoveryPolicy(
        enabled=True,
        disabled_packages=frozenset({"af-fixture"}),
    )
    assert InstalledPackageDiscoverer((site,)).discover(disabled) == ()
    assert not sentinel.exists()
    assert "af_fixture.provider" not in sys.modules


def test_manifest_json_is_strict_and_discovery_never_imports_provider(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    sentinel, distribution = _install_distribution(site)
    packages = _discover(site)

    assert len(packages) == 1
    assert packages[0].manifest.package_id == "af-fixture"
    assert not sentinel.exists()
    assert "af_fixture.provider" not in sys.modules

    (distribution / "af-package.json").write_text(
        '{"schema":1,"schema":1}',
        encoding="utf-8",
    )
    with pytest.raises(InvalidPackageManifest, match="duplicate"):
        _discover(site)


def test_planner_rejects_incompatible_hosts_and_package_cycles_without_imports(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    first_sentinel, _ = _install_distribution(
        site,
        distribution_id="first-package",
        module_name="first_package",
        provider_id="first.provider",
        package_dependencies=(("second-package", ">=1.0.0"),),
    )
    second_sentinel, _ = _install_distribution(
        site,
        distribution_id="second-package",
        module_name="second_package",
        provider_id="second.provider",
        package_dependencies=(("first-package", ">=1.0.0"),),
    )
    packages = _discover(site)

    with pytest.raises(PackageCompatibilityError, match="AF"):
        PackagePlanner().plan(
            packages,
            af_version="1.0.0",
            python_version="3.12.0",
        )
    with pytest.raises(PackageDependencyError, match="graph"):
        _plan(packages)

    assert not first_sentinel.exists()
    assert not second_sentinel.exists()
    assert "first_package.provider" not in sys.modules
    assert "second_package.provider" not in sys.modules


def test_planner_rejects_provider_cycles_before_import(tmp_path: Path) -> None:
    site = tmp_path / "site"
    first_sentinel, _ = _install_distribution(
        site,
        distribution_id="first-provider-package",
        module_name="first_provider_package",
        provider_id="first.provider",
        provider_dependencies=("second.provider",),
    )
    second_sentinel, _ = _install_distribution(
        site,
        distribution_id="second-provider-package",
        module_name="second_provider_package",
        provider_id="second.provider",
        provider_dependencies=("first.provider",),
    )

    with pytest.raises(PackageDependencyError, match="provider dependency graph"):
        _plan(_discover(site))
    assert not first_sentinel.exists()
    assert not second_sentinel.exists()


def test_all_approvals_and_current_digests_are_checked_before_any_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = tmp_path / "site"
    first_sentinel, _ = _install_distribution(
        site,
        distribution_id="first-approved-package",
        module_name="first_approved_package",
        provider_id="first.approved.provider",
    )
    second_sentinel, _ = _install_distribution(
        site,
        distribution_id="second-unapproved-package",
        module_name="second_unapproved_package",
        provider_id="second.unapproved.provider",
    )
    plan = _plan(_discover(site))
    first = next(
        package
        for package in plan.packages
        if package.manifest.package_id == "first-approved-package"
    )
    approval = PackageApproval(
        package_id=first.manifest.package_id,
        version=first.manifest.version,
        manifest_digest=first.manifest_digest,
        artifact_digest=first.artifact_digest,
    )
    monkeypatch.syspath_prepend(str(site))

    with pytest.raises(PackageAuthorizationError, match="approved"):
        PackageLoader().load(plan, (approval,))
    assert not first_sentinel.exists()
    assert not second_sentinel.exists()
    assert "first_approved_package.provider" not in sys.modules


def test_artifact_mutation_invalidates_digest_bound_approval_before_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = tmp_path / "site"
    sentinel, _ = _install_distribution(site)
    plan = _plan(_discover(site))
    package = plan.packages[0]
    approval = PackageApproval(
        package_id=package.manifest.package_id,
        version=package.manifest.version,
        manifest_digest=package.manifest_digest,
        artifact_digest=package.artifact_digest,
    )
    (site / "af_fixture" / "provider.py").write_text(
        "raise RuntimeError('changed')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(site))

    with pytest.raises(PackageAuthorizationError, match="changed"):
        PackageLoader().load(plan, (approval,))
    assert not sentinel.exists()
    assert "af_fixture.provider" not in sys.modules


def test_authorized_external_fixture_imports_then_starts_in_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = tmp_path / "external-site"
    sentinel, _ = _install_distribution(site)
    sentinel_name = "AF_PACKAGE_SENTINEL_AF_FIXTURE"
    monkeypatch.setenv(sentinel_name, str(sentinel))
    sys.modules.pop("af_fixture.provider", None)
    sys.modules.pop("af_fixture", None)

    packages = _discover(site)
    plan = _plan(packages)
    assert not sentinel.exists()
    assert "af_fixture.provider" not in sys.modules

    package = plan.packages[0]
    approval = PackageApproval(
        package_id=package.manifest.package_id,
        version=package.manifest.version,
        manifest_digest=package.manifest_digest,
        artifact_digest=package.artifact_digest,
    )
    monkeypatch.syspath_prepend(str(site))
    providers = PackageLoader().load(plan, (approval,))
    repeated_providers = PackageLoader().load(plan, (approval,))

    assert sentinel.read_text(encoding="utf-8") == "imported"
    assert len(repeated_providers) == len(providers)
    fixture_provider = next(
        provider for provider in providers if provider.provider_id == "fixture.provider"
    )
    assert len(fixture_provider.dependencies) == 1
    assert fixture_provider.dependencies[0].startswith("af.package-resources.")
    assert fixture_provider.deferred is False
    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        providers=providers,
    )
    state = application.container.resolve_token("fixture.state")
    assert state == {"booted": True, "shutdown": False}
    assert application.container.resolve_token("fixture.message") == "hello from af-fixture"
    application.shutdown()
    assert state == {"booted": True, "shutdown": True}

    sys.modules.pop("af_fixture.provider", None)
    sys.modules.pop("af_fixture", None)
    os.environ.pop(sentinel_name, None)


def test_shadowed_provider_route_is_rejected_before_any_module_executes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = tmp_path / "approved-site"
    approved_sentinel, _ = _install_distribution(site)
    package = _discover(site)[0]
    plan = _plan((package,))
    approval = PackageApproval(
        package_id=package.manifest.package_id,
        version=package.manifest.version,
        manifest_digest=package.manifest_digest,
        artifact_digest=package.artifact_digest,
    )
    shadow = tmp_path / "shadow-site"
    shadow_package = shadow / "af_fixture"
    shadow_package.mkdir(parents=True)
    shadow_sentinel = tmp_path / "shadow.imported"
    (shadow_package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(shadow_sentinel)!r}).write_text('bad')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.syspath_prepend(str(shadow))
    sys.modules.pop("af_fixture.provider", None)
    sys.modules.pop("af_fixture", None)

    with pytest.raises(PackageAuthorizationError, match="approved artifact"):
        PackageLoader().load(plan, (approval,))

    assert not shadow_sentinel.exists()
    assert not approved_sentinel.exists()


def test_unowned_nested_module_shadow_is_rejected_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = tmp_path / "approved-site"
    approved_sentinel, _ = _install_distribution(site)
    package = _discover(site)[0]
    plan = _plan((package,))
    approval = PackageApproval(
        package_id=package.manifest.package_id,
        version=package.manifest.version,
        manifest_digest=package.manifest_digest,
        artifact_digest=package.artifact_digest,
    )
    shadow_sentinel = tmp_path / "nested-shadow.imported"
    nested_shadow = site / "af_fixture" / "provider"
    nested_shadow.mkdir()
    (nested_shadow / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(shadow_sentinel)!r}).write_text('bad')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(site))
    sys.modules.pop("af_fixture.provider", None)
    sys.modules.pop("af_fixture", None)

    with pytest.raises(PackageAuthorizationError, match="approved artifact"):
        PackageLoader().load(plan, (approval,))

    assert not shadow_sentinel.exists()
    assert not approved_sentinel.exists()


def _valid_manifest(**changes: object) -> PackageManifest:
    values: dict[str, object] = {
        "schema": 1,
        "package_id": "fixture",
        "version": "1.2.3",
        "requires_af": ">=0.1.0a0,<0.2.0",
        "requires_python": ">=3.12",
        "dependencies": (),
        "providers": (PackageProviderDeclaration("fixture.provider", "fixture:Provider"),),
        "resources": (),
    }
    values.update(changes)
    return PackageManifest(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: PackageDependency("Bad ID", ">=1"), "identifier"),
        (lambda: PackageDependency("valid", ""), "constraint"),
        (
            lambda: PackageProviderDeclaration("provider", "not-an-object"),
            "module:Class",
        ),
        (
            lambda: PackageProviderDeclaration("provider", "fixture:Provider", ["other"]),  # type: ignore[arg-type]
            "tuple",
        ),
        (
            lambda: PackageProviderDeclaration("provider", "fixture:Provider", ("other", "other")),
            "unique",
        ),
        (
            lambda: PackageProviderDeclaration("provider", "fixture:Provider", ("provider",)),
            "itself",
        ),
        (
            lambda: PackageProviderDeclaration(
                "af.package-resources.0123456789abcdef01234567",
                "fixture:Provider",
            ),
            "reserved",
        ),
        (
            lambda: PackageProviderDeclaration(
                "provider",
                "fixture:Provider",
                deferred=1,  # type: ignore[arg-type]
            ),
            "boolean",
        ),
        (
            lambda: PackageResourceDeclaration("resource", "../secret", "0" * 64),
            "relative",
        ),
        (
            lambda: PackageResourceDeclaration("resource", "safe.txt", "BAD"),
            "SHA-256",
        ),
        (lambda: _valid_manifest(schema=2), "schema"),
        (lambda: _valid_manifest(dependencies=[]), "dependencies"),  # type: ignore[list-item]
        (lambda: _valid_manifest(providers=()), "providers"),
        (lambda: _valid_manifest(resources=[]), "resources"),  # type: ignore[list-item]
        (
            lambda: _valid_manifest(
                dependencies=(
                    PackageDependency("dep", ">=1"),
                    PackageDependency("dep", "<2"),
                )
            ),
            "unique",
        ),
        (
            lambda: _valid_manifest(
                package_id="fixture",
                dependencies=(PackageDependency("fixture", ">=1"),),
            ),
            "itself",
        ),
        (
            lambda: _valid_manifest(
                providers=(
                    PackageProviderDeclaration("same", "a:Provider"),
                    PackageProviderDeclaration("same", "b:Provider"),
                )
            ),
            "unique",
        ),
        (
            lambda: _valid_manifest(
                resources=(
                    PackageResourceDeclaration("same", "a", "0" * 64),
                    PackageResourceDeclaration("same", "b", "1" * 64),
                )
            ),
            "unique",
        ),
    ],
)
def test_manifest_value_objects_fail_closed(factory, message: str) -> None:
    with pytest.raises(InvalidPackageManifest, match=message):
        factory()


def test_discovered_package_approval_and_policy_validate_all_identity_fields() -> None:
    manifest = _valid_manifest()
    package = DiscoveredPackage(
        manifest,
        "0" * 64,
        "1" * 64,
        "/site",
        "/site/fixture.dist-info",
        "/site/fixture.dist-info/af-package.json",
        ("fixture.py",),
    )
    assert PackageApproval("fixture", "1.2.3", "0" * 64, "1" * 64).matches(package)
    assert not PackageApproval("fixture", "1.2.4", "0" * 64, "1" * 64).matches(package)
    with pytest.raises(InvalidPackageManifest, match="location"):
        DiscoveredPackage(manifest, "0" * 64, "1" * 64, "/site", "/dist", "/manifest", ())
    with pytest.raises(ValueError, match="policy"):
        PackageDiscoveryPolicy(enabled=1)  # type: ignore[arg-type]
    with pytest.raises(InvalidPackageManifest, match="identifier"):
        PackageDiscoveryPolicy(enabled=True, disabled_packages=frozenset({"Bad ID"}))


def test_planner_covers_duplicate_missing_version_and_external_provider_failures() -> None:
    base = DiscoveredPackage(
        _valid_manifest(),
        "0" * 64,
        "1" * 64,
        "/site",
        "/site/dist",
        "/site/manifest",
        ("fixture.py",),
    )
    with pytest.raises(PackageDependencyError, match="invalid candidate"):
        PackagePlanner().plan((object(),))  # type: ignore[arg-type]
    with pytest.raises(PackageDependencyError, match="package ids"):
        PackagePlanner().plan((base, base))
    with pytest.raises(PackageCompatibilityError, match="Python"):
        PackagePlanner().plan((base,), python_version="2.7")
    missing = DiscoveredPackage(
        _valid_manifest(
            package_id="consumer",
            dependencies=(PackageDependency("missing", ">=1"),),
        ),
        "2" * 64,
        "3" * 64,
        "/site",
        "/site/dist2",
        "/site/manifest2",
        ("consumer.py",),
    )
    with pytest.raises(PackageDependencyError, match="unavailable"):
        PackagePlanner().plan((missing,))
    with pytest.raises(PackageDependencyError, match="external provider id"):
        PackagePlanner().plan((base,), external_provider_ids=("Bad ID",))
    with pytest.raises(PackageDependencyError, match="reserved"):
        PackagePlanner().plan(
            (base,),
            external_provider_ids=("af.package-resources.0123456789abcdef01234567",),
        )
    with pytest.raises(PackageDependencyError, match="globally unique"):
        PackagePlanner().plan((base,), external_provider_ids=("fixture.provider",))


def test_manifest_adapter_preserves_hooks_and_blocks_framework_resource_rebinding() -> None:
    events: list[str] = []

    class Provider(ServiceProvider):
        def register(self, registrar: ProviderRegistrar) -> None:
            registrar.bind("value", lambda _: "ok", scope=Scope.APPLICATION)
            registrar.alias("alias", "value")
            registrar.tag("values", ServiceKey("value"))

        def boot(self, container, resources) -> None:
            del container, resources
            events.append("boot")

        def shutdown(self, container) -> None:
            del container
            events.append("shutdown")

    adapter = ManifestProviderAdapter(
        PackageProviderDeclaration("fixture.provider", "fixture:Provider"),
        Provider(),
        resource_provider_id="resource.provider",
    )
    root = Container()
    with root.provider_registration():
        adapter.register(root)
    root.freeze()
    scope = root.application_scope()
    adapter.boot(scope, object())  # type: ignore[arg-type]
    adapter.shutdown(scope)
    assert scope.resolve_token("alias") == "ok"
    assert scope.resolve_tagged("values") == ("ok",)
    assert events == ["boot", "shutdown"]
    scope.close()
    root.close()

    class BadProvider(ServiceProvider):
        def register(self, registrar: ProviderRegistrar) -> None:
            registrar.bind(PackageResources, lambda _: object())

    bad = ManifestProviderAdapter(
        PackageProviderDeclaration("bad.provider", "bad:Provider"),
        BadProvider(),
        resource_provider_id="resource.provider",
    )
    bad_root = Container()
    with (
        bad_root.provider_registration(),
        pytest.raises(PackageImportError, match="framework-owned"),
    ):
        bad.register(bad_root)
    with pytest.raises(PackageImportError, match="ServiceProvider"):
        ManifestProviderAdapter(  # type: ignore[arg-type]
            PackageProviderDeclaration("bad.provider", "bad:Provider"),
            object(),
            resource_provider_id="resource.provider",
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b'{"schema":1,"id":"fixture","version":"1.0.0"}',
        b'{"schema":1,"id":"fixture","version":"1.0.0","requires_af":">=0.1",'
        b'"requires_python":">=3.12","dependencies":"bad","providers":[],"resources":[]}',
        b'{"schema":1,"id":"fixture","version":"1.0.0","requires_af":">=0.1",'
        b'"requires_python":">=3.12","dependencies":[],"providers":[1],"resources":[]}',
        b'{"schema":1,"id":"fixture","version":"1.0.0","requires_af":">=0.1",'
        b'"requires_python":">=3.12","dependencies":[],"providers":[],'
        b'"resources":[{"id":"r","path":"r","sha256":1}]}',
    ],
)
def test_manifest_parser_rejects_noncanonical_shapes(payload: bytes) -> None:
    with pytest.raises(InvalidPackageManifest):
        package_infrastructure._parse_manifest(payload)


@pytest.mark.parametrize(
    "path",
    ["", ".", "..", "/absolute", "a/../b", "a//b", "a\\b"],
)
def test_owned_paths_are_strictly_relative(path: str) -> None:
    assert package_infrastructure._is_safe_relative_path(path) is False
    assert package_infrastructure._is_safe_relative_path("package/file.py") is True


def test_discovery_rejects_untrusted_roots_metadata_and_record_claims(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(InvalidPackageManifest, match="root"):
        InstalledPackageDiscoverer((missing,)).discover(PackageDiscoveryPolicy(enabled=True))

    file_root = tmp_path / "file"
    file_root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(InvalidPackageManifest, match="root"):
        InstalledPackageDiscoverer((file_root,)).discover(PackageDiscoveryPolicy(enabled=True))

    site = tmp_path / "site"
    _, distribution = _install_distribution(site)
    (distribution / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: wrong\nVersion: 1.0.0\n",
        encoding="utf-8",
    )
    _write_record(site, distribution)
    with pytest.raises(InvalidPackageManifest, match="identity"):
        _discover(site)

    (distribution / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: af-fixture\nVersion: 1.0.0\n",
        encoding="utf-8",
    )
    record = distribution / "RECORD"
    record.write_text("../escape,sha256=AAAA,1\n", encoding="utf-8")
    with pytest.raises(InvalidPackageManifest, match="RECORD"):
        _discover(site)


def test_verified_resources_recheck_identity_size_digest_and_encoding(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    _install_distribution(site)
    package = _discover(site)[0]
    resources = package_infrastructure._VerifiedPackageResources(package)
    with pytest.raises(PackageAuthorizationError, match="declared"):
        resources.read_bytes("missing")
    with pytest.raises(PackageAuthorizationError):
        resources.read_bytes("Bad ID")

    resource = site / "af_fixture" / "message.txt"
    resource.write_text("changed", encoding="utf-8")
    with pytest.raises(PackageAuthorizationError, match="integrity"):
        resources.read_bytes("fixture.message")

    resource.write_bytes(b"\xff")
    digest = hashlib.sha256(resource.read_bytes()).hexdigest()
    manifest = package.manifest
    altered_manifest = PackageManifest(
        manifest.schema,
        manifest.package_id,
        manifest.version,
        manifest.requires_af,
        manifest.requires_python,
        manifest.dependencies,
        manifest.providers,
        (PackageResourceDeclaration("fixture.message", "af_fixture/message.txt", digest),),
    )
    altered = DiscoveredPackage(
        altered_manifest,
        package.manifest_digest,
        package.artifact_digest,
        package.search_root,
        package.distribution_directory,
        package.manifest_path,
        package.owned_files,
    )
    with pytest.raises(PackageAuthorizationError, match="UTF-8"):
        package_infrastructure._VerifiedPackageResources(altered).read_text("fixture.message")


def test_loader_rejects_duplicate_approvals_and_invalid_provider_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = tmp_path / "site"
    sentinel, _ = _install_distribution(site)
    package = _discover(site)[0]
    plan = _plan((package,))
    approval = PackageApproval(
        package.manifest.package_id,
        package.manifest.version,
        package.manifest_digest,
        package.artifact_digest,
    )
    with pytest.raises(PackageAuthorizationError, match="unique"):
        PackageLoader().load(plan, (approval, approval))
    with pytest.raises(TypeError, match="PackagePlan"):
        PackageLoader().load(object(), ())  # type: ignore[arg-type]

    provider_file = site / "af_fixture" / "provider.py"
    provider_file.write_text("FixtureProvider = object\n", encoding="utf-8")
    _write_record(site, Path(package.distribution_directory))
    package = _discover(site)[0]
    plan = _plan((package,))
    approval = PackageApproval(
        package.manifest.package_id,
        package.manifest.version,
        package.manifest_digest,
        package.artifact_digest,
    )
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.setenv("AF_PACKAGE_SENTINEL_AF_FIXTURE", str(sentinel))
    sys.modules.pop("af_fixture.provider", None)
    sys.modules.pop("af_fixture", None)
    with pytest.raises(PackageImportError, match="ServiceProvider"):
        PackageLoader().load(plan, (approval,))


@pytest.mark.parametrize(
    ("version", "constraint", "expected"),
    [
        ("1.2.3", "==1.2.3", True),
        ("1.2.3", "==1.2.4", False),
        ("1.2.3", "!=1.2.4", True),
        ("1.2.3", "!=1.2.3", False),
        ("1.2.3", ">=1.2.3", True),
        ("1.2.3", ">=2", False),
        ("1.2.3", "<=1.2.3", True),
        ("1.2.3", "<=1", False),
        ("1.2.3", ">1.2.2", True),
        ("1.2.3", ">2", False),
        ("1.2.3", "<2", True),
        ("1.2.3", "<1", False),
        ("1.0a1", "<1.0b1", True),
        ("1.0rc1", "<1.0", True),
    ],
)
def test_version_constraints_cover_every_supported_operator(
    version: str,
    constraint: str,
    expected: bool,
) -> None:
    assert package_framework._satisfies(version, constraint) is expected


@pytest.mark.parametrize("value", [None, "v1", "1.2.dev1"])
def test_versions_reject_unsupported_syntax(value: object) -> None:
    with pytest.raises(InvalidPackageManifest):
        package_framework._ComparableVersion.parse(value)


def test_low_level_package_file_checks_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(InvalidPackageManifest, match="escapes"):
        package_infrastructure._validated_owned_path(root, outside)

    directory = root / "directory"
    directory.mkdir()
    with pytest.raises(InvalidPackageManifest, match="regular"):
        package_infrastructure._read_limited_file(root, directory, limit=10, label="fixture")

    large = root / "large"
    large.write_bytes(b"x" * 11)
    with pytest.raises(InvalidPackageManifest, match="size limit"):
        package_infrastructure._read_limited_file(root, large, limit=10, label="fixture")

    assert package_infrastructure._canonical_distribution_name("A_B.C") == "a-b-c"


@pytest.mark.parametrize(
    ("digest", "size", "message"),
    [
        ("", "not-a-number", "size"),
        ("", "2", "size does not match"),
        ("broken", "1", "digest"),
        ("md5=AAAA", "1", "digest does not match"),
        ("sha256=AAAA", "1", "digest does not match"),
    ],
)
def test_record_claims_validate_size_algorithm_and_digest(
    digest: str,
    size: str,
    message: str,
) -> None:
    with pytest.raises(InvalidPackageManifest, match=message):
        package_infrastructure._verify_record_claim(digest, size, "0" * 64, 1)
    package_infrastructure._verify_record_claim("", "", "0" * 64, 1)


def test_record_and_owned_file_lists_reject_duplicates_and_bad_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    distribution = root / "fixture.dist-info"
    distribution.mkdir(parents=True)
    data = root / "file.py"
    data.write_text("value = 1\n", encoding="utf-8")
    record = distribution / "RECORD"
    record.write_text("file.py,,\nfile.py,,\n", encoding="utf-8")
    with pytest.raises(InvalidPackageManifest, match="path"):
        package_infrastructure._read_and_hash_record(root, record)
    with pytest.raises(InvalidPackageManifest, match="owned-file list"):
        package_infrastructure._hash_owned_files(root, ())
    with pytest.raises(InvalidPackageManifest, match="owned-file path"):
        package_infrastructure._hash_owned_files(root, ("../outside",))


def test_declared_provider_and_resource_must_be_owned_and_digest_matched(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    manifest = _valid_manifest(
        resources=(PackageResourceDeclaration("resource", "resource.txt", "0" * 64),)
    )
    with pytest.raises(InvalidPackageManifest, match="provider module"):
        package_infrastructure._validate_declared_provider_files(manifest, ())
    with pytest.raises(InvalidPackageManifest, match="resource is not owned"):
        package_infrastructure._validate_declared_resources(root, manifest, ())

    resource = root / "resource.txt"
    resource.write_text("content", encoding="utf-8")
    with pytest.raises(InvalidPackageManifest, match="digest"):
        package_infrastructure._validate_declared_resources(root, manifest, ("resource.txt",))


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":NaN}',
        b"\xff",
        b'{"schema":1,"id":"x","version":"1","requires_af":">=0.1",'
        b'"requires_python":">=3.12","dependencies":[],"providers":'
        b'[{"id":"p","object":"x:P","dependencies":[1],"deferred":false}],'
        b'"resources":[]}',
    ],
)
def test_manifest_parser_rejects_nonfinite_non_utf8_and_nonstrings(payload: bytes) -> None:
    with pytest.raises(InvalidPackageManifest):
        package_infrastructure._parse_manifest(payload)
