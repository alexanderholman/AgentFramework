"""The same machine setup application boundary for wizard and automation paths."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from af.application.machine_setup import MachineSetupController, MachineSetupRequest


def _emit(payload: dict[str, object]) -> None:
    typer.echo(json.dumps(payload, sort_keys=True))


def register_initialization_commands(
    root_cli: typer.Typer,
    init_group: typer.Typer,
    factory: Callable[[], MachineSetupController],
) -> None:
    def failure(error: Exception) -> None:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None

    @init_group.callback(invoke_without_command=True)
    def initialize(
        context: typer.Context,
        plan: Annotated[
            bool, typer.Option("--plan", help="Discover and preview without mutation.")
        ] = False,
        apply: Annotated[
            bool, typer.Option("--apply", help="Apply the displayed plan after confirmation.")
        ] = False,
        yes: Annotated[
            bool, typer.Option("--yes", help="Explicitly approve the displayed local plan.")
        ] = False,
        non_interactive: Annotated[bool, typer.Option("--non-interactive")] = False,
        state_dir: Annotated[Path, typer.Option("--state-dir", envvar="AF_MACHINE_ROOT")] = Path(
            "storage"
        ),
        profile: Annotated[list[str] | None, typer.Option("--profile")] = None,
        config: Annotated[Path | None, typer.Option("--config")] = None,
        model_path: Annotated[list[Path] | None, typer.Option("--model-path")] = None,
        engine_path: Annotated[list[Path] | None, typer.Option("--engine-path")] = None,
        max_memory_mib: Annotated[int, typer.Option("--max-memory-mib")] = 256,
        minimum_free_disk_mib: Annotated[int, typer.Option("--minimum-free-disk-mib")] = 64,
    ) -> None:
        if context.invoked_subcommand is not None:
            return
        try:
            if plan and apply:
                raise ValueError("--plan and --apply are mutually exclusive")
            interactive = not plan and not non_interactive
            profiles = tuple(profile or ())
            models, engines = tuple(model_path or ()), tuple(engine_path or ())
            if interactive and not profiles and config is None:
                profiles = tuple(
                    part.strip()
                    for part in typer.prompt(
                        "Use-case profiles (core, database, queue, cache, scheduler)",
                        default="core",
                    ).split(",")
                )
                if not models:
                    answer = typer.prompt(
                        "Model files (comma-separated, blank skips)", default="", show_default=False
                    )
                    models = tuple(Path(part.strip()) for part in answer.split(",") if part.strip())
                if not engines:
                    answer = typer.prompt(
                        "Engine executable paths (blank skips)", default="", show_default=False
                    )
                    engines = tuple(
                        Path(part.strip()) for part in answer.split(",") if part.strip()
                    )
            controller = factory()
            request = MachineSetupRequest(
                state_dir,
                profiles=profiles or ("core",),
                model_paths=models,
                engine_paths=engines,
                max_memory_mib=max_memory_mib,
                minimum_free_disk_mib=minimum_free_disk_mib,
            )
            if config is not None:
                if profile or model_path or engine_path:
                    raise ValueError("--config cannot be combined with profiles or discovery paths")
                with config.open("rb") as source:
                    encoded = source.read(1_048_577)
                if len(encoded) > 1_048_576:
                    raise ValueError("machine declaration exceeds its size bound")
                declaration = json.loads(encoded)
                if not isinstance(declaration, dict):
                    raise ValueError("machine declaration must be an object")
                request = MachineSetupRequest.from_declaration(state_dir, declaration)
            proposed = controller.plan(request)
            _emit(proposed.to_dict())
            if plan or (non_interactive and not apply):
                return
            approved = yes or (
                interactive and typer.confirm("Apply this local setup plan?", default=False)
            )
            if not approved:
                if non_interactive and apply:
                    raise ValueError("non-interactive apply requires --yes")
                return
            receipt = controller.apply(proposed, confirmed=True)
            _emit(receipt)
            if receipt["state"] != "completed":
                raise typer.Exit(code=1)
        except (ValueError, TypeError, OSError) as error:
            failure(error)

    @root_cli.command("status")
    def status(
        state_dir: Annotated[Path, typer.Option("--state-dir", envvar="AF_MACHINE_ROOT")] = Path(
            "storage"
        ),
    ) -> None:
        _emit(factory().status(state_dir))

    machine = typer.Typer(
        no_args_is_help=True, help="Local machine configuration and retained-data status."
    )

    @machine.command("status")
    def machine_status(
        state_dir: Annotated[Path, typer.Option("--state-dir", envvar="AF_MACHINE_ROOT")] = Path(
            "storage"
        ),
    ) -> None:
        _emit(factory().status(state_dir))

    @machine.command("export")
    def export(
        state_dir: Annotated[Path, typer.Option("--state-dir", envvar="AF_MACHINE_ROOT")] = Path(
            "storage"
        ),
    ) -> None:
        try:
            _emit(factory().export(state_dir))
        except (ValueError, TypeError, OSError) as error:
            failure(error)

    root_cli.add_typer(machine, name="machine")

    def capability_group(capability: str) -> typer.Typer:
        group = typer.Typer(
            no_args_is_help=True,
            help=f"Explicit local {capability} lifecycle; disabling retains data.",
        )

        @group.command("enable")
        def enable(
            state_dir: Annotated[
                Path, typer.Option("--state-dir", envvar="AF_MACHINE_ROOT")
            ] = Path("storage"),
            plan: Annotated[bool, typer.Option("--plan")] = False,
            yes: Annotated[bool, typer.Option("--yes")] = False,
        ) -> None:
            try:
                controller = factory()
                proposed = controller.enable_plan(state_dir, capability)
                _emit(proposed.to_dict())
                if plan:
                    return
                if not (
                    yes
                    or typer.confirm(
                        "Enable this local capability and dependencies?", default=False
                    )
                ):
                    return
                receipt = controller.apply(proposed, confirmed=True)
                _emit(receipt)
                if receipt["state"] != "completed":
                    raise typer.Exit(code=1)
            except (ValueError, TypeError, OSError) as error:
                failure(error)

        @group.command("status")
        def capability_status(
            state_dir: Annotated[
                Path, typer.Option("--state-dir", envvar="AF_MACHINE_ROOT")
            ] = Path("storage"),
        ) -> None:
            result = factory().status(state_dir)
            _emit({"capability": capability, "machine": result})

        @group.command("disable")
        def disable(
            state_dir: Annotated[
                Path, typer.Option("--state-dir", envvar="AF_MACHINE_ROOT")
            ] = Path("storage"),
            yes: Annotated[bool, typer.Option("--yes")] = False,
        ) -> None:
            try:
                if yes or typer.confirm(
                    "Disable future use and retain all existing data?", default=False
                ):
                    _emit(factory().disable(state_dir, capability, confirmed=True))
            except (ValueError, TypeError, OSError) as error:
                failure(error)

        return group

    for capability in ("database", "queue", "cache", "scheduler"):
        root_cli.add_typer(capability_group(capability), name=capability)
