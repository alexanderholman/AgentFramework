"""The installed developer command runs pytest without booting application state."""

import subprocess
import sys


def test_installed_test_command_runs_project_tests_and_preserves_failure(tmp_path):
    test = tmp_path / "test_sample.py"
    test.write_text("def test_sample():\n    assert 1 + 1 == 2\n")
    command = [
        sys.executable,
        "-c",
        "from af.interfaces.console import app; app()",
        "test",
        "-q",
        "test_sample.py",
    ]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "1 passed" in result.stdout
    assert not (tmp_path / "storage").exists()
    test.write_text("def test_sample():\n    assert False\n")
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "1 failed" in result.stdout
    assert not (tmp_path / "storage").exists()
