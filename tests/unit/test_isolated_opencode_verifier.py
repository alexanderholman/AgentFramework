from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).parents[2]
VERIFIER = REPO_ROOT / "scripts" / "verify_isolated_opencode.py"


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_isolated_opencode", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_config(root: Path) -> dict[str, object]:
    template = (REPO_ROOT / "integrations/opencode-isolated/opencode.json.template").read_text(
        encoding="utf-8"
    )
    config = json.loads(template.replace("__AF_MCP__", str(root / "af-venv/bin/af-mcp")))
    config["plugin_origins"] = [
        {"spec": (root / "xdg/config/opencode/plugins/af-route.js").as_uri()}
    ]
    return config


def _agent_list() -> str:
    names = (
        "dispatcher (primary)",
        "architect (subagent)",
        "builder (subagent)",
        "reviewer (subagent)",
        "tester (subagent)",
        "general (subagent)",
        "title (primary)",
        "summary (primary)",
        "compaction (primary)",
    )
    return "\n".join(names) + "\n"


def _run_responses(root: Path, config: dict[str, object], paths: str):
    def run(_launcher: Path, *arguments: str) -> str:
        responses = {
            ("--version",): "1.18.4\n",
            ("debug", "paths"): paths,
            ("debug", "config"): json.dumps(config),
            ("agent", "list"): _agent_list(),
        }
        return responses[arguments]

    return run


def test_verifier_rejects_incomplete_debug_paths(tmp_path: Path, monkeypatch) -> None:
    module = _load_verifier()
    root = tmp_path / "opencode-af"
    config = _valid_config(root)
    paths = f"home       {root}/home\n"
    monkeypatch.setattr(module, "_run", _run_responses(root, config, paths))

    with pytest.raises(RuntimeError, match="paths"):
        module.verify(root, tmp_path / "launcher")


def test_verifier_rejects_filesystem_root(monkeypatch) -> None:
    module = _load_verifier()
    root = Path("/")
    config = _valid_config(root)
    paths = "\n".join(
        (
            "home /home",
            "data /xdg/data/opencode",
            "cache /xdg/cache/opencode",
            "config /xdg/config/opencode",
            "state /xdg/state/opencode",
            "tmp /tmp/opencode",
        )
    )
    monkeypatch.setattr(module, "_run", _run_responses(root, config, paths))

    with pytest.raises(RuntimeError, match="filesystem root"):
        module.verify(root, Path("/launcher"))


def test_verifier_rejects_unexpected_mcp_command(tmp_path: Path, monkeypatch) -> None:
    module = _load_verifier()
    root = tmp_path / "opencode-af"
    config = _valid_config(root)
    config["mcp"]["af"]["command"] = [str(tmp_path / "external/af-mcp")]
    paths = "\n".join(
        (
            f"home {root}/home",
            f"data {root}/xdg/data/opencode",
            f"cache {root}/xdg/cache/opencode",
            f"config {root}/xdg/config/opencode",
            f"state {root}/xdg/state/opencode",
            f"tmp {root}/tmp/opencode",
        )
    )
    monkeypatch.setattr(module, "_run", _run_responses(root, config, paths))

    with pytest.raises(RuntimeError, match="MCP command"):
        module.verify(root, tmp_path / "launcher")


def test_isolated_opencode_verifier_accepts_clean_routed_instance(tmp_path: Path) -> None:
    root = tmp_path / "opencode-af"
    plugin = root / "xdg/config/opencode/plugins/af-route.js"
    template = (REPO_ROOT / "integrations/opencode-isolated/opencode.json.template").read_text(
        encoding="utf-8"
    )
    config = json.loads(template.replace("__AF_MCP__", str(root / "af-venv/bin/af-mcp")))
    config["plugin_origins"] = [{"spec": plugin.as_uri()}]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    launcher = tmp_path / "opencode-af"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"root = {str(root)!r}\n"
        f"config = json.loads(Path({str(config_path)!r}).read_text(encoding='utf-8'))\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('1.18.4')\n"
        "elif args == ['debug', 'paths']:\n"
        "    print(f'home       {root}/home')\n"
        "    print(f'data       {root}/xdg/data/opencode')\n"
        "    print(f'cache      {root}/xdg/cache/opencode')\n"
        "    print(f'config     {root}/xdg/config/opencode')\n"
        "    print(f'state      {root}/xdg/state/opencode')\n"
        "    print(f'tmp        {root}/tmp/opencode')\n"
        "elif args == ['debug', 'config']:\n"
        "    print(json.dumps(config))\n"
        "elif args == ['agent', 'list']:\n"
        "    print('dispatcher (primary)')\n"
        "    print(']')\n"
        "    print('architect (subagent)')\n"
        "    print('builder (subagent)')\n"
        "    print('reviewer (subagent)')\n"
        "    print('tester (subagent)')\n"
        "    print('general (subagent)')\n"
        "    print('title (primary)')\n"
        "    print('summary (primary)')\n"
        "    print('compaction (primary)')\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    completed = subprocess.run(
        [sys.executable, str(VERIFIER), "--root", str(root), "--launcher", str(launcher)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "isolated-opencode-check: ok" in completed.stdout

    config["plugin_origins"] = [{"spec": (tmp_path / "external/plugins/af-route.js").as_uri()}]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    rejected = subprocess.run(
        completed.args,
        capture_output=True,
        check=False,
        text=True,
    )
    assert rejected.returncode == 1
    assert "exactly one AF route plugin" in rejected.stdout

    config["plugin_origins"] = [{"spec": plugin.as_uri()}]
    config["agent"]["reviewer"]["permission"]["edit"] = "allow"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    unsafe = subprocess.run(
        completed.args,
        capture_output=True,
        check=False,
        text=True,
    )
    assert unsafe.returncode == 1
    assert "unexpected permissions for reviewer" in unsafe.stdout
