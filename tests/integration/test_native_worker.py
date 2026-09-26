"""Run dependency-free Node protocol tests as part of the repository gate."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_native_worker_protocol_conformance_under_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable; Bare and mobile hosting remain unqualified")
    root = Path(__file__).parents[2]
    suites = sorted((root / "native" / "peers" / "test").glob("*.test.cjs"))
    assert suites
    result = subprocess.run(
        [node, "--test", *(str(path) for path in suites)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
