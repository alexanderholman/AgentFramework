"""Persistent local authentication keys survive races without unsafe fallback."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy.engine import make_url

from af.infrastructure.local_keys import LocalAuthenticationKey, workflow_key


def test_first_start_race_selects_one_private_key_and_restart_reuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = make_url(f"sqlite:///{tmp_path / 'nested' / 'state.db'}")
    ready = Barrier(8)
    original_link = os.link

    def simultaneous_link(source: str, target: Path) -> None:
        ready.wait(timeout=5)
        original_link(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", simultaneous_link)
        with ThreadPoolExecutor(max_workers=8) as pool:
            keys = list(pool.map(lambda _: workflow_key(url), range(8)))
    key_path = tmp_path / "nested" / "state.workflow-key"
    assert len(set(keys)) == 1
    assert len(keys[0]) == 32
    assert key_path.read_bytes() == workflow_key(url) == keys[0]
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert list(key_path.parent.iterdir()) == [key_path]
    assert keys[0].hex() not in repr(LocalAuthenticationKey(keys[0]))
    assert repr(keys[0]) not in repr(LocalAuthenticationKey(keys[0]))


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:"])
def test_volatile_databases_get_isolated_ephemeral_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.chdir(tmp_path)
    assert workflow_key(make_url(url)) != workflow_key(make_url(url))
    assert list(tmp_path.iterdir()) == []


def test_non_sqlite_host_key_is_persistent_without_database_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    url = make_url("postgresql://host/example")
    first = workflow_key(url)
    assert workflow_key(url) == first
    assert (tmp_path / "storage" / "af-workflow.key").read_bytes() == first


@pytest.mark.parametrize("length", [0, 31, 33, 1024])
def test_corrupt_key_is_denied_without_replacement(tmp_path: Path, length: int) -> None:
    key_path = tmp_path / "state.workflow-key"
    key_path.write_bytes(b"x" * length)
    key_path.chmod(0o600)
    with pytest.raises(ValueError, match="invalid length"):
        workflow_key(make_url(f"sqlite:///{tmp_path / 'state.db'}"))
    assert key_path.read_bytes() == b"x" * length


@pytest.mark.parametrize("kind", ["world-readable", "directory", "symlink"])
def test_nonprivate_or_indirect_keys_fail_closed(tmp_path: Path, kind: str) -> None:
    key_path = tmp_path / "state.workflow-key"
    if kind == "directory":
        key_path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "private"
        target.write_bytes(b"x" * 32)
        target.chmod(0o600)
        key_path.symlink_to(target)
    else:
        key_path.write_bytes(b"x" * 32)
        key_path.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        workflow_key(make_url(f"sqlite:///{tmp_path / 'state.db'}"))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO regression requires POSIX")
def test_fifo_key_is_rejected_without_waiting_for_a_writer(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "state.workflow-key", 0o600)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from sqlalchemy.engine import make_url; "
            "from af.infrastructure.local_keys import workflow_key; "
            "workflow_key(make_url(sys.argv[1]))",
            f"sqlite:///{tmp_path / 'state.db'}",
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert result.returncode != 0
    assert "private regular file" in result.stderr
    with pytest.raises(ValueError, match="private regular file"):
        workflow_key(make_url(f"sqlite:///{tmp_path / 'state.db'}"))
