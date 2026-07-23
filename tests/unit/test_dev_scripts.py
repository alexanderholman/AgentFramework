from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
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
    assert environment["PATH"].split(os.pathsep)[0] == str(module.PROJECT_ENV_BIN)
    assert environment["AF_APPEND_ONLY_BASE_REF"] == "origin/master"
    assert "GITHUB_TOKEN" not in environment
    assert "PIP_INDEX_URL" not in environment
    assert "UV_CONFIG_FILE" not in environment
    assert "UV_INDEX_URL" not in environment
    assert "UV_NO_SYNC" not in environment
    assert "UV_PROJECT_ENVIRONMENT" not in environment
    nested_python = subprocess.run(
        ["python3", "-c", "import yaml"],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        env=environment,
        text=True,
    )
    assert nested_python.returncode == 0


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
    uv_executable = shutil.which("uv")
    assert uv_executable is not None
    isolated_bin = tmp_path / "bin"
    isolated_bin.mkdir()
    (isolated_bin / "uv").symlink_to(Path(uv_executable).resolve())
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
    environment["PATH"] = os.pathsep.join((str(isolated_bin), "/usr/bin", "/bin"))

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


def test_locked_release_uv_resolution_prefers_inherited_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    module = _load_af_dev()
    inherited_bin = tmp_path / "inherited-bin"
    inherited_bin.mkdir()
    inherited_uv = inherited_bin / "uv"
    inherited_uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    inherited_uv.chmod(0o755)

    project_bin = tmp_path / ".venv" / "bin"
    project_bin.mkdir(parents=True)
    project_uv = project_bin / "uv"
    project_uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    project_uv.chmod(0o755)

    monkeypatch.setattr(module, "PROJECT_ENV_BIN", project_bin)
    monkeypatch.setenv("PATH", os.pathsep.join((str(inherited_bin), "/usr/bin", "/bin")))

    assert module.resolve_locked_uv_executable() == str(inherited_uv.resolve())


def test_locked_release_actions_ignore_project_venv_uv(tmp_path: Path, monkeypatch: Any) -> None:
    module = _load_af_dev()
    inherited_bin = tmp_path / "inherited-bin"
    inherited_bin.mkdir()
    uv_log = tmp_path / "uv.log"
    uv_probe = tmp_path / "uv_probe.py"
    uv_probe.write_text(
        "import pathlib\n"
        "import sys\n"
        "log_path = pathlib.Path(sys.argv[1])\n"
        "log_path.write_text(\n"
        "    log_path.read_text(encoding='utf-8') + sys.argv[2] + '\\n'\n"
        "    if log_path.exists()\n"
        "    else sys.argv[2] + '\\n',\n"
        "    encoding='utf-8',\n"
        ")\n",
        encoding="utf-8",
    )

    inherited_uv = inherited_bin / "uv"
    inherited_uv.write_text(
        (
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(uv_probe))} "
            f"{shlex.quote(str(uv_log))} inherited\n"
        ),
        encoding="utf-8",
    )
    inherited_uv.chmod(0o755)

    project_bin = tmp_path / ".venv" / "bin"
    project_bin.mkdir(parents=True)
    project_uv = project_bin / "uv"
    project_uv.write_text(
        (
            "#!/bin/sh\n"
            f"{shlex.quote(sys.executable)} {shlex.quote(str(uv_probe))} "
            f"{shlex.quote(str(uv_log))} project\n"
            "exit 33\n"
        ),
        encoding="utf-8",
    )
    project_uv.chmod(0o755)

    assert module.ACTIONS["check"][0][0] == "uv"
    assert module.ACTIONS["package"][0][0] == "uv"

    monkeypatch.setattr(module, "PROJECT_ENV_BIN", project_bin)
    monkeypatch.setenv("PATH", os.pathsep.join((str(inherited_bin), "/usr/bin", "/bin")))
    monkeypatch.setattr(
        module,
        "ACTIONS",
        {
            **module.ACTIONS,
            "check": (("uv", "--version"),),
            "package": (("uv", "--version"),),
        },
    )
    monkeypatch.setattr(sys, "argv", [str(AF_DEV), "--action", "check", "--execute"])
    assert module.main() == 0
    monkeypatch.setattr(sys, "argv", [str(AF_DEV), "--action", "package", "--execute"])
    assert module.main() == 0
    assert uv_log.read_text(encoding="utf-8").splitlines() == ["inherited", "inherited"]


