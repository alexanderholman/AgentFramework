from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
INSTALLER = REPO_ROOT / "scripts" / "install_isolated_opencode.py"


def test_isolated_opencode_installer_creates_sans_mmpr_layout(tmp_path: Path) -> None:
    source_binary = tmp_path / "source-opencode"
    source_binary.write_text("#!/bin/sh\necho 1.18.4\n", encoding="utf-8")
    source_binary.chmod(0o755)
    root = tmp_path / "opencode-af"
    launcher_link = tmp_path / "bin" / "opencode-af"
    launcher_link.parent.mkdir()

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
            "--launcher-link",
            str(launcher_link),
            "--execute",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert (root / "bin/opencode").read_bytes() == source_binary.read_bytes()
    assert root.stat().st_mode & 0o077 == 0
    assert (root / "xdg/config/opencode").stat().st_mode & 0o077 == 0
    assert (root / "bin/opencode-af").stat().st_mode & 0o111
    config = json.loads((root / "xdg/config/opencode/opencode.json").read_text(encoding="utf-8"))
    assert config["default_agent"] == "dispatcher"
    assert config["model"] == "opencode/mimo-v2.5-free"
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
    assert "AF_DATABASE_URL=" in launcher
    assert "OPENCODE_CONFIG_CONTENT" not in launcher
    assert config["agent"]["builder"]["permission"]["bash"] == "deny"
    assert config["agent"]["reviewer"]["permission"]["bash"] == "deny"
    assert config["agent"]["tester"]["permission"]["bash"] == "deny"
    plugin_dir = root / "xdg/config/opencode/plugins"
    assert [path.name for path in plugin_dir.iterdir()] == ["af-route.js"]
    assert (root / "xdg/config/opencode/lib/route-gate-core.js").is_file()

    (plugin_dir / "stale-plugin.js").write_text("export default () => ({})\n", encoding="utf-8")
    repeated = subprocess.run(
        completed.args,
        capture_output=True,
        check=False,
        text=True,
    )
    assert repeated.returncode == 0, repeated.stderr
    assert [path.name for path in plugin_dir.iterdir()] == ["af-route.js"]
    assert (root / "bin/opencode-af").is_file()
    assert launcher_link.is_symlink()
    assert launcher_link.resolve() == root / "bin/opencode-af"


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


def test_isolated_opencode_installer_reuses_existing_python_environment(tmp_path: Path) -> None:
    source_binary = tmp_path / "source-opencode"
    source_binary.write_text("#!/bin/sh\necho 1.18.4\n", encoding="utf-8")
    source_binary.chmod(0o755)
    root = tmp_path / "opencode-af"
    python = root / "af-venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    uv_log = tmp_path / "uv.log"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"path = Path({str(uv_log)!r})\n"
        "with path.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

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
            "--uv-binary",
            str(fake_uv),
            "--execute",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    calls = uv_log.read_text(encoding="utf-8").splitlines()
    assert all(not call.startswith("venv ") for call in calls)
    assert calls == [f"pip install --python {python.parents[1]} {REPO_ROOT}"]


def test_isolated_opencode_installer_rejects_managed_directory_symlinks(tmp_path: Path) -> None:
    source_binary = tmp_path / "source-opencode"
    source_binary.write_text("#!/bin/sh\necho 1.18.4\n", encoding="utf-8")
    source_binary.chmod(0o755)
    root = tmp_path / "opencode-af"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "xdg").symlink_to(outside, target_is_directory=True)

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

    assert completed.returncode != 0
    assert "managed path must not be a symlink" in completed.stderr
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("name", ["package.json", "opencode.json"])
def test_isolated_opencode_installer_rejects_managed_file_symlinks(
    tmp_path: Path,
    name: str,
) -> None:
    source_binary = tmp_path / "source-opencode"
    source_binary.write_text("#!/bin/sh\necho 1.18.4\n", encoding="utf-8")
    source_binary.chmod(0o755)
    root = tmp_path / "opencode-af"
    config_dir = root / "xdg/config/opencode"
    config_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("preserve\n", encoding="utf-8")
    (config_dir / name).symlink_to(outside)

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

    assert completed.returncode != 0
    assert "managed path must not be a symlink" in completed.stderr
    assert outside.read_text(encoding="utf-8") == "preserve\n"
