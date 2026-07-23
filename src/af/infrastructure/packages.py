"""Filesystem-only package discovery and the explicit approved import boundary."""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import importlib
import io
import json
import re
import sys
from collections.abc import Iterable
from email.parser import BytesParser
from importlib.machinery import PathFinder
from pathlib import Path
from types import ModuleType
from typing import Any

from af.framework.container import ServiceProvider
from af.framework.packages import (
    DiscoveredPackage,
    InvalidPackageManifest,
    ManifestProviderAdapter,
    PackageApproval,
    PackageAuthorizationError,
    PackageDependency,
    PackageDiscoveryPolicy,
    PackageImportError,
    PackageManifest,
    PackagePlan,
    PackageProviderDeclaration,
    PackageResourceDeclaration,
    PackageResourceProvider,
    PackageResources,
)

__all__ = ("InstalledPackageDiscoverer", "PackageLoader")

_MANIFEST_NAME = "af-package.json"
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_METADATA_BYTES = 256 * 1024
_MAX_RECORD_BYTES = 512 * 1024
_MAX_ARTIFACT_FILES = 4096
_MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
_MAX_RESOURCE_BYTES = 16 * 1024 * 1024
_DIST_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,198}[A-Za-z0-9])?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_AUTHORIZED_MODULES: dict[str, tuple[str, str]] = {}


class InstalledPackageDiscoverer:
    """Discover wheel-like installed packages without importing their modules."""

    def __init__(self, search_paths: Iterable[str | Path]) -> None:
        self._search_paths = tuple(Path(path) for path in search_paths)

    def discover(
        self,
        policy: PackageDiscoveryPolicy | None = None,
    ) -> tuple[DiscoveredPackage, ...]:
        policy = PackageDiscoveryPolicy() if policy is None else policy
        if not isinstance(policy, PackageDiscoveryPolicy):
            raise TypeError("policy must be a PackageDiscoveryPolicy")
        if not policy.enabled:
            return ()
        disabled = {_canonical_distribution_name(value) for value in policy.disabled_packages}
        discovered: list[DiscoveredPackage] = []
        for configured_root in self._search_paths:
            root = _validated_root(configured_root)
            for distribution in sorted(root.iterdir(), key=lambda item: item.name):
                if (
                    not distribution.name.endswith(".dist-info")
                    or not distribution.is_dir()
                    or distribution.is_symlink()
                ):
                    continue
                if _distribution_directory_is_disabled(distribution.name, disabled):
                    continue
                manifest_path = distribution / _MANIFEST_NAME
                if not manifest_path.is_file():
                    continue
                distribution_name, distribution_version = _read_distribution_metadata(
                    root, distribution
                )
                if _canonical_distribution_name(distribution_name) in disabled:
                    continue
                raw_manifest = _read_limited_file(
                    root,
                    manifest_path,
                    limit=_MAX_MANIFEST_BYTES,
                    label="package manifest",
                )
                manifest = _parse_manifest(raw_manifest)
                if (
                    _canonical_distribution_name(manifest.package_id)
                    != _canonical_distribution_name(distribution_name)
                    or manifest.version != distribution_version
                ):
                    raise InvalidPackageManifest(
                        "manifest identity does not match distribution metadata"
                    )
                record_path = distribution / "RECORD"
                owned_files, artifact_digest = _read_and_hash_record(root, record_path)
                manifest_relative = manifest_path.relative_to(root).as_posix()
                if manifest_relative not in owned_files:
                    raise InvalidPackageManifest("manifest must be owned by the distribution")
                _validate_declared_provider_files(manifest, owned_files)
                _validate_declared_resources(root, manifest, owned_files)
                discovered.append(
                    DiscoveredPackage(
                        manifest=manifest,
                        manifest_digest=hashlib.sha256(raw_manifest).hexdigest(),
                        artifact_digest=artifact_digest,
                        search_root=str(root),
                        distribution_directory=str(distribution),
                        manifest_path=str(manifest_path),
                        owned_files=owned_files,
                    )
                )
        return tuple(discovered)


