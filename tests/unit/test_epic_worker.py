from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
EPIC_WORKER = REPO_ROOT / "scripts" / "af_epic_worker"


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize(
    ("configured_session_id", "expected_session_id"),
    [
        (None, "agentframework-epic-177-worker"),
        ("deployment-session", "deployment-session"),
    ],
)
def test_epic_worker_logs_checkpoint_with_stable_session_id(
    tmp_path: Path,
    configured_session_id: str | None,
    expected_session_id: str,
) -> None:
    state_dir = tmp_path / "state"
    lane_dir = tmp_path / "lane"
    repository = tmp_path / "repository"
    repository.mkdir()
    lane_dir.mkdir()
    directive = lane_dir / "directive.md"
    directive.write_text("Follow the epic.\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "memlog-args.txt"

    _executable(
        fake_bin / "git",
        "#!/bin/sh\n"
        'case " $* " in\n'
        '  *" fetch "*) exit 0 ;;\n'
        "  *\" rev-parse \"*) printf '%s\\n' "
        "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'; exit 0 ;;\n"
        "esac\n"
        "exit 2\n",
    )
    _executable(fake_bin / "gh", "#!/bin/sh\nprintf '%s\\n' '0'\n")
    _executable(
        fake_bin / "memlog",
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$MEMLOG_CAPTURE"\n',
    )
    _executable(
        fake_bin / "opencode",
        "#!/bin/sh\ntouch \"$AF_EPIC_STATE_DIR/STOP\"\nprintf '%s\\n' 'completed fake iteration'\n",
    )

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "AF_EPIC_REPO_DIR": str(repository),
            "AF_EPIC_STATE_DIR": str(state_dir),
            "AF_EPIC_LANE_DIR": str(lane_dir),
            "AF_EPIC_DIRECTIVE_FILE": str(directive),
            "AF_EPIC_POLL_SECONDS": "0",
            "AF_EPIC_ITERATION_TIMEOUT": "10",
            "OPENCODE_BIN": str(fake_bin / "opencode"),
            "MEMLOG_BIN": str(fake_bin / "memlog"),
            "MEMLOG_CAPTURE": str(capture),
        }
    )
    if configured_session_id is not None:
        environment["AF_EPIC_MEMLOG_SESSION_ID"] = configured_session_id

    completed = subprocess.run(
        ["bash", str(EPIC_WORKER)],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    arguments = capture.read_text(encoding="utf-8").splitlines()
    session_index = arguments.index("--session-id")
    assert arguments[session_index + 1] == expected_session_id
    message_index = arguments.index("--message")
    checkpoint_message = arguments[message_index + 1]
    assert "I will continue" in checkpoint_message
    assert "the following arguments are required: --session-id" not in (
        state_dir / "worker.log"
    ).read_text(encoding="utf-8")
