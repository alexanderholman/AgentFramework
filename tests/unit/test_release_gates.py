from __future__ import annotations

import base64
import copy
import csv
import hashlib
import importlib.util
import io
import json
import stat
import subprocess
import sys
import tarfile
import tomllib
import warnings
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

REPO_ROOT = Path(__file__).parents[2]
RELEASE_GATES = REPO_ROOT / "scripts" / "release_gates.py"
REPOSITORY_MANIFEST = REPO_ROOT / "release" / "production-mvp.yaml"


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
            "release_target": "private-mvp-candidate",
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
            "supported_surfaces": ["cli-offline-maintenance", "mcp-stdio"],
            "optional_extensions": [],
            "excluded": [
                "public-network-exposure",
                "http-html-rest-graphql-openapi",
                "mcp-streamable-http",
                "secure-mcp-tunnel",
                "multi-tenant-operation",
                "model-backed-selection",
                "cross-driver-certification",
                "peer-to-peer-execution",
                "public-package-publication",
            ],
        },
        "gates": gates,
    }


def _repository_policy_manifest(*, state: str = "unmet") -> dict[str, Any]:
    manifest = copy.deepcopy(yaml.safe_load(REPOSITORY_MANIFEST.read_text(encoding="utf-8")))
    for gate in manifest["gates"]:
        gate["state"] = state
        if state == "met":
            gate["evidence"] = [
                {
                    "kind": "file",
                    "ref": "README.md",
                    "description": "Synthetic policy-completeness evidence.",
                }
            ]
            gate.pop("next_action", None)
        else:
            gate["evidence"] = []
            gate["next_action"] = "Complete this gate before release."
    return manifest


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
    omit_dependency_metadata: bool = False,
    omit_record: bool = False,
    tamper_sdist_path: str | None = None,
) -> tuple[Path, Path]:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    wheel = tmp_path / f"af_agent_framework-{version}-py3-none-any.whl"
    sdist = tmp_path / f"af_agent_framework-{version}.tar.gz"
    dist_info = f"af_agent_framework-{version}.dist-info"
    source_root = REPO_ROOT / "src" / "af"
    wheel_files: dict[str, bytes] = {
        f"af/{path.relative_to(source_root).as_posix()}": path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    }
    metadata_lines = [
        "Metadata-Version: 2.4",
        f"Name: {project['name']}",
        f"Version: {version}",
        f"Requires-Python: {project['requires-python']}",
    ]
    if not omit_dependency_metadata:
        metadata_lines.extend(f"Requires-Dist: {item}" for item in project["dependencies"])
    wheel_files[f"{dist_info}/METADATA"] = ("\n".join(metadata_lines) + "\n").encode()
    wheel_files[f"{dist_info}/entry_points.txt"] = (
        "[console_scripts]\n"
        + "".join(f"{name} = {target}\n" for name, target in project["scripts"].items())
    ).encode()
    wheel_files[f"{dist_info}/WHEEL"] = (
        b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    wheel_files[f"{dist_info}/licenses/LICENSE"] = (REPO_ROOT / "LICENSE").read_bytes()
    if omit_wheel_path is not None:
        wheel_files.pop(omit_wheel_path)
    if extra_wheel_path is not None:
        wheel_files[extra_wheel_path] = b"unexpected\n"
    if not omit_record:
        record_buffer = io.StringIO()
        writer = csv.writer(record_buffer, lineterminator="\n")
        for name, content in sorted(wheel_files.items()):
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
            writer.writerow((name, f"sha256={digest.decode()}", str(len(content))))
        writer.writerow((f"{dist_info}/RECORD", "", ""))
        wheel_files[f"{dist_info}/RECORD"] = record_buffer.getvalue().encode()
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
    sdist_files: dict[str, bytes] = {
        "pyproject.toml": (REPO_ROOT / "pyproject.toml").read_bytes(),
        "README.md": (REPO_ROOT / "README.md").read_bytes(),
        "LICENSE": (REPO_ROOT / "LICENSE").read_bytes(),
        "release/production-mvp.yaml": REPOSITORY_MANIFEST.read_bytes(),
        "scripts/release_gates.py": RELEASE_GATES.read_bytes(),
        "docs/production-mvp.md": (REPO_ROOT / "docs/production-mvp.md").read_bytes(),
    }
    sdist_files.update(
        {
            f"src/af/{path.relative_to(source_root).as_posix()}": path.read_bytes()
            for path in source_root.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        }
    )
    if tamper_sdist_path is not None:
        sdist_files[tamper_sdist_path] = b"tampered\n"
    with tarfile.open(sdist, "w:gz") as archive:
        for name, content in sdist_files.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(content)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))
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
    assert report["requirements_complete"] is False
    assert "ready" not in report
    assert report["certifies_production"] is False
    assert report["blocking_gates"] >= 20
    assert "security.authentication-boundary" in report["blockers"]
    assert "data.backup-restore" in report["blockers"]
    assert "operations.health-readiness" in report["blockers"]
    assert "release.commit-bound-evidence" in report["blockers"]


