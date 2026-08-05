from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).parents[2]
VERIFIER = REPO_ROOT / "scripts" / "verify_epic_worker_policy.py"


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_epic_worker_policy", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_config(module: ModuleType) -> dict[str, object]:
    return {
        "default_agent": module.CHECKPOINT_AGENT,
        "permission": copy.deepcopy(module.EXPECTED_PERMISSIONS),
        "agent": {
            module.CHECKPOINT_AGENT: {
                "mode": "primary",
                "disable": False,
                "prompt": module.CHECKPOINT_SYSTEM_PROMPT,
                "permission": copy.deepcopy(module.EXPECTED_PERMISSIONS),
            }
        },
        "mcp": {},
        "plugin": [],
        "plugin_origins": [],
    }


def _valid_agent(module: ModuleType, policy_root: Path) -> dict[str, object]:
    return {
        "name": module.CHECKPOINT_AGENT,
        "mode": "primary",
        "prompt": module.CHECKPOINT_SYSTEM_PROMPT,
        "permission": [
            {"permission": "*", "pattern": "*", "action": "deny"},
            {"permission": "read", "pattern": "*", "action": "allow"},
            {"permission": "edit", "pattern": "*", "action": "allow"},
            {"permission": "bash", "pattern": "*", "action": "deny"},
            {"permission": "task", "pattern": "*", "action": "deny"},
            {"permission": "webfetch", "pattern": "*", "action": "deny"},
            {"permission": "websearch", "pattern": "*", "action": "deny"},
            {"permission": "skill", "pattern": "*", "action": "deny"},
            {"permission": "question", "pattern": "*", "action": "deny"},
            {"permission": "external_directory", "pattern": "*", "action": "deny"},
            {
                "permission": "external_directory",
                "pattern": str(policy_root / "xdg-data/opencode/tool-output/*"),
                "action": "allow",
            },
        ],
        "tools": {
            "read": True,
            "glob": True,
            "grep": True,
            "edit": True,
            "write": True,
            "todowrite": True,
            "bash": False,
            "task": False,
            "webfetch": False,
            "websearch": False,
            "skill": False,
            "question": False,
        },
    }


def test_checkpoint_policy_accepts_exact_effective_config() -> None:
    module = _load_verifier()

    module.verify(_valid_config(module))


def test_rendered_checkpoint_policy_verifies_exactly() -> None:
    module = _load_verifier()

    config = module.render_config()

    module.verify(config)
    assert config["agent"][module.CHECKPOINT_AGENT]["mode"] == "primary"


def test_runtime_agent_policy_allows_only_private_tool_output(tmp_path: Path) -> None:
    module = _load_verifier()

    module.verify_agent(_valid_agent(module, tmp_path), tmp_path)


def test_runtime_agent_policy_rejects_external_allow(tmp_path: Path) -> None:
    module = _load_verifier()
    agent = _valid_agent(module, tmp_path)
    permissions = agent["permission"]
    assert isinstance(permissions, list)
    permissions.append(
        {
            "permission": "external_directory",
            "pattern": "/home/alpha/.local/share/opencode/tool-output/*",
            "action": "allow",
        }
    )

    with pytest.raises(RuntimeError, match="runtime checkpoint policy"):
        module.verify_agent(agent, tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda config, module: config.update(default_agent="build"),
        lambda config, module: config["agent"][module.CHECKPOINT_AGENT].update(mode="subagent"),
        lambda config, module: config["agent"][module.CHECKPOINT_AGENT]["permission"].update(
            bash="allow"
        ),
        lambda config, module: config.update(mcp={"unsafe": {"enabled": True}}),
        lambda config, module: config.update(plugin=["file:///tmp/unsafe.js"]),
    ],
)
def test_checkpoint_policy_rejects_effective_escape(mutation) -> None:
    module = _load_verifier()
    config = _valid_config(module)
    mutation(config, module)

    with pytest.raises(RuntimeError, match="checkpoint policy"):
        module.verify(config)
