"""Inert package declarations, planning, approval, and provider adaptation."""

from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from functools import total_ordering
from typing import Protocol

from af import __version__ as AF_VERSION
from af.framework.container import (
    AsyncFactory,
    Container,
    Factory,
    ProviderRegistrar,
    ProviderResourceRegistry,
    Qualifier,
    Scope,
    ServiceKey,
    ServiceProvider,
)
from af.framework.errors import AFError

__all__ = (
    "AF_VERSION",
    "DiscoveredPackage",
    "InvalidPackageManifest",
    "ManifestProviderAdapter",
    "PackageApproval",
    "PackageAuthorizationError",
    "PackageCompatibilityError",
    "PackageDependency",
    "PackageDependencyError",
    "PackageDiscoveryPolicy",
    "PackageImportError",
    "PackageManifest",
    "PackagePlan",
    "PackagePlanner",
    "PackageProviderDeclaration",
    "PackageResourceDeclaration",
    "PackageResourceProvider",
    "PackageResources",
    "PlannedProvider",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_OBJECT_REFERENCE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:"
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(
    r"^(?P<release>[0-9]+(?:\.[0-9]+){0,7})"
    r"(?:(?P<pre>a|b|rc)(?P<pre_number>[0-9]+))?$"
)
_CONSTRAINT = re.compile(r"^(>=|<=|==|!=|>|<)\s*(.+)$")
_PYTHON_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
_PACKAGE_RESOURCE_PROVIDER_PREFIX = "af.package-resources."


class PackageError(AFError):
    """Base class for package-boundary failures."""


class InvalidPackageManifest(PackageError, ValueError):
    """Raised when inert package metadata is malformed."""


class PackageCompatibilityError(PackageError):
    """Raised when a package cannot run on the current AF/Python host."""


class PackageDependencyError(PackageError):
    """Raised when package or provider dependencies cannot be ordered."""


class PackageAuthorizationError(PackageError):
    """Raised before imports when package approval is absent or stale."""


class PackageImportError(PackageError):
    """Raised after authorization when a provider cannot be loaded safely."""


def _validate_identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise InvalidPackageManifest(f"{field} must be a canonical identifier")
    return value


def _validate_digest(value: object, field: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise InvalidPackageManifest(f"{field} must be a lowercase SHA-256 digest")
    return value


@total_ordering
@dataclass(frozen=True, slots=True, eq=False)
class _ComparableVersion:
    release: tuple[int, ...]
    prerelease: tuple[int, int]

    @classmethod
    def parse(cls, value: object) -> _ComparableVersion:
        if type(value) is not str:
            raise InvalidPackageManifest("version must be a string")
        matched = _VERSION.fullmatch(value)
        if matched is None:
            raise InvalidPackageManifest("version uses unsupported syntax")
        release = tuple(int(part) for part in matched.group("release").split("."))
        pre = matched.group("pre")
        if pre is None:
            prerelease = (3, 0)
        else:
            prerelease = ({"a": 0, "b": 1, "rc": 2}[pre], int(matched.group("pre_number")))
        return cls(release, prerelease)

    def _key(self) -> tuple[tuple[int, ...], tuple[int, int]]:
        return (self.release + (0,) * (8 - len(self.release)), self.prerelease)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _ComparableVersion):
            return NotImplemented
        return self._key() < other._key()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _ComparableVersion):
            return NotImplemented
        return self._key() == other._key()


def _parse_constraint(value: object) -> tuple[tuple[str, _ComparableVersion], ...]:
    if type(value) is not str or not value or len(value) > 256:
        raise InvalidPackageManifest("version constraint must be a bounded string")
    clauses: list[tuple[str, _ComparableVersion]] = []
    for raw_clause in value.split(","):
        clause = raw_clause.strip()
        matched = _CONSTRAINT.fullmatch(clause)
        if matched is None:
            raise InvalidPackageManifest("version constraint uses unsupported syntax")
        clauses.append((matched.group(1), _ComparableVersion.parse(matched.group(2).strip())))
    return tuple(clauses)


def _satisfies(version: str, constraint: str) -> bool:
    candidate = _ComparableVersion.parse(version)
    for operator, expected in _parse_constraint(constraint):
        if operator == "==" and candidate != expected:
            return False
        if operator == "!=" and candidate == expected:
            return False
        if operator == ">=" and candidate < expected:
            return False
        if operator == "<=" and candidate > expected:
            return False
        if operator == ">" and candidate <= expected:
            return False
        if operator == "<" and candidate >= expected:
            return False
    return True


