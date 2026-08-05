from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
EPIC_WORKER = REPO_ROOT / "scripts" / "af_epic_worker"


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize("agent", ['unsafe"agent', "alpha-coder", "explore"])
def test_epic_worker_rejects_unsafe_agent_name_before_dispatch(tmp_path: Path, agent: str) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "AF_EPIC_AGENT": agent,
            "AF_EPIC_STATE_DIR": str(tmp_path / "state"),
            "AF_EPIC_LANE_DIR": str(tmp_path / "lane"),
        }
    )

    completed = subprocess.run(
        ["bash", str(EPIC_WORKER)],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 2
    assert "AF_EPIC_AGENT must be af-epic-checkpoint" in completed.stderr


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
    worker_link = tmp_path / "epic-worker"
    worker_link.symlink_to(EPIC_WORKER)
    capture = tmp_path / "memlog-args.txt"
    opencode_capture = tmp_path / "opencode-args.txt"
    prompt_capture = tmp_path / "opencode-prompt.txt"
    config_capture = tmp_path / "opencode-config.json"
    permission_capture = tmp_path / "opencode-permission.txt"
    debug_capture = tmp_path / "opencode-debug-args.txt"
    agent_debug_capture = tmp_path / "opencode-agent-debug-args.txt"
    config_environment_capture = tmp_path / "opencode-config-environment.txt"
    provider_file = tmp_path / "opencode.json"
    provider_file.write_text(
        json.dumps(
            {
                "provider": {
                    "lovelace-general": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "Lovelace General",
                        "options": {
                            "baseURL": "http://127.0.0.1:8080/v1",
                            "timeout": 900000,
                            "apiKey": "local",
                        },
                        "models": {"qwen": {"name": "Qwen", "tool_call": True}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    private_tool_output = state_dir / "opencode-policy/xdg-data/opencode/tool-output/*"
    agent_debug_response = json.dumps(
        {
            "name": "af-epic-checkpoint",
            "mode": "primary",
            "prompt": (
                "Operate only as an untrusted local checkpoint analyst. Never claim "
                "acceptance or completion, and never attempt remote mutation or permission bypass."
            ),
            "permission": [
                {"permission": "*", "pattern": "*", "action": "deny"},
                {"permission": "bash", "pattern": "*", "action": "deny"},
                {"permission": "task", "pattern": "*", "action": "deny"},
                {"permission": "webfetch", "pattern": "*", "action": "deny"},
                {"permission": "websearch", "pattern": "*", "action": "deny"},
                {"permission": "skill", "pattern": "*", "action": "deny"},
                {"permission": "question", "pattern": "*", "action": "deny"},
                {"permission": "external_directory", "pattern": "*", "action": "deny"},
                {
                    "permission": "external_directory",
                    "pattern": str(private_tool_output),
                    "action": "allow",
                },
            ],
            "tools": {
                "bash": False,
                "task": False,
                "webfetch": False,
                "websearch": False,
                "skill": False,
                "question": False,
            },
        }
    )

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
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" '
        '"$XDG_CACHE_HOME" "$XDG_STATE_HOME" "$TMPDIR" "$OPENCODE_CONFIG_DIR" '
        '"${OPENCODE_CONFIG-unset}" > "$CONFIG_ENVIRONMENT_CAPTURE"\n'
        'if [ "$1 $2 $3" = "--pure debug config" ]; then\n'
        '  printf \'%s\\n\' "$@" > "$DEBUG_CAPTURE"\n'
        "  printf '%s\\n' \"$OPENCODE_CONFIG_CONTENT\"\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1 $2 $3" = "--pure debug agent" ]; then\n'
        '  printf \'%s\\n\' "$@" > "$AGENT_DEBUG_CAPTURE"\n'
        "  printf '%s\\n' \"$AGENT_DEBUG_RESPONSE\"\n"
        "  exit 0\n"
        "fi\n"
        'touch "$AF_EPIC_STATE_DIR/STOP"\n'
        'printf \'%s\\n\' "$@" > "$OPENCODE_CAPTURE"\n'
        "for argument do prompt=$argument; done\n"
        'printf \'%s\' "$prompt" > "$PROMPT_CAPTURE"\n'
        'printf \'%s\' "$OPENCODE_CONFIG_CONTENT" > "$CONFIG_CAPTURE"\n'
        'if [ "${OPENCODE_PERMISSION+x}" = x ]; then\n'
        "  printf 'present\\n' > \"$PERMISSION_CAPTURE\"\n"
        "else\n"
        "  printf 'absent\\n' > \"$PERMISSION_CAPTURE\"\n"
        "fi\n"
        "printf '%s\\n' 'completed fake iteration'\n",
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
            "OPENCODE_CAPTURE": str(opencode_capture),
            "PROMPT_CAPTURE": str(prompt_capture),
            "CONFIG_CAPTURE": str(config_capture),
            "PERMISSION_CAPTURE": str(permission_capture),
            "DEBUG_CAPTURE": str(debug_capture),
            "AGENT_DEBUG_CAPTURE": str(agent_debug_capture),
            "AGENT_DEBUG_RESPONSE": agent_debug_response,
            "CONFIG_ENVIRONMENT_CAPTURE": str(config_environment_capture),
            "OPENCODE_CONFIG_CONTENT": '{"permission":"allow"}',
            "OPENCODE_PERMISSION": '"allow"',
            "OPENCODE_CONFIG": str(tmp_path / "unsafe.json"),
            "OPENCODE_CONFIG_DIR": str(tmp_path / "unsafe-config"),
            "AF_EPIC_POLICY_PYTHON": sys.executable,
            "AF_EPIC_OPENCODE_CONFIG_FILE": str(provider_file),
            "AF_EPIC_MODEL": "lovelace-general/qwen",
        }
    )
    if configured_session_id is not None:
        environment["AF_EPIC_MEMLOG_SESSION_ID"] = configured_session_id

    completed = subprocess.run(
        ["bash", str(worker_link)],
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
    worker_log = (state_dir / "worker.log").read_text(encoding="utf-8")
    assert "the following arguments are required: --session-id" not in worker_log
    assert "iteration 1 finished rc=0" in worker_log
    assert "iteration 1 completed" not in worker_log

    opencode_arguments = opencode_capture.read_text(encoding="utf-8").splitlines()
    assert opencode_arguments[:2] == ["--pure", "run"]
    assert debug_capture.read_text(encoding="utf-8").splitlines() == [
        "--pure",
        "debug",
        "config",
    ]
    assert agent_debug_capture.read_text(encoding="utf-8").splitlines() == [
        "--pure",
        "debug",
        "agent",
        "af-epic-checkpoint",
    ]
    assert config_environment_capture.read_text(encoding="utf-8").splitlines() == [
        str(state_dir / "opencode-policy/home"),
        str(state_dir / "opencode-policy/xdg-config"),
        str(state_dir / "opencode-policy/xdg-data"),
        str(state_dir / "opencode-policy/xdg-cache"),
        str(state_dir / "opencode-policy/xdg-state"),
        str(state_dir / "opencode-policy/tmp"),
        str(state_dir / "opencode-policy/opencode-config"),
        "unset",
    ]
    assert not (state_dir / "opencode-policy/xdg-data/opencode/auth.json").exists()
    prompt = prompt_capture.read_text(encoding="utf-8")
    assert prompt.index("Follow the epic.") < prompt.index("## Unattended safety boundary")
    normalized_prompt = " ".join(prompt.split())
    assert "must not push, merge, close, comment, review, or mutate GitHub" in normalized_prompt
    assert "must not claim issue, epic, or acceptance completion" in normalized_prompt

    config = json.loads(config_capture.read_text(encoding="utf-8"))
    assert config["default_agent"] == "af-epic-checkpoint"
    assert config["model"] == "lovelace-general/qwen"
    assert set(config["provider"]) == {"lovelace-general"}
    permissions = config["agent"]["af-epic-checkpoint"]["permission"]
    assert permissions["*"] == "deny"
    assert permissions["read"]["*.env"] == "deny"
    assert permissions["read"]["*.env.*"] == "deny"
    assert permissions["edit"]["*"] == "allow"
    assert permissions["edit"][".git/**"] == "deny"
    assert permissions["bash"] == "deny"
    assert permissions["task"] == "deny"
    assert permissions["webfetch"] == "deny"
    assert permissions["websearch"] == "deny"
    assert permissions["external_directory"] == "deny"
    assert permission_capture.read_text(encoding="utf-8") == "absent\n"

    current = json.loads((state_dir / "current.json").read_text(encoding="utf-8"))
    assert current["executionMode"] == "checkpoint-only"
