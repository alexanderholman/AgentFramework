import runpy
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import cast

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_repeatable_script_refs.py"
REPO_ROOT = Path(__file__).parents[2]
has_script_ref = cast(
    Callable[[list[str], int], bool], runpy.run_path(str(SCRIPT))["has_script_ref"]
)


def test_script_reference_five_lines_after_repeatable_statement_is_nearby() -> None:
    lines = [
        "Repeatable work must be scripted.",
        "detail one",
        "detail two",
        "detail three",
        "detail four",
        "Use scripts/registry.yaml.",
    ]

    assert has_script_ref(lines, 0) is True


def test_dev_dependencies_include_pyyaml_for_registry_scripts() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    dev_dependencies = pyproject["project"]["optional-dependencies"]["dev"]

    assert any(dependency.lower().startswith("pyyaml") for dependency in dev_dependencies)
    assert all(not dependency.lower().startswith("httpx2") for dependency in dev_dependencies)


def test_validate_agents_uses_uv_managed_python_for_yaml_registry_checks() -> None:
    script = (REPO_ROOT / "validate_agents.sh").read_text(encoding="utf-8")

    assert "uv run python3 scripts/validate_registry.py --registry scripts/registry.yaml" in script
    assert (
        "uv run python3 scripts/check_dry_run_conventions.py --registry scripts/registry.yaml"
        in script
    )