@dataclass(frozen=True, slots=True)
class PackageDependency:
    package_id: str
    version: str

    def __post_init__(self) -> None:
        _validate_identifier(self.package_id, "package dependency id")
        _parse_constraint(self.version)


@dataclass(frozen=True, slots=True)
class PackageProviderDeclaration:
    provider_id: str
    object_reference: str
    dependencies: tuple[str, ...] = ()
    deferred: bool = False

    def __post_init__(self) -> None:
        _validate_identifier(self.provider_id, "provider id")
        if self.provider_id.startswith(_PACKAGE_RESOURCE_PROVIDER_PREFIX):
            raise InvalidPackageManifest("provider id uses a framework-reserved namespace")
        if (
            type(self.object_reference) is not str
            or len(self.object_reference) > 512
            or _OBJECT_REFERENCE.fullmatch(self.object_reference) is None
        ):
            raise InvalidPackageManifest("provider object must be a module:Class reference")
        if type(self.dependencies) is not tuple:
            raise InvalidPackageManifest("provider dependencies must be a tuple")
        for dependency in self.dependencies:
            _validate_identifier(dependency, "provider dependency")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise InvalidPackageManifest("provider dependencies must be unique")
        if self.provider_id in self.dependencies:
            raise InvalidPackageManifest("provider cannot depend on itself")
        if type(self.deferred) is not bool:
            raise InvalidPackageManifest("provider deferred must be boolean")


@dataclass(frozen=True, slots=True)
class PackageResourceDeclaration:
    resource_id: str
    path: str
    sha256: str

    def __post_init__(self) -> None:
        _validate_identifier(self.resource_id, "resource id")
        if (
            type(self.path) is not str
            or not self.path
            or len(self.path) > 512
            or "\\" in self.path
            or self.path.startswith("/")
            or any(part in {"", ".", ".."} for part in self.path.split("/"))
        ):
            raise InvalidPackageManifest("resource path must be a safe relative path")
        _validate_digest(self.sha256, "resource digest")


class PackageResources(Protocol):
    """Digest-checked, identifier-only access to approved package resources."""

    def read_bytes(self, resource_id: str) -> bytes: ...

    def read_text(self, resource_id: str) -> str: ...


class PackageResourceProvider(ServiceProvider):
    """Trusted declaration provider for one package's verified resource reader."""

    def __init__(self, package_id: str, resources: PackageResources) -> None:
        _validate_identifier(package_id, "package id")
        self.provider_id = (
            "af.package-resources." + hashlib.sha256(package_id.encode("utf-8")).hexdigest()[:24]
        )
        self._package_id = package_id
        self._resources = resources

    def register(self, container: ProviderRegistrar) -> None:
        resources = self._resources
        container.bind(
            PackageResources,
            lambda _: resources,
            qualifier=self._package_id,
            scope=Scope.APPLICATION,
            owned=False,
        )


@dataclass(frozen=True, slots=True)
class PackageManifest:
    schema: int
    package_id: str
    version: str
    requires_af: str
    requires_python: str
    dependencies: tuple[PackageDependency, ...]
    providers: tuple[PackageProviderDeclaration, ...]
    resources: tuple[PackageResourceDeclaration, ...]

    def __post_init__(self) -> None:
        if type(self.schema) is not int or self.schema != 1:
            raise InvalidPackageManifest("unsupported package manifest schema")
        _validate_identifier(self.package_id, "package id")
        _ComparableVersion.parse(self.version)
        _parse_constraint(self.requires_af)
        _parse_constraint(self.requires_python)
        if type(self.dependencies) is not tuple:
            raise InvalidPackageManifest("package dependencies must be a tuple")
        if type(self.providers) is not tuple or not self.providers:
            raise InvalidPackageManifest("package providers must be a non-empty tuple")
        if type(self.resources) is not tuple:
            raise InvalidPackageManifest("package resources must be a tuple")
        if len({dependency.package_id for dependency in self.dependencies}) != len(
            self.dependencies
        ):
            raise InvalidPackageManifest("package dependency ids must be unique")
        if self.package_id in {dependency.package_id for dependency in self.dependencies}:
            raise InvalidPackageManifest("package cannot depend on itself")
        if len({provider.provider_id for provider in self.providers}) != len(self.providers):
            raise InvalidPackageManifest("provider ids must be unique within a package")
        if len({resource.resource_id for resource in self.resources}) != len(self.resources):
            raise InvalidPackageManifest("resource ids must be unique within a package")


