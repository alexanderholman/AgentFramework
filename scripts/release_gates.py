#!/usr/bin/env python3
"""Validate and report AF's private production-MVP release gates."""

from __future__ import annotations

import argparse
import base64
import configparser
import csv
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from packaging.markers import Marker
from packaging.metadata import Metadata
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import InvalidName, canonicalize_name
from yaml.tokens import (
    AliasToken,
    AnchorToken,
    BlockEndToken,
    BlockMappingStartToken,
    BlockSequenceStartToken,
    FlowMappingEndToken,
    FlowMappingStartToken,
    FlowSequenceEndToken,
    FlowSequenceStartToken,
    ScalarToken,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path("release/production-mvp.yaml")
MAX_MANIFEST_BYTES = 256 * 1024
MAX_YAML_TOKENS = 20_000
MAX_YAML_DEPTH = 64
MAX_YAML_SCALAR_LENGTH = 16_384
MAX_ARCHIVE_MEMBERS = 5_000
MAX_ARCHIVE_FILE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 128 * 1024 * 1024
SCHEMA_VERSION = "1.0.0"
EXPECTED_POLICY_SHA256 = "9f9dfb0d3621390654253c60aae169a76fb35f28f0f6ced69fa772f03592ae50"

ROOT_FIELDS = frozenset({"schema_version", "profile", "gates"})
PROFILE_FIELDS = frozenset(
    {
        "id",
        "title",
        "release_target",
        "maturity",
        "constraints",
        "supported_surfaces",
        "optional_extensions",
        "excluded",
    }
)
CONSTRAINT_FIELDS = frozenset(
    {
        "operating_system",
        "python",
        "nodes",
        "tenants",
        "databases",
        "live_processes",
        "database",
        "routes",
        "network_exposure",
        "remote_inference",
    }
)
GATE_FIELDS = frozenset(
    {
        "id",
        "category",
        "title",
        "requirement",
        "blocking",
        "verification",
        "state",
        "evidence",
        "next_action",
        "issues",
    }
)
EVIDENCE_FIELDS = frozenset({"kind", "ref", "description"})
CATEGORIES = frozenset(
    {
        "scope",
        "quality",
        "artifact",
        "runtime",
        "security",
        "data",
        "operations",
        "accessibility",
        "release",
    }
)
STATES = frozenset({"met", "unmet", "pending"})
VERIFICATION_KINDS = frozenset({"automated", "manual", "hybrid"})
EVIDENCE_KINDS = frozenset({"file", "script", "ci", "manual"})
GATE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*$")
VERSION_PATTERN = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']$', re.MULTILINE)
FORBIDDEN_COMMON_PREFIXES = (
    ".git/",
    ".venv/",
    "storage/",
)
FORBIDDEN_WHEEL_PREFIXES = (
    "tests/",
    "scripts/",
)
FORBIDDEN_ARCHIVE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
UV_BUILD_OPTIONS = (
    "--offline",
    "--no-sources",
    "--no-build-isolation",
)
PACKAGE_SMOKE = """
from importlib.metadata import distributions
from importlib.resources import files
from pathlib import Path
import sys

target = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(target))

import af
from af.bootstrap import create_application
from af.config import Settings
from af.interfaces.cli import create_cli
from typer.testing import CliRunner

module_path = Path(af.__file__).resolve()
if target not in module_path.parents:
    raise SystemExit("wheel smoke imported AF outside the temporary target")
if not files("af").joinpath("views/templates/base.html").is_file():
    raise SystemExit("installed wheel has no base HTML template")

installed = [
    distribution
    for distribution in distributions(path=[str(target)])
    if distribution.metadata["Name"] == "af-agent-framework"
]
if len(installed) != 1:
    raise SystemExit("temporary target does not contain exactly one AF distribution")
entry_points = {
    entry_point.name: entry_point
    for entry_point in installed[0].entry_points
    if entry_point.group == "console_scripts"
}
if set(entry_points) != {"af", "af-mcp"}:
    raise SystemExit("installed wheel console entry points do not match the release contract")
for name, entry_point in entry_points.items():
    try:
        loaded = entry_point.load()
    except (AttributeError, ImportError, ModuleNotFoundError) as exc:
        raise SystemExit(f"installed wheel entry-point target failed to load: {name}") from exc
    if not callable(loaded):
        raise SystemExit(f"installed wheel entry-point target is not callable: {name}")

application = create_application(settings=Settings(database_url="sqlite:///:memory:"))
runner = CliRunner()
hello = runner.invoke(create_cli(application), ["hello"])
counted = runner.invoke(create_cli(application), ["ask", "how many rs in strawberry"])
if hello.exit_code != 0 or hello.stdout.strip() != "Hello, world!":
    raise SystemExit("installed wheel failed the hello smoke")
if counted.exit_code != 0 or counted.stdout.strip() != "3":
    raise SystemExit("installed wheel failed the character-count smoke")
""".strip()


class ManifestError(ValueError):
    """Raised when release-gate data is malformed or unsafe."""


class InputError(ManifestError):
    """Raised for invalid or inaccessible operator inputs."""


class ArtifactError(ManifestError):
    """Raised when a package artifact fails release validation."""


class StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: StrictSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ManifestError("mapping keys must be scalar values") from exc
        if duplicate:
            raise ManifestError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class Gate:
    """Validated release gate used to calculate readiness."""

    gate_id: str
    category: str
    blocking: bool
    state: str