class PackageLoader:
    """Authorize every selected artifact before importing the first provider."""

    def load(
        self,
        plan: PackagePlan,
        approvals: Iterable[PackageApproval],
    ) -> tuple[ServiceProvider, ...]:
        if not isinstance(plan, PackagePlan):
            raise TypeError("plan must be a PackagePlan")
        approval_by_id: dict[str, PackageApproval] = {}
        for approval in approvals:
            if not isinstance(approval, PackageApproval):
                raise PackageAuthorizationError("package approval is invalid")
            if approval.package_id in approval_by_id:
                raise PackageAuthorizationError("package approvals must be unique")
            approval_by_id[approval.package_id] = approval

        # This complete pass is deliberately separate from the import loop.
        for package in plan.packages:
            selected_approval = approval_by_id.get(package.manifest.package_id)
            if selected_approval is None or not selected_approval.matches(package):
                raise PackageAuthorizationError("package is not explicitly approved")
            self._revalidate_artifact(package)
        for provider in plan.providers:
            self._validate_import_route(
                provider.package,
                provider.declaration.object_reference,
            )

        resources_by_package = {
            package.manifest.package_id: _VerifiedPackageResources(package)
            for package in plan.packages
        }
        resource_providers = {
            package.manifest.package_id: PackageResourceProvider(
                package.manifest.package_id,
                resources_by_package[package.manifest.package_id],
            )
            for package in plan.packages
        }
        declared_provider_ids = {provider.declaration.provider_id for provider in plan.providers}
        if any(
            provider.provider_id in declared_provider_ids
            for provider in resource_providers.values()
        ):
            raise PackageAuthorizationError("package provider id uses a reserved identity")

        loaded: list[ServiceProvider] = list(resource_providers.values())
        for provider in plan.providers:
            implementation = self._import_provider(
                provider.package,
                provider.declaration.object_reference,
            )
            loaded.append(
                ManifestProviderAdapter(
                    provider.declaration,
                    implementation,
                    resource_provider_id=resource_providers[
                        provider.package.manifest.package_id
                    ].provider_id,
                )
            )
        return tuple(loaded)

    @staticmethod
    def _revalidate_artifact(package: DiscoveredPackage) -> None:
        root = Path(package.search_root)
        try:
            raw_manifest = _read_limited_file(
                root,
                Path(package.manifest_path),
                limit=_MAX_MANIFEST_BYTES,
                label="package manifest",
            )
            artifact_digest = _hash_owned_files(root, package.owned_files)
        except (InvalidPackageManifest, OSError) as error:
            raise PackageAuthorizationError("approved package artifact is unavailable") from error
        if (
            hashlib.sha256(raw_manifest).hexdigest() != package.manifest_digest
            or artifact_digest != package.artifact_digest
        ):
            raise PackageAuthorizationError("approved package artifact has changed")

    @staticmethod
    def _import_provider(
        package: DiscoveredPackage,
        object_reference: str,
    ) -> ServiceProvider:
        module_name, attribute_path = object_reference.split(":", 1)
        try:
            module = importlib.import_module(module_name)
            if not _module_belongs_to_package(module, package):
                raise PackageImportError("provider module is outside the approved package")
            binding = (package.manifest_digest, package.artifact_digest)
            module_parts = module_name.split(".")
            for index in range(1, len(module_parts) + 1):
                imported_name = ".".join(module_parts[:index])
                imported = sys.modules.get(imported_name)
                if imported is not None and _module_belongs_to_package(imported, package):
                    _AUTHORIZED_MODULES[imported_name] = binding
            target: Any = module
            for attribute in attribute_path.split("."):
                target = getattr(target, attribute)
            if (
                not isinstance(target, type)
                or not issubclass(target, ServiceProvider)
                or target.__module__ != module_name
            ):
                raise PackageImportError("provider target must be a package ServiceProvider class")
            provider = target()
            if not isinstance(provider, ServiceProvider):
                raise PackageImportError("provider construction returned an invalid object")
            return provider
        except PackageImportError:
            raise
        except Exception as error:
            raise PackageImportError("approved provider could not be imported") from error

    @staticmethod
    def _validate_import_route(
        package: DiscoveredPackage,
        object_reference: str,
    ) -> None:
        module_name = object_reference.split(":", 1)[0]
        module_parts = module_name.split(".")
        binding = (package.manifest_digest, package.artifact_digest)
        search_path: tuple[str, ...] | None = None
        for index in range(1, len(module_parts) + 1):
            qualified_name = ".".join(module_parts[:index])
            imported = sys.modules.get(qualified_name)
            try:
                if imported is not None:
                    if _AUTHORIZED_MODULES.get(
                        qualified_name
                    ) != binding or not _module_belongs_to_package(imported, package):
                        raise PackageAuthorizationError(
                            "package provider was imported before authorization"
                        )
                    origin = getattr(imported, "__file__", None)
                    locations_value = getattr(imported, "__path__", None)
                    locations = (
                        None
                        if locations_value is None
                        else tuple(str(value) for value in locations_value)
                    )
                else:
                    spec = PathFinder.find_spec(qualified_name, search_path)
                    origin = None if spec is None else spec.origin
                    locations = (
                        None
                        if spec is None or spec.submodule_search_locations is None
                        else tuple(str(value) for value in spec.submodule_search_locations)
                    )
                if not _origin_belongs_to_package(origin, package):
                    raise PackageAuthorizationError(
                        "package import route does not select the approved artifact"
                    )
                if index < len(module_parts):
                    if locations is None or len(locations) != 1:
                        raise PackageAuthorizationError("package import route is ambiguous")
                    origin_parent = Path(str(origin)).resolve(strict=True).parent
                    if Path(locations[0]).resolve(strict=True) != origin_parent:
                        raise PackageAuthorizationError("package import route is ambiguous")
                    search_path = locations
            except PackageAuthorizationError:
                raise
            except (ImportError, OSError, TypeError, ValueError) as error:
                raise PackageAuthorizationError("package import route is unavailable") from error


