from __future__ import annotations

import importlib.util
import io
import json
import stat
import subprocess
import sys
import tarfile
import warnings
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

REPO_ROOT = Path(__file__).parents[2]
RELEASE_GATES = REPO_ROOT / "scripts" / "release_gates.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("af_release_gates", RELEASE_GATES)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _gate(
    gate_id: str,
    category: str,
    state: str,
    *,
    evidence: list[dict[str, str]] | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    gate: dict[str, Any] = {
        "id": gate_id,
        "category": category,
        "title": gate_id.replace(".", " ").title(),
        "requirement": f"Prove {gate_id} before release.",
        "blocking": True,
        "verification": "automated",
        "state": state,
        "evidence": evidence or [],
        "issues": ["https://github.com/alexanderholman/AgentFramework/issues/3"],
    }
    if next_action is not None:
        gate["next_action"] = next_action
    return gate


def _manifest(gates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "profile": {
            "id": "private-single-node-loopback-deterministic",
            "title": "Private single-node deterministic production MVP",
            "release_target": "0.1.0",
            "maturity": "pre-release",
            "constraints": {
                "operating_system": "linux",
                "python": "3.12",
                "nodes": 1,
                "tenants": 1,
                "active_processes_per_database": 1,
                "database": "sqlite",
                "routes": "deterministic-only",
                "network_exposure": "loopback",
                "remote_inference": False,
            },
            "supported_surfaces": ["cli", "mcp-stdio"],
            "optional_extensions": ["secure-mcp-tunnel"],
            "excluded": ["public-network-exposure", "multi-tenant-operation"],
        },
        "gates": gates,
    }


def _write_fixture(tmp_path: Path, manifest: dict[str, Any]) -> tuple[Path, Path]:
    (tmp_path / "release").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "README.md").write_text("fixture\n", encoding="utf-8")
    (tmp_path / "scripts" / "registry.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0.0",
                "scripts": [{"id": "af-dev", "status": "active"}],
                "policy": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "release" / "production-mvp.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return tmp_path, manifest_path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RELEASE_GATES), *args],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        text=True,
    )


def _write_distribution_fixture(
    tmp_path: Path,
    *,
    omit_wheel_path: str | None = None,
    extra_wheel_path: str | None = None,
    symlink_wheel_path: str | None = None,
    duplicate_wheel_path: str | None = None,
    duplicate_sdist_path: str | None = None,
) -> tuple[Path, Path]:
    version = "0.1.0a0"
    wheel = tmp_path / f"af_agent_framework-{version}-py3-none-any.whl"
    sdist = tmp_path / f"af_agent_framework-{version}.tar.gz"
    dist_info = f"af_agent_framework-{version}.dist-info"
    wheel_files: dict[str, str] = {
        "af/__init__.py": f'__version__ = "{version}"\n',
        "af/py.typed": "",
        "af/views/templates/base.html": "<!doctype html>\n",
        "af/views/templates/index.html": "{% extends 'base.html' %}\n",
        "af/views/templates/tasks/index.html": "{% extends 'base.html' %}\n",
        "af/views/templates/tasks/show.html": "{% extends 'base.html' %}\n",
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.4\n"
            "Name: af-agent-framework\n"
            f"Version: {version}\n"
            "Requires-Python: >=3.12\n"
        ),
        f"{dist_info}/entry_points.txt": (
            "[console_scripts]\naf = af.interfaces.cli:app\naf-mcp = af.interfaces.mcp:main\n"
        ),
        f"{dist_info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    if omit_wheel_path is not None:
        wheel_files.pop(omit_wheel_path)
    if extra_wheel_path is not None:
        wheel_files[extra_wheel_path] = "unexpected\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in wheel_files.items():
            archive.writestr(name, content)
        if symlink_wheel_path is not None:
            link = zipfile.ZipInfo(symlink_wheel_path)
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "af/__init__.py")
        if duplicate_wheel_path is not None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(duplicate_wheel_path, "duplicate\n")

    root = f"af_agent_framework-{version}"
    sdist_files = {
        "pyproject.toml": "[project]\nname='af-agent-framework'\nversion='0.1.0a0'\n",
        "README.md": "# AF\n",
        "LICENSE": "All rights reserved.\n",
        "src/af/__init__.py": f'__version__ = "{version}"\n',
        "release/production-mvp.yaml": "schema_version: '1.0.0'\n",
        "scripts/release_gates.py": "# release gate\n",
        "docs/production-mvp.md": "# Production MVP\n",
    }
    with tarfile.open(sdist, "w:gz") as archive:
        for name, content in sdist_files.items():
            payload = content.encode()
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
        if duplicate_sdist_path is not None:
            payload = b"duplicate\n"
            info = tarfile.TarInfo(f"{root}/{duplicate_sdist_path}")
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    return sdist, wheel


