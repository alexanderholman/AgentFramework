from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parents[2]
AF_DEV = REPO_ROOT / "scripts" / "af_dev.py"
ROUTE_AND_RUN = REPO_ROOT / "scripts" / "route_and_run.py"


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
    assert "uv run ruff format --check src/af tests scripts" in completed.stdout
    assert "uv run ruff check src/af tests scripts" in completed.stdout


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