class _VerifiedPackageResources(PackageResources):
    def __init__(self, package: DiscoveredPackage) -> None:
        self._root = Path(package.search_root)
        self._resources = {
            resource.resource_id: resource for resource in package.manifest.resources
        }

    def read_bytes(self, resource_id: str) -> bytes:
        declaration = self._resources.get(resource_id)
        if declaration is None:
            raise PackageAuthorizationError("package resource is not declared")
        try:
            data = _read_limited_file(
                self._root,
                self._root / declaration.path,
                limit=_MAX_RESOURCE_BYTES,
                label="package resource",
            )
        except InvalidPackageManifest as error:
            raise PackageAuthorizationError("package resource integrity check failed") from error
        if hashlib.sha256(data).hexdigest() != declaration.sha256:
            raise PackageAuthorizationError("package resource integrity check failed")
        return data

    def read_text(self, resource_id: str) -> str:
        try:
            return self.read_bytes(resource_id).decode("utf-8")
        except UnicodeDecodeError as error:
            raise PackageAuthorizationError("package resource is not valid UTF-8") from error


def _validated_root(path: Path) -> Path:
    try:
        if path.is_symlink():
            raise InvalidPackageManifest("package search root must not be a symlink")
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise InvalidPackageManifest("package search root is unavailable") from error
    if not resolved.is_dir():
        raise InvalidPackageManifest("package search root must be a directory")
    return resolved


def _canonical_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _distribution_directory_is_disabled(
    directory_name: str,
    disabled: set[str],
) -> bool:
    stem = directory_name.removesuffix(".dist-info")
    distribution_name, separator, _ = stem.rpartition("-")
    return bool(
        separator
        and distribution_name
        and _canonical_distribution_name(distribution_name) in disabled
    )


def _read_distribution_metadata(root: Path, distribution: Path) -> tuple[str, str]:
    raw = _read_limited_file(
        root,
        distribution / "METADATA",
        limit=_MAX_METADATA_BYTES,
        label="distribution metadata",
    )
    try:
        message = BytesParser().parsebytes(raw, headersonly=True)
        names = message.get_all("Name", [])
        versions = message.get_all("Version", [])
    except Exception as error:
        raise InvalidPackageManifest("distribution metadata is malformed") from error
    if len(names) != 1 or len(versions) != 1:
        raise InvalidPackageManifest("distribution metadata identity is ambiguous")
    name = names[0]
    version = versions[0]
    if (
        type(name) is not str
        or _DIST_NAME.fullmatch(name) is None
        or type(version) is not str
        or not version
        or len(version) > 100
        or _CONTROL.search(version)
    ):
        raise InvalidPackageManifest("distribution metadata identity is invalid")
    return name, version


def _read_limited_file(root: Path, path: Path, *, limit: int, label: str) -> bytes:
    validated = _validated_owned_path(root, path)
    try:
        size = validated.stat().st_size
        if size > limit:
            raise InvalidPackageManifest(f"{label} exceeds its size limit")
        data = validated.read_bytes()
    except InvalidPackageManifest:
        raise
    except OSError as error:
        raise InvalidPackageManifest(f"{label} is unavailable") from error
    if len(data) != size or len(data) > limit:
        raise InvalidPackageManifest(f"{label} changed while being read")
    return data