@dataclass(frozen=True, slots=True)
class Manifest:
    """Validated production profile and its release gates."""

    schema_version: str
    profile_id: str
    policy_sha256: str
    manifest_sha256: str
    gates: tuple[Gate, ...]

    def report(self) -> dict[str, Any]:
        counts = Counter(gate.state for gate in self.gates)
        blockers = sorted(
            gate.gate_id for gate in self.gates if gate.blocking and gate.state != "met"
        )
        blocking_count = sum(gate.blocking for gate in self.gates)
        declarations_complete = blocking_count > 0 and not blockers
        return {
            "blocking_gates": blocking_count,
            "blockers": blockers,
            "certifies_production": False,
            "counts": {state: counts.get(state, 0) for state in sorted(STATES)},
            "declarations_complete": declarations_complete,
            "manifest_sha256": self.manifest_sha256,
            "policy_sha256": self.policy_sha256,
            "profile": self.profile_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ArtifactSummary:
    """Identity and digests for one validated source/wheel pair."""

    version: str
    sdist_sha256: str
    wheel_sha256: str


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    """Normalized publishable project metadata used across source and wheel checks."""

    name: str
    version: str
    requires_python: str
    dependencies: tuple[tuple[object, ...], ...]
    extras: tuple[str, ...]
    license_files: tuple[str, ...]
    scripts: tuple[tuple[str, str], ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _requirement_identity(requirement: Requirement) -> tuple[object, ...]:
    return (
        canonicalize_name(requirement.name),
        tuple(
            sorted((specifier.operator, specifier.version) for specifier in requirement.specifier)
        ),
        tuple(sorted(canonicalize_name(extra) for extra in requirement.extras)),
        str(requirement.marker or ""),
        requirement.url or "",
    )


def _parse_requirement(value: object, location: str) -> Requirement:
    if not isinstance(value, str):
        raise ArtifactError(f"{location} must contain requirement strings")
    try:
        return Requirement(value)
    except InvalidRequirement as exc:
        raise ArtifactError(f"{location} contains an invalid requirement") from exc


def _normalize_requirements(values: object, location: str) -> tuple[tuple[object, ...], ...]:
    if not isinstance(values, list):
        raise ArtifactError(f"{location} must be a list of requirement strings")
    normalized: list[tuple[object, ...]] = []
    for value in values:
        requirement = _parse_requirement(value, location)
        normalized.append(_requirement_identity(requirement))
    return tuple(sorted(normalized))


def _normalize_project_requirements(
    requirements: object, optional: object, location: str
) -> tuple[tuple[object, ...], ...]:
    normalized = list(_normalize_requirements(requirements, f"{location} dependencies"))
    if not isinstance(optional, dict):
        raise ArtifactError(f"{location} optional dependencies must be a mapping")
    for extra, extra_requirements in optional.items():
        if not isinstance(extra, str) or not extra or not isinstance(extra_requirements, list):
            raise ArtifactError(f"{location} optional dependencies are invalid")
        try:
            normalized_extra = canonicalize_name(extra, validate=True)
        except InvalidName as exc:
            raise ArtifactError(f"{location} optional dependency name is invalid") from exc
        for value in extra_requirements:
            requirement = _parse_requirement(value, f"{location} optional dependencies")
            marker = f'extra == "{normalized_extra}"'
            if requirement.marker is not None:
                marker = f"({requirement.marker}) and ({marker})"
            requirement.marker = Marker(marker)
            normalized.append(_requirement_identity(requirement))
    return tuple(sorted(normalized))


def _identity_from_project_table(project: object, location: str) -> ProjectIdentity:
    if not isinstance(project, dict):
        raise ArtifactError(f"{location} has no project table")
    name = project.get("name")
    version = project.get("version")
    requires_python = project.get("requires-python")
    if not all(isinstance(value, str) and value for value in (name, version, requires_python)):
        raise ArtifactError(
            f"{location} project name, version, and requires-python must be strings"
        )
    scripts = project.get("scripts")
    if (
        not isinstance(scripts, dict)
        or not scripts
        or not all(
            isinstance(key, str) and key and isinstance(value, str) and value
            for key, value in scripts.items()
        )
    ):
        raise ArtifactError(f"{location} project scripts must be a non-empty mapping")
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise ArtifactError(f"{location} optional dependencies must be a mapping")
    license_files = project.get("license-files")
    if (
        not isinstance(license_files, list)
        or not license_files
        or not all(isinstance(value, str) and value for value in license_files)
    ):
        raise ArtifactError(f"{location} license-files must be a non-empty list")
    for license_file in license_files:
        path = PurePosixPath(license_file)
        if path.is_absolute() or ".." in path.parts or "\\" in license_file:
            raise ArtifactError(f"{location} license-files contains an unsafe path")
    return ProjectIdentity(
        name=name,
        version=version,
        requires_python=requires_python,
        dependencies=_normalize_project_requirements(
            project.get("dependencies"), optional, location
        ),
        extras=tuple(sorted(canonicalize_name(extra, validate=True) for extra in optional)),
        license_files=tuple(sorted(license_files)),
        scripts=tuple(sorted(scripts.items())),
    )


def _project_identity(root: Path) -> ProjectIdentity:
    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ArtifactError("pyproject.toml is unreadable or invalid") from exc
    identity = _identity_from_project_table(pyproject.get("project"), "pyproject.toml")

    try:
        init_text = (root / "src/af/__init__.py").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ArtifactError("src/af/__init__.py is unreadable") from exc
    match = VERSION_PATTERN.search(init_text)
    if match is None:
        raise ArtifactError("src/af/__init__.py has no literal __version__")
    if match.group(1) != identity.version:
        raise ArtifactError("pyproject and runtime versions do not match")
    return identity


def _validate_core_metadata(
    contents: bytes, identity: ProjectIdentity, artifact: str
) -> tuple[str, ...]:
    try:
        metadata = Metadata.from_email(contents, validate=True)
    except Exception as exc:
        raise ArtifactError(f"{artifact} core metadata is invalid") from exc
    if metadata.name != identity.name:
        raise ArtifactError(f"{artifact} project name does not match pyproject.toml")
    if str(metadata.version) != identity.version:
        raise ArtifactError(f"{artifact} version does not match pyproject.toml")
    if str(metadata.requires_python or "") != identity.requires_python:
        raise ArtifactError(f"{artifact} Python requirement does not match pyproject.toml")
    dependencies = [str(requirement) for requirement in metadata.requires_dist or []]
    if _normalize_requirements(dependencies, f"{artifact} dependencies") != identity.dependencies:
        raise ArtifactError(f"{artifact} dependencies do not match pyproject.toml")
    extras = tuple(sorted(str(extra) for extra in metadata.provides_extra or []))
    if extras != identity.extras:
        raise ArtifactError(f"{artifact} extras do not match pyproject.toml")
    license_files = tuple(sorted(metadata.license_files or []))
    if license_files != identity.license_files:
        raise ArtifactError(f"{artifact} license metadata does not match pyproject.toml")
    for license_file in license_files:
        path = PurePosixPath(license_file)
        if path.is_absolute() or ".." in path.parts or "\\" in license_file:
            raise ArtifactError(f"{artifact} license metadata contains an unsafe path")
    return license_files


def _validate_archive_name(name: str, archive: str) -> PurePosixPath:
    if not name or "\\" in name:
        raise ArtifactError(f"{archive} contains an unsafe path")
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise ArtifactError(f"{archive} contains an unsafe path: {name}")
    return path


def _forbidden_archive_path(name: str, *, wheel: bool) -> bool:
    normalized = name.removeprefix("./")
    prefixes = FORBIDDEN_COMMON_PREFIXES + (FORBIDDEN_WHEEL_PREFIXES if wheel else ())
    return (
        normalized.startswith(prefixes)
        or "/__pycache__/" in f"/{normalized}"
        or normalized.endswith(FORBIDDEN_ARCHIVE_SUFFIXES)
    )


def _is_regular_tar_member(member: tarfile.TarInfo) -> bool:
    return member.type in (tarfile.REGTYPE, tarfile.AREGTYPE)


def _is_directory_tar_member(member: tarfile.TarInfo) -> bool:
    return member.type == tarfile.DIRTYPE


def _source_files(root: Path) -> dict[str, Path]:
    source_root = root / "src/af"
    return {
        path.relative_to(source_root).as_posix(): path
        for path in source_root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    }


def _read_zip_member(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_ARCHIVE_FILE_BYTES:
        raise ArtifactError(f"wheel member exceeds the size limit: {name}")
    return archive.read(info)


def _validate_wheel_record(contents: Mapping[str, bytes], record_name: str) -> None:
    if record_name not in contents:
        raise ArtifactError("wheel is missing a valid RECORD")
    try:
        rows = list(csv.reader(io.StringIO(contents[record_name].decode("utf-8"))))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ArtifactError("wheel is missing a valid RECORD") from exc
    recorded: set[str] = set()
    for row in rows:
        if len(row) != 3 or row[0] in recorded:
            raise ArtifactError("wheel is missing a valid RECORD")
        name, encoded_hash, encoded_size = row
        recorded.add(name)
        if name == record_name:
            if encoded_hash or encoded_size:
                raise ArtifactError("wheel RECORD must not hash itself")
            continue
        content = contents.get(name)
        if content is None or not encoded_hash.startswith("sha256="):
            raise ArtifactError("wheel is missing a valid RECORD")
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        if encoded_hash != f"sha256={digest}" or encoded_size != str(len(content)):
            raise ArtifactError(f"wheel RECORD hash or size mismatch: {name}")
    if recorded != set(contents):
        raise ArtifactError("wheel RECORD does not cover every archive member")


def _inspect_wheel(root: Path, wheel: Path, *, identity: ProjectIdentity) -> None:
    expected_filename = (
        f"{canonicalize_name(identity.name).replace('-', '_')}-{identity.version}-py3-none-any.whl"
    )
    if wheel.is_symlink() or not wheel.is_file() or wheel.name != expected_filename:
        raise ArtifactError("wheel filename or platform tag does not match project metadata")
    try:
        with zipfile.ZipFile(wheel) as archive:
            items = archive.infolist()
            if len(items) > MAX_ARCHIVE_MEMBERS:
                raise ArtifactError("wheel contains too many members")
            if sum(item.file_size for item in items) > MAX_ARCHIVE_EXPANDED_BYTES:
                raise ArtifactError("wheel exceeds the expanded-size limit")
            contents: dict[str, bytes] = {}
            member_names: set[str] = set()
            for item in items:
                path = _validate_archive_name(item.filename, "wheel")
                mode = (item.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                if item.is_dir():
                    supported_type = file_type in (0, stat.S_IFDIR)
                else:
                    supported_type = file_type in (0, stat.S_IFREG)
                if not supported_type:
                    raise ArtifactError(f"wheel contains unsupported member: {item.filename}")
                normalized = path.as_posix().rstrip("/")
                if normalized in member_names:
                    raise ArtifactError(f"wheel contains duplicate member: {normalized}")
                member_names.add(normalized)
                if item.is_dir():
                    continue
                if _forbidden_archive_path(normalized, wheel=True):
                    raise ArtifactError(f"wheel contains forbidden path: {normalized}")
                contents[normalized] = _read_zip_member(archive, normalized)

            distribution = (
                f"{canonicalize_name(identity.name).replace('-', '_')}-{identity.version}.dist-info"
            )
            license_prefix = f"{distribution}/licenses/"
            license_members = {name for name in contents if name.startswith(license_prefix)}
            if not license_members:
                raise ArtifactError("wheel contains no license files")
            packaged_licenses = {name.removeprefix(license_prefix) for name in license_members}
            if packaged_licenses != set(identity.license_files):
                raise ArtifactError("wheel license files do not match pyproject.toml")
            for name in license_members:
                relative = name.removeprefix(license_prefix)
                source_license = root / relative
                if (
                    not relative
                    or "/" in relative
                    or source_license.is_symlink()
                    or not source_license.is_file()
                    or contents[name] != source_license.read_bytes()
                ):
                    raise ArtifactError(f"wheel contains unexpected license file: {name}")
            generated = {
                f"{distribution}/METADATA",
                f"{distribution}/WHEEL",
                f"{distribution}/entry_points.txt",
                f"{distribution}/RECORD",
            } | license_members
            source_files = _source_files(root)
            expected_package = {f"af/{name}" for name in source_files}
            expected = expected_package | generated
            unexpected = sorted(set(contents) - expected)
            if unexpected:
                raise ArtifactError(f"wheel contains unexpected path: {unexpected[0]}")
            for required_name in sorted(expected - set(contents)):
                if required_name.endswith("/RECORD"):
                    raise ArtifactError("wheel is missing a valid RECORD")
                raise ArtifactError(f"wheel is missing required file: {required_name}")
            for relative, source_path in source_files.items():
                if contents[f"af/{relative}"] != source_path.read_bytes():
                    raise ArtifactError(f"wheel source differs from the checkout: af/{relative}")

            metadata_name = f"{distribution}/METADATA"
            declared_licenses = set(
                _validate_core_metadata(contents[metadata_name], identity, "wheel")
            )
            if declared_licenses != packaged_licenses:
                raise ArtifactError("wheel license metadata does not match packaged files")

            entry_points = configparser.ConfigParser(interpolation=None)
            entry_points.optionxform = str
            entry_points.read_string(contents[f"{distribution}/entry_points.txt"].decode("utf-8"))
            if entry_points.sections() != ["console_scripts"]:
                raise ArtifactError("wheel entry-point groups do not match pyproject.toml")
            if tuple(sorted(entry_points["console_scripts"].items())) != identity.scripts:
                raise ArtifactError("wheel console entry points do not match pyproject.toml")
            for _, target in identity.scripts:
                module_name = target.partition(":")[0]
                module_path = f"{module_name.replace('.', '/')}.py"
                package_path = f"{module_name.replace('.', '/')}/__init__.py"
                if module_path not in contents and package_path not in contents:
                    raise ArtifactError(f"wheel is missing entry-point target: {module_name}")

            wheel_metadata = Parser().parsestr(contents[f"{distribution}/WHEEL"].decode("utf-8"))
            if wheel_metadata.get("Root-Is-Purelib", "").lower() != "true":
                raise ArtifactError("wheel is not marked as pure Python")
            if wheel_metadata.get_all("Tag", []) != ["py3-none-any"]:
                raise ArtifactError("wheel platform tag is not py3-none-any")
            _validate_wheel_record(contents, f"{distribution}/RECORD")
    except (configparser.Error, OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise ArtifactError("wheel is unreadable or invalid") from exc


def _inspect_sdist(root: Path, sdist: Path, *, identity: ProjectIdentity) -> None:
    distribution = f"{canonicalize_name(identity.name).replace('-', '_')}-{identity.version}"
    if sdist.is_symlink() or not sdist.is_file() or sdist.name != f"{distribution}.tar.gz":
        raise ArtifactError("sdist filename does not match project metadata")
    required_files = {
        *identity.license_files,
        "PKG-INFO",
        "README.md",
        "docs/production-mvp.md",
        "pyproject.toml",
        "release/production-mvp.yaml",
        "scripts/release_gates.py",
        "src/af/__init__.py",
        "uv.lock",
    }
    try:
        with tarfile.open(sdist, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ArtifactError("sdist contains too many members")
            expanded_size = sum(member.size for member in members if _is_regular_tar_member(member))
            if expanded_size > MAX_ARCHIVE_EXPANDED_BYTES:
                raise ArtifactError("sdist exceeds the expanded-size limit")
            roots: set[str] = set()
            files: dict[str, tarfile.TarInfo] = {}
            member_names: set[str] = set()
            for member in members:
                path = _validate_archive_name(member.name, "sdist")
                if not _is_regular_tar_member(member) and not _is_directory_tar_member(member):
                    raise ArtifactError(f"sdist contains unsupported member: {member.name}")
                if not path.parts:
                    continue
                roots.add(path.parts[0])
                normalized = path.as_posix().rstrip("/")
                if normalized in member_names:
                    duplicate = (
                        PurePosixPath(*path.parts[1:]).as_posix()
                        if len(path.parts) > 1
                        else normalized
                    )
                    raise ArtifactError(f"sdist contains duplicate member: {duplicate}")
                member_names.add(normalized)
                if len(path.parts) == 1 and not _is_directory_tar_member(member):
                    raise ArtifactError(f"sdist contains unsupported member: {member.name}")
                if len(path.parts) > 1 and _is_regular_tar_member(member):
                    relative = PurePosixPath(*path.parts[1:]).as_posix()
                    if member.size > MAX_ARCHIVE_FILE_BYTES:
                        raise ArtifactError(f"sdist member exceeds the size limit: {relative}")
                    if relative in files:
                        raise ArtifactError(f"sdist contains duplicate member: {relative}")
                    if _forbidden_archive_path(relative, wheel=False):
                        raise ArtifactError(f"sdist contains forbidden path: {relative}")
                    files[relative] = member
            if roots != {distribution}:
                raise ArtifactError("sdist top-level directory does not match project metadata")

            source_files = _source_files(root)
            required = required_files | {f"src/af/{name}" for name in source_files}
            for required_name in sorted(required - set(files)):
                raise ArtifactError(f"sdist is missing required file: {required_name}")

            def read_member(name: str) -> bytes:
                handle = archive.extractfile(files[name])
                if handle is None:
                    raise ArtifactError(f"sdist member is unreadable: {name}")
                return handle.read()

            archived_project = tomllib.loads(read_member("pyproject.toml").decode("utf-8"))
            if (
                _identity_from_project_table(
                    archived_project.get("project"), "sdist pyproject.toml"
                )
                != identity
            ):
                raise ArtifactError("sdist project metadata does not match the checkout")
            _validate_core_metadata(read_member("PKG-INFO"), identity, "sdist")
            for relative, source_path in source_files.items():
                archive_name = f"src/af/{relative}"
                if read_member(archive_name) != source_path.read_bytes():
                    raise ArtifactError(f"sdist source differs from the checkout: {archive_name}")
            for relative in required_files - {"src/af/__init__.py"}:
                if relative == "PKG-INFO":
                    continue
                if read_member(relative) != (root / relative).read_bytes():
                    raise ArtifactError(f"sdist release file differs from the checkout: {relative}")
            for relative in sorted(set(files) - {"PKG-INFO"}):
                source_path = root / relative
                if source_path.is_symlink() or not source_path.is_file():
                    raise ArtifactError(f"sdist contains unexpected path: {relative}")
                if read_member(relative) != source_path.read_bytes():
                    raise ArtifactError(f"sdist file differs from the checkout: {relative}")
    except (OSError, UnicodeDecodeError, tarfile.TarError, tomllib.TOMLDecodeError) as exc:
        raise ArtifactError("sdist is unreadable or invalid") from exc


def inspect_distribution(root: Path, sdist: Path, wheel: Path) -> ArtifactSummary:
    """Validate source/wheel identity, safe contents, and required runtime files."""
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InputError("repository root is inaccessible") from exc
    identity = _project_identity(root)
    _inspect_sdist(root, sdist, identity=identity)
    _inspect_wheel(root, wheel, identity=identity)
    return ArtifactSummary(
        version=identity.version,
        sdist_sha256=_sha256(sdist),
        wheel_sha256=_sha256(wheel),
    )


def _run_release_command(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    stage: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            cwd=cwd,
            env=dict(environment),
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArtifactError(f"package {stage} could not complete") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        suffix = f": {detail[-2000:]}" if detail else ""
        raise ArtifactError(f"package {stage} failed with exit {completed.returncode}{suffix}")
    return completed


def _ensure_clean_checkout(root: Path, environment: Mapping[str, str], git_executable: str) -> None:
    try:
        completed = subprocess.run(
            (git_executable, "status", "--porcelain", "--untracked-files=all"),
            capture_output=True,
            check=False,
            cwd=root,
            env=dict(environment),
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InputError("git is unavailable for the clean-checkout proof") from exc
    if completed.returncode:
        raise InputError("git could not verify the release checkout")
    if completed.stdout:
        raise ArtifactError("package build requires a clean tracked and untracked checkout")


def _release_commit(root: Path, environment: Mapping[str, str], git_executable: str) -> str:
    try:
        completed = subprocess.run(
            (git_executable, "rev-parse", "--verify", "HEAD"),
            capture_output=True,
            check=False,
            cwd=root,
            env=dict(environment),
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InputError("git could not identify the release commit") from exc
    commit = completed.stdout.strip()
    if completed.returncode or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise InputError("git could not identify the release commit")
    return commit


def _extract_source_snapshot(archive_path: Path, destination: Path) -> None:
    destination.mkdir()
    try:
        with tarfile.open(archive_path, "r:") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ArtifactError("source snapshot contains too many members")
            expanded_size = sum(member.size for member in members if _is_regular_tar_member(member))
            if expanded_size > MAX_ARCHIVE_EXPANDED_BYTES:
                raise ArtifactError("source snapshot exceeds the expanded-size limit")
            seen: set[str] = set()
            for member in members:
                path = _validate_archive_name(member.name, "source snapshot")
                normalized = path.as_posix().rstrip("/")
                if normalized in seen:
                    raise ArtifactError(f"source snapshot contains duplicate member: {normalized}")
                seen.add(normalized)
                target = destination.joinpath(*path.parts)
                if _is_directory_tar_member(member):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not _is_regular_tar_member(member) or member.size > MAX_ARCHIVE_FILE_BYTES:
                    raise ArtifactError(
                        f"source snapshot contains unsupported member: {member.name}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = archive.extractfile(member)
                if handle is None:
                    raise ArtifactError(f"source snapshot member is unreadable: {member.name}")
                target.write_bytes(handle.read())
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (OSError, tarfile.TarError) as exc:
        raise ArtifactError("source snapshot is unreadable or invalid") from exc


def _release_environment(temporary_root: Path) -> dict[str, str]:
    environment: dict[str, str] = {}
    for name in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    home = temporary_root / "home"
    cache = temporary_root / "uv-cache"
    home.mkdir()
    cache.mkdir()
    environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(temporary_root),
            "UV_CACHE_DIR": str(cache),
            "UV_NO_CONFIG": "1",
            "UV_NO_PROGRESS": "1",
            "UV_OFFLINE": "1",
            "UV_PYTHON": sys.executable,
            "PYTHONNOUSERSITE": "1",
        }
    )
    return environment


def build_and_validate_distribution(
    root: Path, uv_command: str = "uv", git_command: str = "git"
) -> ArtifactSummary:
    """Build wheel-from-sdist, install it temporarily, and run deterministic smoke checks."""
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InputError("repository root is inaccessible") from exc
    uv_executable = shutil.which(uv_command)
    if uv_executable is None:
        raise InputError("uv executable is unavailable")
    try:
        uv_executable = str(Path(uv_executable).resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        raise InputError("uv executable is inaccessible") from exc
    git_executable = shutil.which(git_command)
    if git_executable is None:
        raise InputError("git executable is unavailable")
    try:
        git_executable = str(Path(git_executable).resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        raise InputError("git executable is inaccessible") from exc
    with tempfile.TemporaryDirectory(prefix="af-release-") as temporary:
        temporary_root = Path(temporary)
        artifacts = temporary_root / "artifacts"
        build_root = temporary_root / "source"
        reference_root = temporary_root / "reference"
        source_archive = temporary_root / "source.tar"
        target = temporary_root / "installed"
        artifacts.mkdir()
        target.mkdir()
        environment = _release_environment(temporary_root)
        _ensure_clean_checkout(root, environment, git_executable)
        commit = _release_commit(root, environment, git_executable)
        _run_release_command(
            (git_executable, "archive", "--format=tar", f"--output={source_archive}", commit),
            cwd=root,
            environment=environment,
            stage="commit snapshot",
            timeout=60,
        )
        source_archive_sha256 = _sha256(source_archive)
        _extract_source_snapshot(source_archive, build_root)

        common = (
            uv_executable,
            "build",
            *UV_BUILD_OPTIONS,
            "--out-dir",
            str(artifacts),
        )
        _run_release_command(
            (*common, "--sdist", str(build_root)),
            cwd=temporary_root,
            environment=environment,
            stage="sdist build",
            timeout=300,
        )
        sdists = sorted(artifacts.glob("*.tar.gz"))
        if len(sdists) != 1:
            raise ArtifactError("package build did not produce exactly one sdist")
        if _sha256(source_archive) != source_archive_sha256:
            raise ArtifactError("commit snapshot changed while the sdist was being built")
        if _release_commit(root, environment, git_executable) != commit:
            raise ArtifactError("release checkout HEAD changed during the package build")
        _ensure_clean_checkout(root, environment, git_executable)
        _extract_source_snapshot(source_archive, reference_root)
        identity = _project_identity(reference_root)
        _inspect_sdist(reference_root, sdists[0], identity=identity)
        sdist_sha256 = _sha256(sdists[0])
        _run_release_command(
            (*common, "--wheel", str(sdists[0])),
            cwd=temporary_root,
            environment=environment,
            stage="wheel-from-sdist build",
            timeout=300,
        )
        wheels = sorted(artifacts.glob("*.whl"))
        if len(wheels) != 1:
            raise ArtifactError("package build did not produce exactly one wheel")
        if _sha256(sdists[0]) != sdist_sha256:
            raise ArtifactError("sdist changed while its wheel was being built")
        if _sha256(source_archive) != source_archive_sha256:
            raise ArtifactError("commit snapshot changed while the wheel was being built")
        if _release_commit(root, environment, git_executable) != commit:
            raise ArtifactError("release checkout HEAD changed during the package build")
        _ensure_clean_checkout(root, environment, git_executable)
        summary = inspect_distribution(reference_root, sdists[0], wheels[0])
        wheel_sha256 = summary.wheel_sha256

        _run_release_command(
            (
                uv_executable,
                "pip",
                "install",
                "--offline",
                "--python",
                sys.executable,
                "--target",
                str(target),
                "--no-deps",
                str(wheels[0]),
            ),
            cwd=temporary_root,
            environment=environment,
            stage="temporary wheel installation",
            timeout=300,
        )
        if _sha256(wheels[0]) != wheel_sha256:
            raise ArtifactError("wheel changed while it was being installed")
        _run_release_command(
            (sys.executable, "-I", "-c", PACKAGE_SMOKE, str(target)),
            cwd=temporary_root,
            environment=environment,
            stage="installed-wheel smoke",
            timeout=60,
        )
        if _release_commit(root, environment, git_executable) != commit:
            raise ArtifactError("release checkout HEAD changed during the package smoke")
        _ensure_clean_checkout(root, environment, git_executable)
        return summary


def _render_package_plan(uv_command: str) -> str:
    first = shlex.join(
        (
            uv_command,
            "build",
            *UV_BUILD_OPTIONS,
            "--out-dir",
            "<temporary-artifacts>",
            "--sdist",
            "<clean-commit-snapshot>",
        )
    )
    return "\n".join(
        (
            "create a clean commit-derived source snapshot with git archive",
            first,
            "build the wheel from that sdist in a temporary directory",
            "inspect artifact identity, entry points, templates, typing, and forbidden state",
            "install the wheel with uv pip install --offline --no-deps into a temporary target",
            "run deterministic smoke checks outside the source checkout",
            "Dry run only. Re-run with --execute to build and smoke the package.",
        )
    )


def _as_mapping(value: object, location: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ManifestError(f"{location} must be a string-keyed mapping")
    return value


def _as_list(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        raise ManifestError(f"{location} must be a list")
    return value


def _as_nonempty_string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{location} must be a non-empty string")
    return value


def _as_string_list(value: object, location: str, *, allow_empty: bool = False) -> list[str]:
    items = _as_list(value, location)
    if not allow_empty and not items:
        raise ManifestError(f"{location} must not be empty")
    return [_as_nonempty_string(item, f"{location}[{index}]") for index, item in enumerate(items)]


def _reject_unknown_fields(
    value: Mapping[str, object], allowed: frozenset[str], location: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        noun = "field" if len(unknown) == 1 else "fields"
        raise ManifestError(f"{location} has unknown {noun}: {', '.join(unknown)}")


def _require_fields(value: Mapping[str, object], required: frozenset[str], location: str) -> None:
    missing = sorted(required - set(value))
    if missing:
        noun = "field" if len(missing) == 1 else "fields"
        raise ManifestError(f"{location} is missing required {noun}: {', '.join(missing)}")


def _read_yaml(path: Path) -> tuple[object, bytes]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ManifestError("manifest is not a readable regular file") from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ManifestError("manifest is not a readable regular file")
            contents = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(contents) > MAX_MANIFEST_BYTES:
            raise ManifestError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
        text = contents.decode("utf-8")
        depth = 0
        for index, token in enumerate(yaml.scan(text), start=1):
            if index > MAX_YAML_TOKENS:
                raise ManifestError("YAML token count exceeds the limit")
            if isinstance(token, (AliasToken, AnchorToken)):
                raise ManifestError("YAML aliases and anchors are not allowed")
            if isinstance(
                token,
                (
                    BlockMappingStartToken,
                    BlockSequenceStartToken,
                    FlowMappingStartToken,
                    FlowSequenceStartToken,
                ),
            ):
                depth += 1
                if depth > MAX_YAML_DEPTH:
                    raise ManifestError("YAML nesting exceeds the limit")
            elif isinstance(
                token,
                (BlockEndToken, FlowMappingEndToken, FlowSequenceEndToken),
            ):
                depth = max(0, depth - 1)
            elif isinstance(token, ScalarToken) and len(token.value) > MAX_YAML_SCALAR_LENGTH:
                raise ManifestError("YAML scalar length exceeds the limit")
        return yaml.load(text, Loader=StrictSafeLoader), contents
    except UnicodeDecodeError as exc:
        raise ManifestError("manifest must be UTF-8") from exc
    except yaml.YAMLError as exc:
        raise ManifestError("manifest contains invalid YAML") from exc
    except RecursionError as exc:
        raise ManifestError("YAML nesting exceeds the limit") from exc


def _load_yaml(path: Path) -> object:
    document, _ = _read_yaml(path)
    return document


def _resolve_inside_root(root: Path, reference: str, location: str) -> Path:
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise ManifestError(f"{location} must resolve within the repository root")
    candidate = root.joinpath(relative)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ManifestError(f"{location} must resolve within the repository root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ManifestError(f"{location} must not reference a symlink")
    if not resolved.is_file():
        raise ManifestError(f"{location} must reference a regular file")
    return resolved


def _registered_script_ids(root: Path) -> frozenset[str]:
    registry_path = _resolve_inside_root(root, "scripts/registry.yaml", "scripts registry")
    registry = _as_mapping(_load_yaml(registry_path), "scripts registry")
    scripts = _as_list(registry.get("scripts"), "scripts registry.scripts")
    identifiers: set[str] = set()
    seen: set[str] = set()
    for index, item in enumerate(scripts):
        script = _as_mapping(item, f"scripts registry.scripts[{index}]")
        identifier = _as_nonempty_string(script.get("id"), f"scripts registry.scripts[{index}].id")
        if identifier in seen:
            raise ManifestError(f"scripts registry has duplicate script ID: {identifier}")
        seen.add(identifier)
        if script.get("status") == "active":
            identifiers.add(identifier)
    return frozenset(identifiers)


def _validate_profile(value: object) -> str:
    profile = _as_mapping(value, "profile")
    _reject_unknown_fields(profile, PROFILE_FIELDS, "profile")
    _require_fields(profile, PROFILE_FIELDS, "profile")

    profile_id = _as_nonempty_string(profile["id"], "profile.id")
    _as_nonempty_string(profile["title"], "profile.title")
    _as_nonempty_string(profile["release_target"], "profile.release_target")
    _as_nonempty_string(profile["maturity"], "profile.maturity")

    constraints = _as_mapping(profile["constraints"], "profile.constraints")
    _reject_unknown_fields(constraints, CONSTRAINT_FIELDS, "profile.constraints")
    _require_fields(constraints, CONSTRAINT_FIELDS, "profile.constraints")
    for key in (
        "operating_system",
        "python",
        "database",
        "routes",
        "network_exposure",
    ):
        _as_nonempty_string(constraints[key], f"profile.constraints.{key}")
    for key in ("nodes", "tenants", "databases", "live_processes"):
        number = constraints[key]
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ManifestError(f"profile.constraints.{key} must be a positive integer")
    if not isinstance(constraints["remote_inference"], bool):
        raise ManifestError("profile.constraints.remote_inference must be a boolean")

    supported_surfaces = _as_string_list(
        profile["supported_surfaces"], "profile.supported_surfaces"
    )
    _as_string_list(
        profile["optional_extensions"],
        "profile.optional_extensions",
        allow_empty=True,
    )
    _as_string_list(profile["excluded"], "profile.excluded")
    network_surfaces = {
        "http-html-rest-graphql-openapi",
        "mcp-streamable-http",
        "secure-mcp-tunnel",
    }
    if constraints["network_exposure"] == "none" and network_surfaces.intersection(
        supported_surfaces
    ):
        raise ManifestError("profile with no network exposure cannot support network surfaces")
    return profile_id


def _validate_evidence(
    value: object,
    *,
    root: Path,
    location: str,
    script_ids: frozenset[str],
) -> None:
    evidence = _as_mapping(value, location)
    _reject_unknown_fields(evidence, EVIDENCE_FIELDS, location)
    _require_fields(evidence, EVIDENCE_FIELDS, location)
    kind = _as_nonempty_string(evidence["kind"], f"{location}.kind")
    reference = _as_nonempty_string(evidence["ref"], f"{location}.ref")
    _as_nonempty_string(evidence["description"], f"{location}.description")
    if kind not in EVIDENCE_KINDS:
        raise ManifestError(f"{location}.kind must be one of: {', '.join(sorted(EVIDENCE_KINDS))}")
    if kind == "file":
        _resolve_inside_root(root, reference, f"{location}.ref")
    elif kind == "script" and reference not in script_ids:
        raise ManifestError(f"{location}.ref names an unregistered script")


def _validate_gate(
    value: object,
    *,
    index: int,
    root: Path,
    script_ids: frozenset[str],
) -> Gate:
    location = f"gates[{index}]"
    gate = _as_mapping(value, location)
    _reject_unknown_fields(gate, GATE_FIELDS, location)
    required = GATE_FIELDS - {"next_action"}
    _require_fields(gate, required, location)

    gate_id = _as_nonempty_string(gate["id"], f"{location}.id")
    if not GATE_ID_PATTERN.fullmatch(gate_id):
        raise ManifestError(f"{location}.id must match <category>.<kebab-id>")
    category = _as_nonempty_string(gate["category"], f"{location}.category")
    if category not in CATEGORIES:
        raise ManifestError(f"{location}.category must be one of: {', '.join(sorted(CATEGORIES))}")
    if not gate_id.startswith(f"{category}."):
        raise ManifestError(f"{location}.id prefix must match its category")

    _as_nonempty_string(gate["title"], f"{location}.title")
    _as_nonempty_string(gate["requirement"], f"{location}.requirement")
    blocking = gate["blocking"]
    if not isinstance(blocking, bool):
        raise ManifestError(f"{location}.blocking must be a boolean")
    verification = _as_nonempty_string(gate["verification"], f"{location}.verification")
    if verification not in VERIFICATION_KINDS:
        raise ManifestError(
            f"{location}.verification must be one of: {', '.join(sorted(VERIFICATION_KINDS))}"
        )
    state = _as_nonempty_string(gate["state"], f"{location}.state")
    if state not in STATES:
        raise ManifestError(f"{location}.state must be one of: {', '.join(sorted(STATES))}")

    evidence = _as_list(gate["evidence"], f"{location}.evidence")
    if state == "met" and not evidence:
        raise ManifestError(f"{location}: met gates require evidence")
    for evidence_index, item in enumerate(evidence):
        _validate_evidence(
            item,
            root=root,
            location=f"{location}.evidence[{evidence_index}]",
            script_ids=script_ids,
        )

    if state != "met":
        _as_nonempty_string(gate.get("next_action"), f"{location}.next_action")
    elif "next_action" in gate:
        _as_nonempty_string(gate["next_action"], f"{location}.next_action")

    issues = _as_string_list(gate["issues"], f"{location}.issues", allow_empty=True)
    for issue in issues:
        if not issue.startswith("https://github.com/"):
            raise ManifestError(f"{location}.issues must contain GitHub HTTPS URLs")

    return Gate(gate_id=gate_id, category=category, blocking=blocking, state=state)


def _policy_digest(document: Mapping[str, object]) -> str:
    profile = _as_mapping(document["profile"], "profile")
    gate_values = _as_list(document["gates"], "gates")
    gate_policy: list[dict[str, object]] = []
    policy_fields = ("id", "category", "title", "requirement", "blocking", "verification")
    for index, value in enumerate(gate_values):
        gate = _as_mapping(value, f"gates[{index}]")
        gate_policy.append({field: gate[field] for field in policy_fields})
    gate_policy.sort(key=lambda gate: str(gate["id"]))
    policy = {
        "schema_version": document["schema_version"],
        "profile": {field: profile[field] for field in sorted(PROFILE_FIELDS)},
        "gates": gate_policy,
    }
    encoded = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_manifest(path: Path, root: Path) -> Manifest:
    """Load and strictly validate one production-MVP manifest."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ManifestError("repository root must be a directory")
    candidate = path if path.is_absolute() else root / path
    try:
        absolute = Path(os.path.abspath(candidate))
        relative = absolute.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ManifestError("manifest must not reference a symlink")
        manifest_path = absolute.resolve(strict=True)
        manifest_path.relative_to(root)
    except ManifestError:
        raise
    except (OSError, ValueError) as exc:
        raise ManifestError("manifest must resolve within the repository root") from exc

    loaded, manifest_bytes = _read_yaml(manifest_path)
    document = _as_mapping(loaded, "root")
    _reject_unknown_fields(document, ROOT_FIELDS, "root")
    _require_fields(document, ROOT_FIELDS, "root")
    schema_version = _as_nonempty_string(document["schema_version"], "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ManifestError(f"unsupported schema_version: {schema_version}")
    profile_id = _validate_profile(document["profile"])

    script_ids = _registered_script_ids(root)
    gate_values = _as_list(document["gates"], "gates")
    if not gate_values:
        raise ManifestError("gates must not be empty")
    gates = tuple(
        _validate_gate(item, index=index, root=root, script_ids=script_ids)
        for index, item in enumerate(gate_values)
    )
    gate_ids = [gate.gate_id for gate in gates]
    duplicates = sorted(gate_id for gate_id, count in Counter(gate_ids).items() if count > 1)
    if duplicates:
        raise ManifestError(f"duplicate gate IDs: {', '.join(duplicates)}")
    policy_sha256 = _policy_digest(document)
    if policy_sha256 != EXPECTED_POLICY_SHA256:
        raise ManifestError(
            f"release policy does not match the canonical policy digest (actual {policy_sha256})"
        )
    return Manifest(
        schema_version=schema_version,
        profile_id=profile_id,
        policy_sha256=policy_sha256,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        gates=gates,
    )


def _render_text(report: Mapping[str, object]) -> str:
    status = (
        "DECLARATIONS COMPLETE" if report["declarations_complete"] else "DECLARATIONS INCOMPLETE"
    )
    lines = [
        f"Production MVP {report['profile']}: {status}",
        "Production certification: not performed (certifies_production: false)",
        f"Manifest SHA-256: {report['manifest_sha256']}",
        f"Policy SHA-256: {report['policy_sha256']}",
        f"Blocking gates: {report['blocking_gates']}",
    ]
    blockers = report["blockers"]
    if isinstance(blockers, Sequence) and blockers:
        lines.append("Unmet or pending blockers:")
        lines.extend(f"- {blocker}" for blocker in blockers)
    return "\n".join(lines)


def _resolve_manifest_argument(root: Path, argument: Path) -> Path:
    return argument if argument.is_absolute() else root / argument


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--action",
        choices=("validate", "report", "check", "package"),
        required=True,
        help="Validate policy, report status, check completeness, or validate a package.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"Manifest path (default: {DEFAULT_MANIFEST}).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root used to resolve evidence references.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Render deterministic JSON for report/check actions.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Build and smoke package artifacts; package actions are dry-run by default.",
    )
    parser.add_argument(
        "--uv",
        default="uv",
        help="uv executable used by the package action.",
    )
    parser.add_argument(
        "--git",
        default="git",
        help="git executable used by the package action.",
    )
    args = parser.parse_args()

    if args.action == "package":
        try:
            if not args.execute:
                print(_render_package_plan(args.uv))
                return 0
            summary = build_and_validate_distribution(args.root, args.uv, args.git)
        except InputError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except ArtifactError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except ManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"error: package action could not access its inputs: {exc}", file=sys.stderr)
            return 2
        package_report = asdict(summary)
        if args.json:
            print(json.dumps(package_report, sort_keys=True, separators=(",", ":")))
        else:
            print(f"Validated AF package {summary.version}")
            print(f"sdist sha256: {summary.sdist_sha256}")
            print(f"wheel sha256: {summary.wheel_sha256}")
        return 0

    try:
        root = args.root.resolve(strict=True)
        manifest = load_manifest(
            _resolve_manifest_argument(root, args.manifest),
            root,
        )
    except (ManifestError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = manifest.report()
    if args.action == "validate":
        print(
            f"Valid production MVP declaration manifest: {manifest.profile_id}; "
            "production certification not performed"
        )
        print(f"Manifest SHA-256: {manifest.manifest_sha256}")
        print(f"Policy SHA-256: {manifest.policy_sha256}")
        return 0
    if args.json:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    else:
        print(_render_text(report))
    if args.action == "check" and not report["declarations_complete"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