def test_locked_release_nested_package_uses_only_inherited_uv(
    tmp_path: Path, monkeypatch: Any
) -> None:
    module = _load_af_dev()
    inherited_bin = tmp_path / "inherited-bin"
    inherited_bin.mkdir()
    uv_log = tmp_path / "uv-log.jsonl"
    uv_probe = tmp_path / "uv_probe.py"
    uv_probe.write_text(
        "import json\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "log_path = Path(sys.argv[1])\n"
        "entry = {'argv': sys.argv[2:]}\n"
        "with log_path.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(entry, separators=(',', ':')) + '\\n')\n"
        "argv = sys.argv[2:]\n"
        "if argv and argv[0] == 'run':\n"
        "    python_index = argv.index('python')\n"
        "    raise SystemExit(subprocess.run(argv[python_index:], check=False).returncode)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    inherited_uv = inherited_bin / "uv"
    inherited_uv.write_text(
        (
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(uv_probe))} "
            f'{shlex.quote(str(uv_log))} "$@"\n'
        ),
        encoding="utf-8",
    )
    inherited_uv.chmod(0o755)

    project_bin = tmp_path / ".venv" / "bin"
    project_bin.mkdir(parents=True)
    project_uv_log = tmp_path / "project-uv.log"
    project_uv = project_bin / "uv"
    project_uv.write_text(
        (f"#!/bin/sh\necho project >> {shlex.quote(str(project_uv_log))}\nexit 33\n"),
        encoding="utf-8",
    )
    project_uv.chmod(0o755)

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    release_gates = scripts_dir / "release_gates.py"
    nested_uv_log = tmp_path / "nested-uv.log"
    release_gates.write_text(
        "from __future__ import annotations\n"
        "import argparse\n"
        "import subprocess\n"
        "from pathlib import Path\n"
        "def main() -> int:\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--action', required=True)\n"
        "    parser.add_argument('--execute', action='store_true')\n"
        "    parser.add_argument('--uv')\n"
        "    args = parser.parse_args()\n"
        "    if args.action != 'package' or not args.execute or not args.uv:\n"
        "        return 9\n"
        "    uv_path = Path(args.uv)\n"
        "    if not uv_path.is_absolute():\n"
        "        return 10\n"
        "    if '.venv' in uv_path.parts:\n"
        "        return 11\n"
        f"    with Path({str(nested_uv_log)!r}).open('a', encoding='utf-8') as handle:\n"
        "        handle.write(str(uv_path) + '\\n')\n"
        "    for command in (\n"
        "        [str(uv_path), 'build', '--sdist'],\n"
        "        [str(uv_path), 'build', '--wheel'],\n"
        "        [str(uv_path), 'pip', 'install', '--offline'],\n"
        "    ):\n"
        "        completed = subprocess.run(command, check=False)\n"
        "        if completed.returncode:\n"
        "            return completed.returncode\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )

    package_command = (
        "uv",
        "run",
        "--no-config",
        "--no-env-file",
        "--offline",
        "--locked",
        "--no-sources",
        "--extra",
        "dev",
        "python",
        "scripts/release_gates.py",
        "--action",
        "package",
        "--execute",
    )
    monkeypatch.setattr(module, "PROJECT_ENV_BIN", project_bin)
    monkeypatch.setattr(
        module,
        "ACTIONS",
        {
            **module.ACTIONS,
            "check": (package_command,),
            "package": (package_command,),
        },
    )
    monkeypatch.setenv("PATH", os.pathsep.join((str(inherited_bin), "/usr/bin", "/bin")))
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(sys, "argv", [str(AF_DEV), "--action", "check", "--execute"])
    assert module.main() == 0
    monkeypatch.setattr(sys, "argv", [str(AF_DEV), "--action", "package", "--execute"])
    assert module.main() == 0

    assert project_uv_log.exists() is False
    assert nested_uv_log.read_text(encoding="utf-8").splitlines() == [
        str(inherited_uv.resolve()),
        str(inherited_uv.resolve()),
    ]
    uv_calls = [
        json.loads(line)["argv"]
        for line in uv_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert (
        uv_calls.count(
            [
                "build",
                "--sdist",
            ]
        )
        == 2
    )
    assert (
        uv_calls.count(
            [
                "build",
                "--wheel",
            ]
        )
        == 2
    )
    assert (
        uv_calls.count(
            [
                "pip",
                "install",
                "--offline",
            ]
        )
        == 2
    )
    nested_run_calls = [call for call in uv_calls if call and call[0] == "run"]
    assert len(nested_run_calls) == 2
    assert all("--uv" in call for call in nested_run_calls)
    assert all(
        call[call.index("--uv") + 1] == str(inherited_uv.resolve()) for call in nested_run_calls
    )


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
