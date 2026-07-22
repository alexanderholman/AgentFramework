#!/usr/bin/env python3
"""Validate and report AF's private production-MVP release gates."""

from __future__ import annotations

import argparse
import configparser
import hashlib
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
from yaml.tokens import AliasToken, AnchorToken

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path("release/production-mvp.yaml")
MAX_MANIFEST_BYTES = 256 * 1024
SCHEMA_VERSION = "1.0.0"

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
        "active_processes_per_database",
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
PACKAGE_SMOKE = """
from importlib.resources import files
from pathlib import Path
import sys

target = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(target))

import af
from af.bootstrap import create_application
from af.config import Settings

module_path = Path(af.__file__).resolve()
if target not in module_path.parents:
    raise SystemExit("wheel smoke imported AF outside the temporary target")
if not files("af").joinpath("views/templates/base.html").is_file():
    raise SystemExit("installed wheel has no base HTML template")

application = create_application(settings=Settings(database_url="sqlite:///:memory:"))
if application.tasks.execute("hello").output != "Hello, world!":
    raise SystemExit("installed wheel failed the hello smoke")
if application.tasks.execute("how many rs in strawberry").output != "3":
    raise SystemExit("installed wheel failed the character-count smoke")
""".strip()


class ManifestError(ValueError):
    """Raised when release-gate data is malformed or unsafe."""


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
    gates: tuple[Gate, ...]

    def report(self) -> dict[str, Any]:
        counts = Counter(gate.state for gate in self.gates)
        blockers = sorted(
            gate.gate_id for gate in self.gates if gate.blocking and gate.state != "met"
        )
        blocking_count = sum(gate.blocking for gate in self.gates)
        return {
            "blocking_gates": blocking_count,
            "blockers": blockers,
            "counts": {state: counts.get(state, 0) for state in sorted(STATES)},
            "profile": self.profile_id,
            "ready": blocking_count > 0 and not blockers,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ArtifactSummary:
    """Identity and digests for one validated source/wheel pair."""

    version: str
    sdist_sha256: str
    wheel_sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_identity(root: Path) -> tuple[str, str, str]:
    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError("pyproject.toml is unreadable or invalid") from exc
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise ManifestError("pyproject.toml has no project table")
    name = project.get("name")
    version = project.get("version")
    requires_python = project.get("requires-python")
    if not all(isinstance(value, str) and value for value in (name, version, requires_python)):
        raise ManifestError("project name, version, and requires-python must be strings")

    try:
        init_text = (root / "src/af/__init__.py").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError("src/af/__init__.py is unreadable") from exc
    match = VERSION_PATTERN.search(init_text)
    if match is None:
        raise ManifestError("src/af/__init__.py has no literal __version__")
    if match.group(1) != version:
        raise ManifestError("pyproject and runtime versions do not match")
    return name, version, requires_python


def _validate_archive_name(name: str, archive: str) -> PurePosixPath:
    if not name or "\\" in name:
        raise ManifestError(f"{archive} contains an unsafe path")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ManifestError(f"{archive} contains an unsafe path: {name}")
    return path


def _forbidden_archive_path(name: str, *, wheel: bool) -> bool:
    normalized = name.removeprefix("./")
    prefixes = FORBIDDEN_COMMON_PREFIXES + (FORBIDDEN_WHEEL_PREFIXES if wheel else ())
    return (
        normalized.startswith(prefixes)
        or "/__pycache__/" in f"/{normalized}"
        or normalized.endswith(FORBIDDEN_ARCHIVE_SUFFIXES)
    )


def _inspect_wheel(
    root: Path,
    wheel: Path,
    *,
    project_name: str,
    version: str,
    requires_python: str,
) -> None:
    if wheel.is_symlink() or not wheel.is_file() or not wheel.name.endswith("-py3-none-any.whl"):
        raise ManifestError("wheel must be one regular py3-none-any artifact")
    try:
        with zipfile.ZipFile(wheel) as archive:
            names: set[str] = set()
            for item in archive.infolist():
                path = _validate_archive_name(item.filename, "wheel")
                mode = (item.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
                    raise ManifestError(f"wheel contains unsupported member: {item.filename}")
                if item.is_dir():
                    continue
                normalized = path.as_posix()
                if normalized in names:
                    raise ManifestError(f"wheel contains duplicate member: {normalized}")
                if _forbidden_archive_path(normalized, wheel=True):
                    raise ManifestError(f"wheel contains forbidden path: {normalized}")
                names.add(normalized)

            source_templates = root / "src/af/views/templates"
            template_names = {
                f"af/views/templates/{path.relative_to(source_templates).as_posix()}"
                for path in source_templates.rglob("*.html")
                if path.is_file() and not path.is_symlink()
            }
            required = {"af/__init__.py", "af/py.typed", *template_names}
            for required_name in sorted(required - names):
                raise ManifestError(f"wheel is missing required file: {required_name}")

            metadata_names = sorted(name for name in names if name.endswith(".dist-info/METADATA"))
            entry_point_names = sorted(
                name for name in names if name.endswith(".dist-info/entry_points.txt")
            )
            if len(metadata_names) != 1 or len(entry_point_names) != 1:
                raise ManifestError("wheel must contain one metadata and entry-points file")
            metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
            if metadata.get("Name") != project_name:
                raise ManifestError("wheel project name does not match pyproject.toml")
            if metadata.get("Version") != version:
                raise ManifestError("wheel version does not match pyproject.toml")
            if metadata.get("Requires-Python") != requires_python:
                raise ManifestError("wheel Python requirement does not match pyproject.toml")

            entry_points = configparser.ConfigParser(interpolation=None)
            entry_points.optionxform = str
            entry_points.read_string(archive.read(entry_point_names[0]).decode("utf-8"))
            expected_entries = {
                "af": "af.interfaces.cli:app",
                "af-mcp": "af.interfaces.mcp:main",
            }
            if "console_scripts" not in entry_points:
                raise ManifestError("wheel is missing console entry points")
            actual_entries = dict(entry_points["console_scripts"])
            if actual_entries != expected_entries:
                raise ManifestError("wheel console entry points do not match pyproject.toml")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise ManifestError("wheel is unreadable or invalid") from exc


def _inspect_sdist(root: Path, sdist: Path, *, version: str) -> None:
    if sdist.is_symlink() or not sdist.is_file() or not sdist.name.endswith(".tar.gz"):
        raise ManifestError("sdist must be one regular tar.gz artifact")
    required = {
        "LICENSE",
        "README.md",
        "docs/production-mvp.md",
        "pyproject.toml",
        "release/production-mvp.yaml",
        "scripts/release_gates.py",
        "src/af/__init__.py",
    }
    try:
        with tarfile.open(sdist, "r:gz") as archive:
            members = archive.getmembers()
            roots: set[str] = set()
            relative_names: set[str] = set()
            for member in members:
                path = _validate_archive_name(member.name, "sdist")
                if member.issym() or member.islnk() or member.isdev():
                    raise ManifestError(f"sdist contains unsupported member: {member.name}")
                if not path.parts:
                    continue
                roots.add(path.parts[0])
                if len(path.parts) > 1 and member.isfile():
                    relative = PurePosixPath(*path.parts[1:]).as_posix()
                    if relative in relative_names:
                        raise ManifestError(f"sdist contains duplicate member: {relative}")
                    if _forbidden_archive_path(relative, wheel=False):
                        raise ManifestError(f"sdist contains forbidden path: {relative}")
                    relative_names.add(relative)
            if len(roots) != 1:
                raise ManifestError("sdist must contain one top-level project directory")
            for required_name in sorted(required - relative_names):
                raise ManifestError(f"sdist is missing required file: {required_name}")

            root_name = next(iter(roots))
            pyproject_member = archive.extractfile(f"{root_name}/pyproject.toml")
            init_member = archive.extractfile(f"{root_name}/src/af/__init__.py")
            if pyproject_member is None or init_member is None:
                raise ManifestError("sdist identity files are unreadable")
            archived_project = tomllib.loads(pyproject_member.read().decode("utf-8"))
            archived_project_table = archived_project.get("project")
            if not isinstance(archived_project_table, dict):
                raise ManifestError("sdist pyproject has no project table")
            if archived_project_table.get("version") != version:
                raise ManifestError("sdist version does not match the checkout")
            init_match = VERSION_PATTERN.search(init_member.read().decode("utf-8"))
            if init_match is None or init_match.group(1) != version:
                raise ManifestError("sdist runtime version does not match the checkout")
    except (OSError, UnicodeDecodeError, tarfile.TarError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError("sdist is unreadable or invalid") from exc


def inspect_distribution(root: Path, sdist: Path, wheel: Path) -> ArtifactSummary:
    """Validate source/wheel identity, safe contents, and required runtime files."""
    root = root.resolve(strict=True)
    project_name, version, requires_python = _project_identity(root)
    _inspect_sdist(root, sdist, version=version)
    _inspect_wheel(
        root,
        wheel,
        project_name=project_name,
        version=version,
        requires_python=requires_python,
    )
    return ArtifactSummary(
        version=version,
        sdist_sha256=_sha256(sdist),
        wheel_sha256=_sha256(wheel),
    )


def inspect_artifact_directory(root: Path, artifacts_dir: Path) -> ArtifactSummary:
    """Select and validate exactly one sdist and one wheel from a directory."""
    try:
        resolved = artifacts_dir.resolve(strict=True)
    except OSError as exc:
        raise ManifestError("artifacts directory is not readable") from exc
    if artifacts_dir.is_symlink() or not resolved.is_dir():
        raise ManifestError("artifacts directory is not readable")
    sdists = sorted(resolved.glob("*.tar.gz"))
    wheels = sorted(resolved.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        raise ManifestError("artifacts directory must contain one sdist and one wheel")
    return inspect_distribution(root, sdists[0], wheels[0])


def _run_release_command(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    stage: str,
    timeout: int,
) -> None:
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
        raise ManifestError(f"package {stage} could not complete") from exc
    if completed.returncode:
        raise ManifestError(f"package {stage} failed")


def build_and_validate_distribution(root: Path, uv_command: str = "uv") -> ArtifactSummary:
    """Build wheel-from-sdist, install it temporarily, and run deterministic smoke checks."""
    root = root.resolve(strict=True)
    uv_executable = shutil.which(uv_command)
    if uv_executable is None:
        raise ManifestError("uv executable is unavailable")
    uv_executable = str(Path(uv_executable).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="af-release-") as temporary:
        temporary_root = Path(temporary)
        artifacts = temporary_root / "artifacts"
        target = temporary_root / "installed"
        cache = temporary_root / "uv-cache"
        artifacts.mkdir()
        target.mkdir()
        environment = os.environ.copy()
        environment["UV_CACHE_DIR"] = str(cache)
        environment["UV_PYTHON"] = f"{sys.version_info.major}.{sys.version_info.minor}"
        environment.pop("PYTHONPATH", None)

        common = (
            uv_executable,
            "build",
            "--no-sources",
            "--no-create-gitignore",
            "--out-dir",
            str(artifacts),
        )
        _run_release_command(
            (*common, "--sdist", str(root)),
            cwd=temporary_root,
            environment=environment,
            stage="sdist build",
            timeout=300,
        )
        sdists = sorted(artifacts.glob("*.tar.gz"))
        if len(sdists) != 1:
            raise ManifestError("package build did not produce exactly one sdist")
        _run_release_command(
            (*common, "--wheel", str(sdists[0])),
            cwd=temporary_root,
            environment=environment,
            stage="wheel-from-sdist build",
            timeout=300,
        )
        wheels = sorted(artifacts.glob("*.whl"))
        if len(wheels) != 1:
            raise ManifestError("package build did not produce exactly one wheel")
        summary = inspect_distribution(root, sdists[0], wheels[0])

        _run_release_command(
            (
                uv_executable,
                "pip",
                "install",
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
        _run_release_command(
            (sys.executable, "-I", "-c", PACKAGE_SMOKE, str(target)),
            cwd=temporary_root,
            environment=environment,
            stage="installed-wheel smoke",
            timeout=60,
        )
        return summary


def _render_package_plan(uv_command: str) -> str:
    first = shlex.join((uv_command, "build", "--no-sources", "--sdist", "<checkout>"))
    return "\n".join(
        (
            first,
            "build the wheel from that sdist in a temporary directory",
            "inspect artifact identity, entry points, templates, typing, and forbidden state",
            "install the wheel into an isolated temporary target",
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


def _load_yaml(path: Path) -> object:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ManifestError("manifest is not a readable regular file") from exc
    if not path.is_file() or path.is_symlink():
        raise ManifestError("manifest is not a readable regular file")
    if size > MAX_MANIFEST_BYTES:
        raise ManifestError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    try:
        text = path.read_text(encoding="utf-8")
        for token in yaml.scan(text):
            if isinstance(token, (AliasToken, AnchorToken)):
                raise ManifestError("YAML aliases and anchors are not allowed")
        return yaml.load(text, Loader=StrictSafeLoader)
    except UnicodeDecodeError as exc:
        raise ManifestError("manifest must be UTF-8") from exc
    except yaml.YAMLError as exc:
        raise ManifestError("manifest contains invalid YAML") from exc


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
    for index, item in enumerate(scripts):
        script = _as_mapping(item, f"scripts registry.scripts[{index}]")
        identifiers.add(
            _as_nonempty_string(script.get("id"), f"scripts registry.scripts[{index}].id")
        )
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
    for key in ("nodes", "tenants", "active_processes_per_database"):
        number = constraints[key]
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ManifestError(f"profile.constraints.{key} must be a positive integer")
    if not isinstance(constraints["remote_inference"], bool):
        raise ManifestError("profile.constraints.remote_inference must be a boolean")

    _as_string_list(profile["supported_surfaces"], "profile.supported_surfaces")
    _as_string_list(
        profile["optional_extensions"],
        "profile.optional_extensions",
        allow_empty=True,
    )
    _as_string_list(profile["excluded"], "profile.excluded")
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


def load_manifest(path: Path, root: Path) -> Manifest:
    """Load and strictly validate one production-MVP manifest."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ManifestError("repository root must be a directory")
    try:
        manifest_path = path.resolve(strict=True)
        manifest_path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ManifestError("manifest must resolve within the repository root") from exc

    document = _as_mapping(_load_yaml(manifest_path), "root")
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
    return Manifest(
        schema_version=schema_version,
        profile_id=profile_id,
        gates=gates,
    )


def _render_text(report: Mapping[str, object]) -> str:
    status = "READY" if report["ready"] else "BLOCKED"
    lines = [
        f"Production MVP {report['profile']}: {status}",
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
        help="Validate schema, report status, or enforce readiness.",
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
        "--artifacts-dir",
        type=Path,
        help="Inspect a prebuilt sdist/wheel pair instead of building package artifacts.",
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
    args = parser.parse_args()

    if args.action == "package":
        try:
            if args.artifacts_dir is not None:
                summary = inspect_artifact_directory(args.root, args.artifacts_dir)
            elif not args.execute:
                print(_render_package_plan(args.uv))
                return 0
            else:
                summary = build_and_validate_distribution(args.root, args.uv)
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
    except (ManifestError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = manifest.report()
    if args.action == "validate":
        print(f"Valid production MVP manifest: {manifest.profile_id}")
        return 0
    if args.json:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    else:
        print(_render_text(report))
    if args.action == "check" and not report["ready"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
