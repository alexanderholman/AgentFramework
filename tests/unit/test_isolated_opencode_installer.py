from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
INSTALLER = REPO_ROOT / "scripts" / "install_isolated_opencode.py"


def test_isolated_opencode_installer_creates_sans_mmpr_layout(tmp_path: Path) -> None:
    source_binary = tmp_path / "source-opencode"
    source_binary.write_text("#!/bin/sh\necho 1.18.4\n", encoding="utf-8")
    source_binary.chmod(0o755)
    root = tmp_path / "opencode-af"

    completed = subprocess.run(
        [
            sys.executable,
            str(INSTALLER),
            "--root",
            str(root),
            "--source-binary",
            str(source_binary),
            "--source-root",
            str(REPO_ROOT),
            "--skip-python-install",
            "--execute",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert (root / "bin/opencode").read_bytes() == source_binary.read_bytes()
    assert (root / "bin/opencode-af").stat().st_mode & 0o111
    config = json.loads((root / "xdg/config/opencode/opencode.json").read_text(encoding="utf-8"))
    assert config["default_agent"] == "dispatcher"
    assert set(config["agent"]) >= {
        "dispatcher",
        "architect",
        "builder",
        "reviewer",
        "tester",
        "general",
    }
    serialized = json.dumps(config).casefold()
    assert "mmpr" not in serialized
    assert "zordon" not in serialized
    launcher = (root / "bin/opencode-af").read_text(encoding="utf-8")
    assert "env -i" in launcher
    assert "OPENCODE_DISABLE_PROJECT_CONFIG=1" in launcher
    assert "OPENCODE_DISABLE_DEFAULT_PLUGINS=1" in launcher
    assert "OPENCODE_DISABLE_EXTERNAL_SKILLS=1" in launcher
    assert "OPENCODE_CONFIG_CONTENT" not in launcher


def test_isolated_opencode_installer_is_dry_run_by_default(tmp_path: Path) -> None:
    root = tmp_path / "opencode-af"

    completed = subprocess.run(
        [sys.executable, str(INSTALLER), "--root", str(root)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert "Dry run only" in completed.stdout
    assert root.exists() is False