@dataclass(frozen=True, slots=True)
class DiscoveredPackage:
    manifest: PackageManifest
    manifest_digest: str
    artifact_digest: str
    search_root: str
    distribution_directory: str
    manifest_path: str
    owned_files: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_digest(self.manifest_digest, "manifest digest")
        _validate_digest(self.artifact_digest, "artifact digest")
        if (
            type(self.search_root) is not str
            or type(self.distribution_directory) is not str
            or type(self.manifest_path) is not str
            or type(self.owned_files) is not tuple
            or not self.owned_files
        ):
            raise InvalidPackageManifest("discovered package location is invalid")


@dataclass(frozen=True, slots=True)
class PackageApproval:
    package_id: str
    version: str
    manifest_digest: str
    artifact_digest: str

    def __post_init__(self) -> None:
        _validate_identifier(self.package_id, "approval package id")
        _ComparableVersion.parse(self.version)
        _validate_digest(self.manifest_digest, "approval manifest digest")
        _validate_digest(self.artifact_digest, "approval artifact digest")

    def matches(self, package: DiscoveredPackage) -> bool:
        return (
            self.package_id == package.manifest.package_id
            and self.version == package.manifest.version
            and self.manifest_digest == package.manifest_digest
            and self.artifact_digest == package.artifact_digest
        )


@dataclass(frozen=True, slots=True)
class PackageDiscoveryPolicy:
    enabled: bool = False
    disabled_packages: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or type(self.disabled_packages) is not frozenset:
            raise ValueError("package discovery policy is invalid")
        normalized: set[str] = set()
        for package_id in self.disabled_packages:
            normalized.add(_validate_identifier(package_id, "disabled package id"))
        object.__setattr__(self, "disabled_packages", frozenset(normalized))


@dataclass(frozen=True, slots=True)
class PlannedProvider:
    package: DiscoveredPackage
    declaration: PackageProviderDeclaration


@dataclass(frozen=True, slots=True)
class PackagePlan:
    packages: tuple[DiscoveredPackage, ...]
    providers: tuple[PlannedProvider, ...]


class PackagePlanner:
    """Validate compatibility and both dependency graphs without importing providers."""

    def plan(
        self,
        packages: Iterable[DiscoveredPackage],
        *,
        af_version: str = AF_VERSION,
        python_version: str = _PYTHON_VERSION,
        external_provider_ids: Iterable[str] = (),
    ) -> PackagePlan:
        _ComparableVersion.parse(af_version)
        _ComparableVersion.parse(python_version)
        package_by_id: dict[str, DiscoveredPackage] = {}
        for package in packages:
            if not isinstance(package, DiscoveredPackage):
                raise PackageDependencyError("package plan contains an invalid candidate")
            package_id = package.manifest.package_id
            if package_id in package_by_id:
                raise PackageDependencyError("package ids must be unique")
            package_by_id[package_id] = package

        for package in package_by_id.values():
            manifest = package.manifest
            if not _satisfies(af_version, manifest.requires_af):
                raise PackageCompatibilityError("package is incompatible with AF")
            if not _satisfies(python_version, manifest.requires_python):
                raise PackageCompatibilityError("package is incompatible with Python")
            for dependency in manifest.dependencies:
                selected = package_by_id.get(dependency.package_id)
                if selected is None:
                    raise PackageDependencyError("package dependency is unavailable")
                if not _satisfies(selected.manifest.version, dependency.version):
                    raise PackageCompatibilityError("package dependency version is incompatible")

        package_order = _topological_order(
            {
                package_id: tuple(
                    dependency.package_id for dependency in package.manifest.dependencies
                )
                for package_id, package in package_by_id.items()
            },
            error_message="package dependency graph is invalid",
        )

        external = set(external_provider_ids)
        for provider_id in external:
            try:
                _validate_identifier(provider_id, "external provider id")
            except InvalidPackageManifest as error:
                raise PackageDependencyError("external provider id is invalid") from error
            if provider_id.startswith(_PACKAGE_RESOURCE_PROVIDER_PREFIX):
                raise PackageDependencyError("external provider id uses a reserved namespace")

        provider_by_id: dict[str, PlannedProvider] = {}
        for package_id in package_order:
            package = package_by_id[package_id]
            for declaration in package.manifest.providers:
                if declaration.provider_id in provider_by_id or declaration.provider_id in external:
                    raise PackageDependencyError("provider ids must be globally unique")
                provider_by_id[declaration.provider_id] = PlannedProvider(package, declaration)
        for provider in provider_by_id.values():
            for provider_dependency in provider.declaration.dependencies:
                if (
                    provider_dependency not in provider_by_id
                    and provider_dependency not in external
                ):
                    raise PackageDependencyError("provider dependency is unavailable")

        provider_order = _topological_order(
            {
                provider_id: tuple(
                    dependency
                    for dependency in provider.declaration.dependencies
                    if dependency in provider_by_id
                )
                for provider_id, provider in provider_by_id.items()
            },
            error_message="provider dependency graph is invalid",
        )
        return PackagePlan(
            tuple(package_by_id[package_id] for package_id in package_order),
            tuple(provider_by_id[provider_id] for provider_id in provider_order),
        )