def test_policy_rejects_removed_mandatory_gate(tmp_path: Path) -> None:
    manifest = _repository_policy_manifest()
    manifest["gates"] = manifest["gates"][:-1]
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
    assert "release policy does not match the canonical policy digest" in completed.stderr


def test_policy_rejects_wider_network_or_remote_inference(tmp_path: Path) -> None:
    manifest = _repository_policy_manifest()
    manifest["profile"]["constraints"]["network_exposure"] = "public"
    manifest["profile"]["constraints"]["remote_inference"] = True
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
    assert "release policy does not match the canonical policy digest" in completed.stderr


def test_check_reports_policy_completeness_without_certifying_production(
    tmp_path: Path,
) -> None:
    root, manifest_path = _write_fixture(
        tmp_path,
        _repository_policy_manifest(state="met"),
    )

    completed = _run(
        "--action",
        "check",
        "--manifest",
        str(manifest_path),
        "--root",
        str(root),
        "--json",
    )

    assert completed.returncode == 0
    report = json.loads(completed.stdout)
    assert report["requirements_complete"] is True
    assert report["certifies_production"] is False
    assert "ready" not in report


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


def test_distribution_inspection_requires_record_hashes(tmp_path: Path) -> None:
    module = _load_module()
    sdist, wheel = _write_distribution_fixture(tmp_path, omit_record=True)

    try:
        module.inspect_distribution(REPO_ROOT, sdist, wheel)
    except module.ManifestError as exc:
        assert "wheel is missing a valid RECORD" in str(exc)
    else:
        raise AssertionError("wheel without RECORD was accepted")


def test_distribution_inspection_rejects_dependency_metadata_drift(tmp_path: Path) -> None:
    module = _load_module()
    sdist, wheel = _write_distribution_fixture(tmp_path, omit_dependency_metadata=True)

    try:
        module.inspect_distribution(REPO_ROOT, sdist, wheel)
    except module.ManifestError as exc:
        assert "wheel dependencies do not match pyproject.toml" in str(exc)
    else:
        raise AssertionError("wheel dependency drift was accepted")


def test_distribution_inspection_rejects_foreign_top_level_package(tmp_path: Path) -> None:
    module = _load_module()
    sdist, wheel = _write_distribution_fixture(tmp_path, extra_wheel_path="foreign/__init__.py")

    try:
        module.inspect_distribution(REPO_ROOT, sdist, wheel)
    except module.ManifestError as exc:
        assert "wheel contains unexpected path: foreign/__init__.py" in str(exc)
    else:
        raise AssertionError("foreign top-level package was accepted")


def test_distribution_inspection_binds_sdist_source_to_checkout(tmp_path: Path) -> None:
    module = _load_module()
    sdist, wheel = _write_distribution_fixture(
        tmp_path, tamper_sdist_path="src/af/framework/types.py"
    )

    try:
        module.inspect_distribution(REPO_ROOT, sdist, wheel)
    except module.ManifestError as exc:
        assert "sdist source differs from the checkout: src/af/framework/types.py" in str(exc)
    else:
        raise AssertionError("tampered sdist source was accepted")


def test_package_build_is_a_dry_run_without_execute() -> None:
    completed = _run("--action", "package")

    assert completed.returncode == 0
    assert "uv build --no-sources --sdist" in completed.stdout
    assert "build the wheel from that sdist" in completed.stdout
    assert "install the wheel into an isolated temporary target" in completed.stdout
    assert "Dry run only. Re-run with --execute to build and smoke the package." in completed.stdout


def test_package_execute_returns_two_when_uv_is_unavailable() -> None:
    completed = _run(
        "--action",
        "package",
        "--execute",
        "--uv",
        "definitely-missing-uv-command",
    )

    assert completed.returncode == 2
    assert "uv executable is unavailable" in completed.stderr


