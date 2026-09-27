"""Inspect finite catalogue snapshots and run measured local smoke benchmarks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import typer

from af.application.algorithm_benchmarks import benchmark_algorithm
from af.domain.algorithms import AlgorithmError, ReferenceAlgorithms
from af.domain.problems import ProblemCatalogue


def register_catalogue_commands(
    cli: typer.Typer, algorithms: Callable[[typer.Context], ReferenceAlgorithms]
) -> None:
    group = typer.Typer(no_args_is_help=True, help="Inspect installed computational catalogues.")

    @group.command("algorithms")
    def list_algorithms(context: typer.Context) -> None:
        typer.echo(algorithms(context).snapshot().decode())

    @group.command("problems")
    def list_problems(context: typer.Context) -> None:
        typer.echo(ProblemCatalogue(algorithms(context)).snapshot().decode())

    @group.command("benchmark")
    def benchmark(
        context: typer.Context,
        name: str,
        data: str,
        repetitions: Annotated[int, typer.Option(min=1, max=64)] = 5,
    ) -> None:
        try:
            record = benchmark_algorithm(
                algorithms(context), name, data.encode(), repetitions=repetitions
            )
            typer.echo(record.canonical_bytes().decode())
        except (AlgorithmError, UnicodeError):
            typer.echo("invalid or unavailable catalogue benchmark", err=True)
            raise typer.Exit(code=2) from None

    cli.add_typer(group, name="catalogues")
