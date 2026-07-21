#!/usr/bin/env python3
"""Install a pinned, isolated OpenCode instance actively routed through AF."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
from pathlib import Path

EXPECTED_OPENCODE_VERSION = "1.18.4"
REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPO_ROOT / "integrations" / "opencode-isolated"


def _launcher(root: Path) -> str:
    quoted_root = shlex.quote(str(root))
    return f"""#!/usr/bin/env bash
set -euo pipefail
ROOT={quoted_root}
TERM_VALUE="${{TERM:-xterm-256color}}"
exec env -i \\
  HOME="$ROOT/home" \\
  USER=opencode-af \\
  LOGNAME=opencode-af \\
  PATH="$ROOT/bin:/usr/local/bin:/usr/bin:/bin" \\
  TERM="$TERM_VALUE" \\
  LANG=C.UTF-8 \\
  TMPDIR="$ROOT/tmp" \\
  XDG_CONFIG_HOME="$ROOT/xdg/config" \\
  XDG_DATA_HOME="$ROOT/xdg/data" \\
  XDG_CACHE_HOME="$ROOT/xdg/cache" \\
  XDG_STATE_HOME="$ROOT/xdg/state" \\
  OPENCODE_CONFIG_DIR="$ROOT/xdg/config/opencode" \\
  OPENCODE_CONFIG="$ROOT/xdg/config/opencode/opencode.json" \\
  OPENCODE_DISABLE_PROJECT_CONFIG=1 \\
  OPENCODE_DISABLE_DEFAULT_PLUGINS=1 \\
  OPENCODE_DISABLE_EXTERNAL_SKILLS=1 \\
  OPENCODE_DISABLE_CLAUDE_CODE=1 \\
  OPENCODE_DISABLE_AUTOUPDATE=1 \\
  OPENCODE_DISABLE_SHARE=1 \\
  AF_ROUTER_CLI="$ROOT/af-venv/bin/af" \\
  "$ROOT/bin/opencode" "$@"
"""


def _copy_templates(root: Path) -> None:
    config_dir = root / "xdg" / "config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    for directory in ("agents", "plugins"):
        shutil.copytree(TEMPLATE_ROOT / directory, config_dir / directory, dirs_exist_ok=True)
    shutil.copy2(TEMPLATE_ROOT / "package.json", config_dir / "package.json")
    template = (TEMPLATE_ROOT / "opencode.json.template").read_text(encoding="utf-8")
    rendered = template.replace("__AF_MCP__", str(root / "af-venv" / "bin" / "af-mcp"))
    json.loads(rendered)
    (config_dir / "opencode.json").write_text(rendered + "\n", encoding="utf-8")


def _verify_binary(source_binary: Path) -> None:
    completed = subprocess.run(
        [str(source_binary), "--version"], capture_output=True, check=False, text=True
    )
    if completed.returncode != 0 or completed.stdout.strip() != EXPECTED_OPENCODE_VERSION:
        raise RuntimeError(f"source OpenCode must be exactly {EXPECTED_OPENCODE_VERSION}")


def install(
    *,
    root: Path,
    source_binary: Path,
    source_root: Path,
    uv_binary: str,
    auth_source: Path | None,
    skip_python_install: bool,
    launcher_link: Path | None,
) -> None:
    _verify_binary(source_binary)
    for directory in (
        root / "bin",
        root / "home",
        root / "tmp",
        root / "xdg" / "data" / "opencode",
        root / "xdg" / "cache" / "opencode",
        root / "xdg" / "state" / "opencode",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_binary, root / "bin" / "opencode")
    _copy_templates(root)
    launcher = root / "bin" / "opencode-af"
    launcher.write_text(_launcher(root), encoding="utf-8")
    launcher.chmod(0o755)

    if auth_source is not None:
        auth_target = root / "xdg" / "data" / "opencode" / "auth.json"
        if not auth_target.exists():
            shutil.copy2(auth_source, auth_target)
            auth_target.chmod(0o600)

    if not skip_python_install:
        subprocess.run([uv_binary, "venv", str(root / "af-venv"), "--python", "3.12"], check=True)
        subprocess.run(
            [
                uv_binary,
                "pip",
                "install",
                "--python",
                str(root / "af-venv" / "bin" / "python"),
                str(source_root),
            ],
            check=True,
        )

    if launcher_link is not None:
        if not launcher_link.parent.is_dir():
            raise RuntimeError(f"launcher parent does not exist: {launcher_link.parent}")
        launcher_link.unlink(missing_ok=True)
        launcher_link.symlink_to(launcher)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("~/.local/opencode-af").expanduser())
    parser.add_argument(
        "--source-binary", type=Path, default=Path("~/.opencode/bin/opencode").expanduser()
    )
    parser.add_argument("--source-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--uv-binary", default="uv")
    parser.add_argument("--auth-source", type=Path)
    parser.add_argument("--skip-python-install", action="store_true")
    parser.add_argument("--launcher-link", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    print(f"Install isolated OpenCode {EXPECTED_OPENCODE_VERSION} under {args.root}")
    print(f"Copy binary from {args.source_binary}")
    print(f"Install AF from {args.source_root}")
    if not args.execute:
        print("Dry run only. Re-run with --execute to apply.")
        return 0
    install(
        root=args.root.resolve(),
        source_binary=args.source_binary.resolve(),
        source_root=args.source_root.resolve(),
        uv_binary=args.uv_binary,
        auth_source=args.auth_source.resolve() if args.auth_source else None,
        skip_python_install=args.skip_python_install,
        launcher_link=args.launcher_link.resolve() if args.launcher_link else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