def _validated_owned_path(root: Path, path: Path) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise InvalidPackageManifest("package file escapes its search root") from None
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise InvalidPackageManifest("package file path is invalid")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise InvalidPackageManifest("package files must not use symlinks")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise InvalidPackageManifest("package file is unavailable") from error
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise InvalidPackageManifest("package file must be regular and contained")
    return resolved


def _parse_manifest(raw: bytes) -> PackageManifest:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise InvalidPackageManifest("manifest contains duplicate keys")
            result[key] = value
        return result

    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda _: (_ for _ in ()).throw(
                InvalidPackageManifest("manifest contains a non-finite number")
            ),
        )
    except InvalidPackageManifest:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidPackageManifest("manifest is not strict UTF-8 JSON") from error
    root = _strict_object(
        document,
        {
            "schema",
            "id",
            "version",
            "requires_af",
            "requires_python",
            "dependencies",
            "providers",
            "resources",
        },
        "manifest",
    )
    dependencies = tuple(
        PackageDependency(
            package_id=item["id"],
            version=item["version"],
        )
        for item in _strict_object_list(
            root["dependencies"],
            {"id", "version"},
            "package dependency",
        )
    )
    providers = tuple(
        PackageProviderDeclaration(
            provider_id=item["id"],
            object_reference=item["object"],
            dependencies=_strict_string_tuple(item["dependencies"], "provider dependencies"),
            deferred=item["deferred"],
        )
        for item in _strict_object_list(
            root["providers"],
            {"id", "object", "dependencies", "deferred"},
            "provider",
        )
    )
    resources = tuple(
        PackageResourceDeclaration(
            resource_id=item["id"],
            path=item["path"],
            sha256=item["sha256"],
        )
        for item in _strict_object_list(
            root["resources"],
            {"id", "path", "sha256"},
            "resource",
        )
    )
    return PackageManifest(
        schema=root["schema"],
        package_id=root["id"],
        version=root["version"],
        requires_af=root["requires_af"],
        requires_python=root["requires_python"],
        dependencies=dependencies,
        providers=providers,
        resources=resources,
    )