def _topological_order(
    dependencies: dict[str, tuple[str, ...]],
    *,
    error_message: str,
) -> tuple[str, ...]:
    for node, required in dependencies.items():
        if node in required or any(dependency not in dependencies for dependency in required):
            raise PackageDependencyError(error_message)
    indegree = {node: len(required) for node, required in dependencies.items()}
    dependants: dict[str, list[str]] = {node: [] for node in dependencies}
    for node, required in dependencies.items():
        for dependency in required:
            dependants[dependency].append(node)
    available = sorted(node for node, count in indegree.items() if count == 0)
    ordered: list[str] = []
    while available:
        node = available.pop(0)
        ordered.append(node)
        for dependant in sorted(dependants[node]):
            indegree[dependant] -= 1
            if indegree[dependant] == 0:
                available.append(dependant)
                available.sort()
    if len(ordered) != len(dependencies):
        raise PackageDependencyError(error_message)
    return tuple(ordered)


class ManifestProviderAdapter(ServiceProvider):
    """Apply approved manifest identity to a loaded provider implementation."""

    def __init__(
        self,
        declaration: PackageProviderDeclaration,
        provider: ServiceProvider,
        *,
        resource_provider_id: str,
    ) -> None:
        if not isinstance(provider, ServiceProvider):
            raise PackageImportError("provider object does not implement ServiceProvider")
        _validate_identifier(resource_provider_id, "resource provider id")
        self.provider_id = declaration.provider_id
        self.dependencies = (*declaration.dependencies, resource_provider_id)
        self.deferred = declaration.deferred
        self._provider = provider

    def register(self, container: ProviderRegistrar) -> None:
        self._provider.register(_PackageRegistrar(container))

    def boot(
        self,
        container: Container,
        resources: ProviderResourceRegistry,
    ) -> None | Awaitable[None]:
        return self._provider.boot(container, resources)

    def shutdown(self, container: Container) -> None | Awaitable[None]:
        return self._provider.shutdown(container)


class _PackageRegistrar(ProviderRegistrar):
    """Keep package code away from framework-owned resource bindings."""

    def __init__(self, registrar: ProviderRegistrar) -> None:
        self._registrar = registrar

    def bind(
        self,
        token: object,
        factory: Factory,
        *,
        qualifier: Qualifier | str | None = None,
        scope: Scope = Scope.TRANSIENT,
        owned: bool = True,
        replace: bool = False,
    ) -> None:
        self._reject_reserved(token)
        self._registrar.bind(
            token,
            factory,
            qualifier=qualifier,
            scope=scope,
            owned=owned,
            replace=replace,
        )

    def bind_async(
        self,
        token: object,
        factory: AsyncFactory,
        *,
        qualifier: Qualifier | str | None = None,
        scope: Scope = Scope.TRANSIENT,
        owned: bool = True,
        replace: bool = False,
    ) -> None:
        self._reject_reserved(token)
        self._registrar.bind_async(
            token,
            factory,
            qualifier=qualifier,
            scope=scope,
            owned=owned,
            replace=replace,
        )

    def alias(
        self,
        alias: object,
        target: object,
        *,
        alias_qualifier: Qualifier | str | None = None,
        target_qualifier: Qualifier | str | None = None,
        replace: bool = False,
    ) -> None:
        self._reject_reserved(alias)
        self._reject_reserved(target)
        self._registrar.alias(
            alias,
            target,
            alias_qualifier=alias_qualifier,
            target_qualifier=target_qualifier,
            replace=replace,
        )

    def tag(self, name: str, *services: ServiceKey | object) -> None:
        for service in services:
            token = service.token if type(service) is ServiceKey else service
            self._reject_reserved(token)
        self._registrar.tag(name, *services)

    @staticmethod
    def _reject_reserved(token: object) -> None:
        if token is PackageResources:
            raise PackageImportError("package resource binding is framework-owned")
