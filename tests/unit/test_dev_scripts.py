from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

REPO_ROOT = Path(__file__).parents[2]
AF_DEV = REPO_ROOT / "scripts" / "af_dev.py"
ROUTE_AND_RUN = REPO_ROOT / "scripts" / "route_and_run.py"
SCRIPT_REGISTRY = REPO_ROOT / "scripts" / "registry.yaml"


def _load_af_dev() -> ModuleType:
    spec = importlib.util.spec_from_file_location("af_dev_script", AF_DEV)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_af_dev_sync_preview_uses_locked_lockfile() -> None:
    completed = subprocess.run(
        [sys.executable, str(AF_DEV), "--action", "sync"],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout.splitlines()[0] == "uv sync --locked --extra dev"


def test_af_dev_check_preview_lints_all_maintained_python_scripts() -> None:
    completed = subprocess.run(
        [sys.executable, str(AF_DEV), "--action", "check"],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        text=True,
    )

    assert completed.returncode == 0
    assert (
        "uv run --no-config --no-env-file --offline --locked --no-sources --extra dev "
        "ruff format --check src/af tests scripts"
    ) in completed.stdout
    assert (
        "uv run --no-config --no-env-file --offline --locked --no-sources --extra dev "
        "ruff check src/af tests scripts"
    ) in completed.stdout
    assert "node --test tests/opencode/route-gate.test.mjs" in completed.stdout


def test_af_dev_check_preview_validates_release_manifest_and_package() -> None:
    completed = subprocess.run(
        [sys.executable, str(AF_DEV), "--action", "check"],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        text=True,
    )

    assert completed.returncode == 0
    assert (
        "uv run --no-config --no-env-file --offline --locked --no-sources --extra dev "
        "python scripts/release_gates.py --action validate"
    ) in completed.stdout
    assert (
        "uv run --no-config --no-env-file --offline --locked --no-sources --extra dev "
        "python scripts/release_gates.py --action package --execute"
    ) in completed.stdout


def test_release_gate_workflow_is_registered() -> None:
    registry = yaml.safe_load(SCRIPT_REGISTRY.read_text(encoding="utf-8"))
    release_gate = next(script for script in registry["scripts"] if script["id"] == "release-gates")
    release_package = next(
        script for script in registry["scripts"] if script["id"] == "release-package"
    )

    assert release_gate["command"] == ("python3 scripts/af_dev.py --action '<action>' --execute")
    assert set(release_gate["capabilities"]) >= {
        "production-readiness-report",
        "release-declaration-validation",
    }
    assert release_gate["parameters"] == ["action"]
    assert release_package["command"] == ("python3 scripts/af_dev.py --action package --execute")
    assert "package-validation" in release_package["capabilities"]


def test_locked_release_environment_rejects_inherited_tool_overrides(
    monkeypatch: Any,
) -> None:
    module = _load_af_dev()
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("PIP_INDEX_URL", "https://user:secret@example.invalid/simple")
    monkeypatch.setenv("UV_CONFIG_FILE", "/tmp/untrusted-uv.toml")
    monkeypatch.setenv("UV_INDEX_URL", "https://user:secret@example.invalid/simple")
    monkeypatch.setenv("UV_NO_SYNC", "1")
    monkeypatch.setenv("UV_PYTHON", "/tmp/untrusted-python")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/tmp/untrusted-environment")
    monkeypatch.setenv("AF_APPEND_ONLY_BASE_REF", "origin/master")

    environment = module.execution_environment("check")

    assert environment["UV_NO_CONFIG"] == "1"
    assert environment["UV_OFFLINE"] == "1"
    assert environment["UV_PYTHON"] == sys.executable
    assert environment["AF_APPEND_ONLY_BASE_REF"] == "origin/master"
    assert "GITHUB_TOKEN" not in environment
    assert "PIP_INDEX_URL" not in environment
    assert "UV_CONFIG_FILE" not in environment
    assert "UV_INDEX_URL" not in environment
    assert "UV_NO_SYNC" not in environment
    assert "UV_PROJECT_ENVIRONMENT" not in environment


def test_release_report_json_uses_the_sanitized_locked_launcher() -> None:
    completed = subprocess.run(
        [sys.executable, str(AF_DEV), "--action", "release-report-json"],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        text=True,
    )

    assert completed.returncode == 0
    assert (
        "uv run --no-config --no-env-file --offline --locked --no-sources --extra dev "
        "python scripts/release_gates.py --action report --json"
    ) in completed.stdout


def test_locked_release_launcher_ignores_inherited_uv_environment(tmp_path: Path) -> None:
    invalid_environment = tmp_path / "not-a-virtual-environment"
    invalid_environment.write_text("invalid\n", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "UV_CONFIG_FILE": str(tmp_path / "missing-uv.toml"),
            "UV_NO_SYNC": "1",
            "UV_PYTHON": str(tmp_path / "missing-python"),
            "UV_PROJECT_ENVIRONMENT": str(invalid_environment),
        }
    )

    completed = subprocess.run(
        [sys.executable, str(AF_DEV), "--action", "release-validate", "--execute"],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        env=environment,
        text=True,
    )

    assert completed.returncode == 0
    assert "production certification not performed" in completed.stdout


def test_route_and_run_prevents_shell_injection(tmp_path: Path) -> None:
    capture_script = tmp_path / "capture_args.py"
    captured_path = tmp_path / "captured.json"
    marker_path = tmp_path / "marker.txt"
    registry_path = tmp_path / "registry.yaml"
    payload = f"'; touch {marker_path} #"

    capture_script.write_text(
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    registry_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0.0",
                "scripts": [
                    {
                        "id": "capture",
                        "intent": ["capture"],
                        "capabilities": ["argument-recording"],
                        "description": "Capture argv for tests",
                        "command": shlex.join(
                            [
                                sys.executable,
                                str(capture_script),
                                str(captured_path),
                                "<value>",
                            ]
                        ),
                        "status": "active",
                    }
                ],
                "policy": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(ROUTE_AND_RUN),
            "--registry",
            str(registry_path),
            "--script-id",
            "capture",
            "--arg",
            f"value={payload}",
            "--execute",
        ],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        text=True,
    )

    assert completed.returncode == 0
    assert marker_path.exists() is False
    assert json.loads(captured_path.read_text(encoding="utf-8")) == [payload]


def test_route_and_run_rejects_malformed_command_template(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0.0",
                "scripts": [
                    {
                        "id": "broken",
                        "intent": ["broken"],
                        "capabilities": ["argument-recording"],
                        "description": "Broken quoted command",
                        "command": f'{sys.executable} -c "print(1)',
                        "status": "active",
                    }
                ],
                "policy": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(ROUTE_AND_RUN),
            "--registry",
            str(registry_path),
            "--script-id",
            "broken",
            "--execute",
        ],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        text=True,
    )

    assert completed.returncode == 2
    assert "malformed command template" in completed.stderr