def test_repository_manifest_is_valid_but_honestly_blocked() -> None:
    validated = _run("--action", "validate")
    reported = _run("--action", "report", "--json")

    assert validated.returncode == 0
    assert reported.returncode == 0
    report = json.loads(reported.stdout)
    assert report["profile"] == "private-single-node-loopback-deterministic"
    assert report["ready"] is False
    assert report["blocking_gates"] >= 20
    assert "security.authentication-boundary" in report["blockers"]
    assert "data.backup-restore" in report["blockers"]
    assert "operations.health-readiness" in report["blockers"]
    assert "release.commit-bound-evidence" in report["blockers"]


def test_distribution_inspection_verifies_identity_and_runtime_files(tmp_path: Path) -> None:
    module = _load_module()
    sdist, wheel = _write_distribution_fixture(tmp_path)

    summary = module.inspect_distribution(REPO_ROOT, sdist, wheel)

    assert summary.version == "0.1.0a0"
    assert len(summary.sdist_sha256) == 64
    assert len(summary.wheel_sha256) == 64


def test_distribution_inspection_rejects_missing_typing_marker(tmp_path: Path) -> None:
    module = _load_module()
    sdist, wheel = _write_distribution_fixture(tmp_path, omit_wheel_path="af/py.typed")

    try:
        module.inspect_distribution(REPO_ROOT, sdist, wheel)
    except module.ManifestError as exc:
        assert "wheel is missing required file: af/py.typed" in str(exc)
    else:
        raise AssertionError("missing typing marker was accepted")


def test_distribution_inspection_rejects_runtime_state(tmp_path: Path) -> None:
    module = _load_module()
    sdist, wheel = _write_distribution_fixture(tmp_path, extra_wheel_path="storage/af.sqlite3")

    try:
        module.inspect_distribution(REPO_ROOT, sdist, wheel)
    except module.ManifestError as exc:
        assert "wheel contains forbidden path: storage/af.sqlite3" in str(exc)
    else:
        raise AssertionError("runtime state was accepted in the wheel")


def test_distribution_inspection_rejects_wheel_symlink(tmp_path: Path) -> None:
    module = _load_module()
    sdist, wheel = _write_distribution_fixture(tmp_path, symlink_wheel_path="af/linked.py")

    try:
        module.inspect_distribution(REPO_ROOT, sdist, wheel)
    except module.ManifestError as exc:
        assert "wheel contains unsupported member: af/linked.py" in str(exc)
    else:
        raise AssertionError("wheel symlink was accepted")


def test_distribution_inspection_rejects_duplicate_wheel_member(tmp_path: Path) -> None:
    module = _load_module()
    sdist, wheel = _write_distribution_fixture(tmp_path, duplicate_wheel_path="af/__init__.py")

    try:
        module.inspect_distribution(REPO_ROOT, sdist, wheel)
    except module.ManifestError as exc:
        assert "wheel contains duplicate member: af/__init__.py" in str(exc)
    else:
        raise AssertionError("duplicate wheel member was accepted")


def test_distribution_inspection_rejects_duplicate_sdist_member(tmp_path: Path) -> None:
    module = _load_module()
    sdist, wheel = _write_distribution_fixture(tmp_path, duplicate_sdist_path="pyproject.toml")

    try:
        module.inspect_distribution(REPO_ROOT, sdist, wheel)
    except module.ManifestError as exc:
        assert "sdist contains duplicate member: pyproject.toml" in str(exc)
    else:
        raise AssertionError("duplicate sdist member was accepted")


def test_package_action_can_inspect_prebuilt_artifacts(tmp_path: Path) -> None:
    _write_distribution_fixture(tmp_path)

    completed = _run(
        "--action",
        "package",
        "--artifacts-dir",
        str(tmp_path),
        "--json",
    )

    assert completed.returncode == 0
    report = json.loads(completed.stdout)
    assert report["version"] == "0.1.0a0"
    assert len(report["sdist_sha256"]) == 64
    assert len(report["wheel_sha256"]) == 64


def test_package_action_returns_one_for_invalid_artifact(tmp_path: Path) -> None:
    _write_distribution_fixture(tmp_path, omit_wheel_path="af/py.typed")

    completed = _run(
        "--action",
        "package",
        "--artifacts-dir",
        str(tmp_path),
    )

    assert completed.returncode == 1
    assert "wheel is missing required file: af/py.typed" in completed.stderr


def test_package_build_is_a_dry_run_without_execute() -> None:
    completed = _run("--action", "package")

    assert completed.returncode == 0
    assert "uv build --no-sources --sdist" in completed.stdout
    assert "build the wheel from that sdist" in completed.stdout
    assert "install the wheel into an isolated temporary target" in completed.stdout
    assert "Dry run only. Re-run with --execute to build and smoke the package." in completed.stdout