def _strict_object(
    value: object,
    keys: set[str],
    label: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise InvalidPackageManifest(f"{label} fields are invalid")
    if any(type(key) is not str for key in value):
        raise InvalidPackageManifest(f"{label} keys must be strings")
    return value


def _strict_object_list(
    value: object,
    keys: set[str],
    label: str,
) -> tuple[dict[str, Any], ...]:
    if type(value) is not list or len(value) > 256:
        raise InvalidPackageManifest(f"{label} list is invalid")
    return tuple(_strict_object(item, keys, label) for item in value)


def _strict_string_tuple(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list or len(value) > 256 or any(type(item) is not str for item in value):
        raise InvalidPackageManifest(f"{label} must be a bounded string list")
    return tuple(value)


def _read_and_hash_record(root: Path, record_path: Path) -> tuple[tuple[str, ...], str]:
    raw_record = _read_limited_file(
        root,
        record_path,
        limit=_MAX_RECORD_BYTES,
        label="distribution RECORD",
    )
    try:
        rows = tuple(csv.reader(io.StringIO(raw_record.decode("utf-8")), strict=True))
    except (UnicodeDecodeError, csv.Error) as error:
        raise InvalidPackageManifest("distribution RECORD is malformed") from error
    if not rows or len(rows) > _MAX_ARTIFACT_FILES:
        raise InvalidPackageManifest("distribution RECORD file count is invalid")
    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if len(row) != 3:
            raise InvalidPackageManifest("distribution RECORD row is invalid")
        relative, recorded_digest, recorded_size = row
        if not _is_safe_relative_path(relative) or relative in seen:
            raise InvalidPackageManifest("distribution RECORD path is invalid")
        seen.add(relative)
        entries.append((relative, recorded_digest, recorded_size))

    artifact = hashlib.sha256()
    total_size = 0
    for relative, recorded_digest, recorded_size in sorted(entries):
        actual_digest, actual_size = _hash_file(root, root / relative)
        total_size += actual_size
        if total_size > _MAX_ARTIFACT_BYTES:
            raise InvalidPackageManifest("package artifact exceeds its size limit")
        _verify_record_claim(recorded_digest, recorded_size, actual_digest, actual_size)
        _update_artifact_digest(artifact, relative, actual_digest, actual_size)
    return tuple(sorted(seen)), artifact.hexdigest()


def _hash_owned_files(root: Path, owned_files: tuple[str, ...]) -> str:
    if not owned_files or len(owned_files) > _MAX_ARTIFACT_FILES:
        raise InvalidPackageManifest("package owned-file list is invalid")
    artifact = hashlib.sha256()
    total_size = 0
    for relative in owned_files:
        if not _is_safe_relative_path(relative):
            raise InvalidPackageManifest("package owned-file path is invalid")
        actual_digest, actual_size = _hash_file(root, root / relative)
        total_size += actual_size
        if total_size > _MAX_ARTIFACT_BYTES:
            raise InvalidPackageManifest("package artifact exceeds its size limit")
        _update_artifact_digest(artifact, relative, actual_digest, actual_size)
    return artifact.hexdigest()


def _hash_file(root: Path, path: Path) -> tuple[str, int]:
    validated = _validated_owned_path(root, path)
    digest = hashlib.sha256()
    size = 0
    try:
        with validated.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_ARTIFACT_BYTES:
                    raise InvalidPackageManifest("package file exceeds its size limit")
                digest.update(chunk)
    except InvalidPackageManifest:
        raise
    except OSError as error:
        raise InvalidPackageManifest("package file is unavailable") from error
    return digest.hexdigest(), size


def _verify_record_claim(
    recorded_digest: str,
    recorded_size: str,
    actual_digest: str,
    actual_size: int,
) -> None:
    if recorded_size:
        if not recorded_size.isascii() or not recorded_size.isdigit():
            raise InvalidPackageManifest("distribution RECORD size is invalid")
        if int(recorded_size) != actual_size:
            raise InvalidPackageManifest("distribution RECORD size does not match")
    if not recorded_digest:
        return
    try:
        algorithm, encoded = recorded_digest.split("=", 1)
        padding = "=" * ((4 - len(encoded) % 4) % 4)
        expected = base64.urlsafe_b64decode(encoded + padding).hex()
    except (ValueError, binascii.Error) as error:
        raise InvalidPackageManifest("distribution RECORD digest is invalid") from error
    if algorithm != "sha256" or expected != actual_digest:
        raise InvalidPackageManifest("distribution RECORD digest does not match")


def _update_artifact_digest(
    artifact: Any,
    relative: str,
    file_digest: str,
    size: int,
) -> None:
    artifact.update(relative.encode("utf-8"))
    artifact.update(b"\0")
    artifact.update(str(size).encode("ascii"))
    artifact.update(b"\0")
    artifact.update(bytes.fromhex(file_digest))


def _is_safe_relative_path(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= 1024
        and "\\" not in value
        and not value.startswith("/")
        and _CONTROL.search(value) is None
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _validate_declared_provider_files(
    manifest: PackageManifest,
    owned_files: tuple[str, ...],
) -> None:
    owned = set(owned_files)
    for provider in manifest.providers:
        module_name = provider.object_reference.split(":", 1)[0]
        module_file = module_name.replace(".", "/") + ".py"
        package_file = module_name.replace(".", "/") + "/__init__.py"
        if module_file not in owned and package_file not in owned:
            raise InvalidPackageManifest("provider module is not owned by the distribution")


def _validate_declared_resources(
    root: Path,
    manifest: PackageManifest,
    owned_files: tuple[str, ...],
) -> None:
    owned = set(owned_files)
    for resource in manifest.resources:
        if resource.path not in owned:
            raise InvalidPackageManifest("declared resource is not owned by the distribution")
        data = _read_limited_file(
            root,
            root / resource.path,
            limit=_MAX_RESOURCE_BYTES,
            label="package resource",
        )
        if hashlib.sha256(data).hexdigest() != resource.sha256:
            raise InvalidPackageManifest("declared resource digest does not match")


def _module_belongs_to_package(
    module: ModuleType,
    package: DiscoveredPackage,
) -> bool:
    return _origin_belongs_to_package(getattr(module, "__file__", None), package)


def _origin_belongs_to_package(
    origin: object,
    package: DiscoveredPackage,
) -> bool:
    if type(origin) is not str:
        return False
    try:
        resolved = Path(origin).resolve(strict=True)
        root = Path(package.search_root).resolve(strict=True)
        relative = resolved.relative_to(root).as_posix()
    except (OSError, ValueError):
        return False
    return relative in package.owned_files