def test_release_environment_drops_credentials_and_package_manager_overrides(
    tmp_path: Path, monkeypatch: Any
) -> None:
    module = _load_module()
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("UV_INDEX_URL", "https://user:secret@example.invalid/simple")
    monkeypatch.setenv("PIP_INDEX_URL", "https://user:secret@example.invalid/simple")
    monkeypatch.setenv("PYTHONPATH", "/untrusted")

    environment = module._release_environment(tmp_path)

    assert environment["PATH"] == "/usr/bin"
    assert environment["HOME"].startswith(str(tmp_path))
    assert environment["UV_CACHE_DIR"].startswith(str(tmp_path))
    assert "GITHUB_TOKEN" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "UV_INDEX_URL" not in environment
    assert "PIP_INDEX_URL" not in environment
    assert "PYTHONPATH" not in environment


def test_report_is_deterministic_and_lists_blockers(tmp_path: Path) -> None:
    manifest = _repository_policy_manifest(state="met")
    by_id = {gate["id"]: gate for gate in manifest["gates"]}
    by_id["security.authentication-boundary"].update(
        state="unmet",
        evidence=[],
        next_action="Keep the service on stdio until the OS boundary is proven.",
    )
    by_id["data.backup-restore"].update(
        state="pending",
        evidence=[],
        next_action="Run and record a representative restore drill.",
    )
    root, manifest_path = _write_fixture(
        tmp_path,
        manifest,
    )

    arguments = (
        "--action",
        "report",
        "--manifest",
        str(manifest_path),
        "--root",
        str(root),
        "--json",
    )
    completed = _run(*arguments)
    repeated = _run(*arguments)

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert repeated.stdout == completed.stdout
    report = json.loads(completed.stdout)
    assert report["blocking_gates"] == len(manifest["gates"])
    assert report["blockers"] == [
        "data.backup-restore",
        "security.authentication-boundary",
    ]
    assert report["counts"] == {
        "met": len(manifest["gates"]) - 2,
        "pending": 1,
        "unmet": 1,
    }
    assert report["requirements_complete"] is False
    assert report["certifies_production"] is False


def test_check_uses_zero_one_two_exit_contract(tmp_path: Path) -> None:
    blocked_root, blocked_path = _write_fixture(
        tmp_path / "blocked",
        _repository_policy_manifest(),
    )
    ready_root, ready_path = _write_fixture(
        tmp_path / "ready",
        _repository_policy_manifest(state="met"),
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
    assert "GATES INCOMPLETE" in blocked.stdout
    assert "operations.health-readiness" in blocked.stdout
    assert ready.returncode == 0
    assert "GATES COMPLETE" in ready.stdout
    assert "READY" not in ready.stdout


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


def test_manifest_rejects_duplicate_gate_ids_independently(tmp_path: Path) -> None:
    manifest = _repository_policy_manifest()
    manifest["gates"].append(copy.deepcopy(manifest["gates"][0]))
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
    assert "duplicate gate IDs" in completed.stderr


def test_manifest_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    root, manifest_path = _write_fixture(tmp_path, _repository_policy_manifest())
    manifest_path.write_text(
        "schema_version: '1.0.0'\nschema_version: '1.0.0'\nprofile: {}\ngates: []\n",
        encoding="utf-8",
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
    assert "duplicate YAML key" in completed.stderr


def test_manifest_rejects_symlinked_file_evidence(tmp_path: Path) -> None:
    manifest = _repository_policy_manifest()
    gate = manifest["gates"][0]
    gate["state"] = "met"
    gate.pop("next_action")
    gate["evidence"] = [
        {
            "kind": "file",
            "ref": "linked-evidence.md",
            "description": "Symlinked evidence must not be trusted.",
        }
    ]
    root, manifest_path = _write_fixture(tmp_path, manifest)
    (root / "linked-evidence.md").symlink_to(root / "README.md")

    completed = _run(
        "--action",
        "validate",
        "--manifest",
        str(manifest_path),
        "--root",
        str(root),
    )

    assert completed.returncode == 2
    assert "must not reference a symlink" in completed.stderr


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


def test_manifest_rejects_excessive_yaml_nesting_without_traceback(tmp_path: Path) -> None:
    root, manifest_path = _write_fixture(tmp_path, _repository_policy_manifest())
    manifest_path.write_text(
        "value: " + "[" * 100 + "0" + "]" * 100 + "\n",
        encoding="utf-8",
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
    assert "YAML nesting exceeds the limit" in completed.stderr
    assert "Traceback" not in completed.stderr