def test_report_is_deterministic_and_lists_blockers(tmp_path: Path) -> None:
    root, manifest_path = _write_fixture(
        tmp_path,
        _manifest(
            [
                _gate(
                    "quality.source-gate",
                    "quality",
                    "met",
                    evidence=[
                        {
                            "kind": "file",
                            "ref": "README.md",
                            "description": "Fixture evidence.",
                        }
                    ],
                ),
                _gate(
                    "security.authentication",
                    "security",
                    "unmet",
                    next_action="Keep the service on loopback until authentication exists.",
                ),
                _gate(
                    "data.backup-restore",
                    "data",
                    "pending",
                    next_action="Run and record a representative restore drill.",
                ),
            ]
        ),
    )

    completed = _run(
        "--action",
        "report",
        "--manifest",
        str(manifest_path),
        "--root",
        str(root),
        "--json",
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "blocking_gates": 3,
        "blockers": ["data.backup-restore", "security.authentication"],
        "counts": {"met": 1, "pending": 1, "unmet": 1},
        "profile": "private-single-node-loopback-deterministic",
        "ready": False,
        "schema_version": "1.0.0",
    }


def test_check_uses_zero_one_two_exit_contract(tmp_path: Path) -> None:
    blocked_root, blocked_path = _write_fixture(
        tmp_path / "blocked",
        _manifest(
            [
                _gate(
                    "operations.health-readiness",
                    "operations",
                    "unmet",
                    next_action="Add non-mutating health and readiness probes.",
                )
            ]
        ),
    )
    ready_root, ready_path = _write_fixture(
        tmp_path / "ready",
        _manifest(
            [
                _gate(
                    "quality.source-gate",
                    "quality",
                    "met",
                    evidence=[
                        {
                            "kind": "script",
                            "ref": "af-dev",
                            "description": "Registered quality gate.",
                        }
                    ],
                )
            ]
        ),
    )

    blocked = _run(
        "--action",
        "check",
        "--manifest",
        str(blocked_path),
        "--root",
        str(blocked_root),
    )
    ready = _run(
        "--action",
        "check",
        "--manifest",
        str(ready_path),
        "--root",
        str(ready_root),
    )

    assert blocked.returncode == 1
    assert "BLOCKED" in blocked.stdout
    assert "operations.health-readiness" in blocked.stdout
    assert ready.returncode == 0
    assert "READY" in ready.stdout


def test_manifest_rejects_duplicate_gate_ids_and_unknown_fields(tmp_path: Path) -> None:
    duplicate = _gate(
        "quality.source-gate",
        "quality",
        "unmet",
        next_action="Run the gate.",
    )
    manifest = _manifest(
        [
            duplicate,
            _gate(
                "quality.source-gate",
                "quality",
                "unmet",
                next_action="Run the gate.",
            ),
        ]
    )
    manifest["unexpected"] = True
    root, manifest_path = _write_fixture(tmp_path, manifest)

    completed = _run(
        "--action",
        "validate",
        "--manifest",
        str(manifest_path),
        "--root",
        str(root),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "unknown field" in completed.stderr


def test_manifest_rejects_met_gate_without_evidence(tmp_path: Path) -> None:
    root, manifest_path = _write_fixture(
        tmp_path,
        _manifest([_gate("quality.source-gate", "quality", "met")]),
    )

    completed = _run(
        "--action",
        "validate",
        "--manifest",
        str(manifest_path),
        "--root",
        str(root),
    )

    assert completed.returncode == 2
    assert "met gates require evidence" in completed.stderr


def test_manifest_rejects_path_escape_and_yaml_aliases(tmp_path: Path) -> None:
    gate = _gate(
        "quality.source-gate",
        "quality",
        "met",
        evidence=[
            {
                "kind": "file",
                "ref": "../outside.txt",
                "description": "Escaping evidence.",
            }
        ],
    )
    root, manifest_path = _write_fixture(tmp_path, _manifest([gate]))

    escaped = _run(
        "--action",
        "validate",
        "--manifest",
        str(manifest_path),
        "--root",
        str(root),
    )

    manifest_path.write_text(
        "schema_version: '1.0.0'\nprofile: &profile {}\ngates: *profile\n",
        encoding="utf-8",
    )
    aliased = _run(
        "--action",
        "validate",
        "--manifest",
        str(manifest_path),
        "--root",
        str(root),
    )

    assert escaped.returncode == 2
    assert "within the repository root" in escaped.stderr
    assert aliased.returncode == 2
    assert "aliases and anchors are not allowed" in aliased.stderr
