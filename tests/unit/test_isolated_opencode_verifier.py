from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
VERIFIER = REPO_ROOT / "scripts" / "verify_isolated_opencode.py"


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
