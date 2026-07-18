import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_repeatable_script_refs.py"
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
