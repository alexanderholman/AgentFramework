"""Machine-readable finite catalogue snapshots and measured smoke benchmarks."""

import json

import typer
from typer.testing import CliRunner

from af.domain.algorithms import ReferenceAlgorithms
from af.interfaces.catalogues import register_catalogue_commands


def test_catalogue_commands_report_installed_snapshots_and_actual_measurements():
    cli = typer.Typer()
    register_catalogue_commands(cli, lambda _: ReferenceAlgorithms())
    runner = CliRunner()
    for name, schema in (
        ("algorithms", "af.algorithms.snapshot@1"),
        ("problems", "af.problems.snapshot@1"),
    ):
        result = runner.invoke(cli, ["catalogues", name])
        assert result.exit_code == 0, result.stdout
        assert json.loads(result.stdout)["schema"] == schema
    result = runner.invoke(
        cli, ["catalogues", "benchmark", "merge-sort", '{"values":[3,1]}', "--repetitions", "2"]
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["repetitions"] == 2
    result = runner.invoke(cli, ["catalogues", "benchmark", "absent", "{}"])
    assert result.exit_code == 2
