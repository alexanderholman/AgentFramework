"""Installed AF console composition, including governed self-operation commands."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from af.application.account_controller import AccountController
from af.application.authentication import AuthenticationService
from af.application.capability_preview import CapabilitySnapshot
from af.application.data_maintenance import DataMaintenanceController
from af.application.database_setup import (
    DatabaseSetupController,
    DatabaseSetupMode,
    DatabaseSetupRequest,
)
from af.application.decisions import DecisionController
from af.application.engineering_controller import EngineeringController
from af.application.human_decisions import HumanDecisionService
from af.application.human_interpreter import HumanInterpreter
from af.application.introspection import IntrospectionService
from af.application.oauth_controller import OAuthController
from af.application.self_design_execution import SelfDesignExecutionController
from af.application.self_development import SelfDevelopmentController
from af.application.self_improvement import SelfImprovementService
from af.application.self_stages import SelfStageController
from af.application.version_control import VersionControlController
from af.application.workflow_control import WorkflowController
from af.bootstrap import AFApplication, create_offline_improvement_service, diagnose_application
from af.bootstrap_extensions import peer_qualification_demo, reference_evolution_fixture
from af.bootstrap_observability import with_reference_admin
from af.bootstrap_scheduling import SchedulerRuntime
from af.bootstrap_setup import create_machine_setup_controller, machine_root
from af.config import Settings
from af.domain.algorithms import ReferenceAlgorithms
from af.framework.errors import AFError, PlatformError, PlatformReason, ServiceNotBound
from af.framework.routing import ProcedureRegistry
from af.framework.self_alignment_operation import SelfAlignmentOperation
from af.framework.self_analysis_operation import SelfAnalysisOperation
from af.framework.self_design_operation import SelfDesignOperation
from af.infrastructure.machine_setup import machine_settings
from af.interfaces.accounts import register_account_commands
from af.interfaces.authentication import register_authentication_commands
from af.interfaces.capabilities import register_capability_commands
from af.interfaces.catalogues import register_catalogue_commands
from af.interfaces.cli import create_cli as create_base_cli
from af.interfaces.cli import current_application_from_context
from af.interfaces.cli_interactions import CliInteraction
from af.interfaces.data_maintenance import register_data_commands
from af.interfaces.decisions import register_decision_commands
from af.interfaces.engineering import register_engineering_commands
from af.interfaces.human_decisions import register_human_decision_commands
from af.interfaces.human_interpreter import register_human_interpreter_commands
from af.interfaces.introspection import register_introspection
from af.interfaces.oauth_grants import register_oauth_commands
from af.interfaces.observability_products import (
    ObservabilityProducts,
    register_observability_commands,
)
from af.interfaces.scheduling import register_scheduling_commands
from af.interfaces.self_development import register_self_development_commands
from af.interfaces.self_improvement import register_self_improvement_commands
from af.interfaces.self_stages import register_lifecycle_stage_commands
from af.interfaces.starters import register_starter_commands
from af.interfaces.tui import create_tui_kernel, register_tui_commands
from af.interfaces.workflows import register_workflow_commands
from af.search.manager import SearchManager


def create_cli(
    application: AFApplication | None = None,
    *,
    application_factory: Callable[[], AFApplication] | None = None,
    interaction_factory: Callable[[], CliInteraction] | None = None,
    database_setup: DatabaseSetupController | None = None,
    version_control: VersionControlController | None = None,
    self_alignment_operation: SelfAlignmentOperation | None = None,
    self_analysis_operation: SelfAnalysisOperation | None = None,
    self_design_operation: SelfDesignOperation | None = None,
    self_design_execution: SelfDesignExecutionController | None = None,
) -> typer.Typer:
    """Compose the installed CLI without granting self-operation write authority."""
    cli = create_base_cli(
        application,
        application_factory=application_factory,
        interaction_factory=interaction_factory,
        version_control=version_control,
    )
    register_starter_commands(cli)
    register_self_development_commands(
        cli,
        lambda context: current_application_from_context(context).container.resolve(
            SelfDevelopmentController
        ),
    )

    def introspection(context: typer.Context) -> IntrospectionService:
        current = current_application_from_context(context)
        return IntrospectionService(
            current.container.resolve(ProcedureRegistry),
            current.lifecycle.provider_order,
            current.container.resolve(SearchManager).driver_manifests,
        )

    def doctor_report() -> dict[str, object]:
        if application is not None:
            return diagnose_application(application.settings)
        root = machine_root()
        if not (root / "machine.json").exists():
            return diagnose_application(Settings.from_environment())
        report = diagnose_application(machine_settings(root))
        machine = create_machine_setup_controller().status(root)
        report["machine"] = machine
        report["ready"] = report["ready"] and machine["state"] == "healthy"
        return report

    register_introspection(
        cli,
        introspection,
        doctor_report,
    )
    register_capability_commands(
        cli,
        lambda context: current_application_from_context(context).container.resolve(
            CapabilitySnapshot
        ),
    )
    register_authentication_commands(
        cli,
        lambda context: current_application_from_context(context).container.resolve(
            AuthenticationService
        ),
        lambda context: current_application_from_context(context).settings.auth_tenant_id,
    )
    register_account_commands(
        cli,
        lambda context: current_application_from_context(context).container.resolve(
            AccountController
        ),
    )
    register_tui_commands(
        cli,
        lambda context: create_tui_kernel(current_application_from_context(context)),
    )

    def observational_products(context: typer.Context) -> ObservabilityProducts:
        current = current_application_from_context(context)
        return with_reference_admin(
            current.container.resolve(ObservabilityProducts), current.tasks, current.operations
        )

    register_observability_commands(
        cli,
        observational_products,
        lambda context: current_application_from_context(context).container.resolve(
            AuthenticationService
        ),
        lambda context: current_application_from_context(context).settings.auth_tenant_id,
    )
    register_engineering_commands(
        cli,
        lambda context: current_application_from_context(context).container.resolve(
            EngineeringController
        ),
    )
    register_human_interpreter_commands(
        cli,
        lambda context: current_application_from_context(context).container.resolve(
            HumanInterpreter
        ),
    )
    register_human_decision_commands(
        cli,
        lambda context: current_application_from_context(context).container.resolve(
            HumanDecisionService
        ),
    )
    register_catalogue_commands(
        cli,
        lambda context: current_application_from_context(context).container.resolve(
            ReferenceAlgorithms
        ),
    )
    register_data_commands(
        cli,
        lambda context: current_application_from_context(context).container.resolve(
            DataMaintenanceController
        ),
    )
    register_decision_commands(
        cli,
        lambda context: current_application_from_context(context).container.resolve(
            DecisionController
        ),
    )
    register_workflow_commands(
        cli,
        lambda context: current_application_from_context(context).container.resolve(
            WorkflowController
        ),
    )

    register_lifecycle_stage_commands(
        cli,
        lambda context: current_application_from_context(context).container.resolve(
            SelfStageController
        ),
    )
    register_oauth_commands(
        cli,
        lambda context: current_application_from_context(context).container.resolve(
            OAuthController
        ),
    )
    register_scheduling_commands(
        cli,
        lambda context: (
            current_application_from_context(context).container.resolve(SchedulerRuntime).commands
        ),
    )
    register_self_improvement_commands(
        cli,
        lambda context: current_application_from_context(context).container.resolve(
            SelfImprovementService
        ),
        lambda context: create_offline_improvement_service(
            current_application_from_context(context)
        ),
        reference_evolution_fixture,
    )

    @cli.command("peer-demo")
    def peer_demo(execute: bool = typer.Option(False, "--execute")) -> None:
        """Preview or exercise paired local nodes with installed character counting."""
        if not execute:
            typer.echo(
                json.dumps(
                    {
                        "operation": "local-peer-qualification",
                        "execute": False,
                        "effects": [
                            "temporary local files",
                            "loopback sockets",
                            "bounded child process",
                        ],
                    }
                )
            )
            return
        try:
            typer.echo(json.dumps(peer_qualification_demo(), sort_keys=True))
        except (AFError, ValueError, OSError):
            typer.echo('{"error":"peer_demo_failed"}', err=True)
            raise typer.Exit(2) from None

    @cli.command("self-align")
    def plan_self_align() -> None:
        """Emit one canonical read-only alignment plan from configured evidence."""
        try:
            if self_alignment_operation is None:
                raise PlatformError(
                    PlatformReason.CONFIGURATION_INVALID,
                    stage="cli.self-align.evidence-provider",
                )
            typer.echo(self_alignment_operation.plan().canonical_json())
        except (AFError, TypeError, ValueError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None

    @cli.command("self-analysis")
    def plan_self_analysis() -> None:
        """Emit one canonical read-only analysis plan from configured evidence."""
        try:
            if self_analysis_operation is None:
                raise PlatformError(
                    PlatformReason.CONFIGURATION_INVALID,
                    stage="cli.self-analysis.evidence-provider",
                )
            typer.echo(self_analysis_operation.plan().canonical_json())
        except (AFError, TypeError, ValueError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None

    @cli.command("self-design")
    def plan_self_design(context: typer.Context) -> None:
        """Emit one canonical read-only design plan from configured evidence."""
        try:
            operation = self_design_operation
            if operation is None:
                try:
                    operation = current_application_from_context(context).container.resolve(
                        SelfDesignOperation
                    )
                except ServiceNotBound:
                    operation = None
            if operation is None:
                raise PlatformError(
                    PlatformReason.CONFIGURATION_INVALID,
                    stage="cli.self-design.evidence-provider",
                )
            typer.echo(operation.plan().canonical_json())
        except (AFError, TypeError, ValueError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None

    @cli.command("self-design-execute")
    def execute_self_design(
        context: typer.Context,
        execute: bool = typer.Option(
            False,
            "--execute",
            help="Apply one explicitly configured governed self-design cycle.",
        ),
    ) -> None:
        """Preview or apply one explicit governed self-design mutation cycle."""
        try:
            controller = self_design_execution
            if controller is None:
                try:
                    controller = current_application_from_context(context).container.resolve(
                        SelfDesignExecutionController
                    )
                except ServiceNotBound:
                    controller = None
            if controller is None:
                raise PlatformError(
                    PlatformReason.CONFIGURATION_INVALID,
                    stage="cli.self-design.execution-controller",
                )
            if not execute:
                typer.echo(controller.preview().canonical_json())
                return
            typer.echo(
                json.dumps(
                    controller.execute().to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        except (AFError, TypeError, ValueError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None

    db_cli = typer.Typer(
        name="db",
        no_args_is_help=True,
        help="Safe local database setup for AF integration testing.",
    )

    @db_cli.command("setup")
    def setup_database(
        context: typer.Context,
        mode: Annotated[DatabaseSetupMode, typer.Option("--mode")] = DatabaseSetupMode.LOCAL_DEBIAN,
        user: Annotated[
            str,
            typer.Option("--user", help="Database user to create or update."),
        ] = "af_test",
        password: Annotated[
            str,
            typer.Option("--password", help="Password for the configured database user."),
        ] = "af_test_pass",
        database: Annotated[
            str,
            typer.Option("--database", help="Database name to create or reuse."),
        ] = "af_test",
        host: Annotated[
            str,
            typer.Option(
                "--host",
                help="Connection host. local-debian mode only supports localhost or 127.0.0.1.",
            ),
        ] = "127.0.0.1",
        port: Annotated[int, typer.Option("--port", help="Connection port.")] = 3306,
        project_root: Annotated[
            Path | None,
            typer.Option("--project-root", help="Project root whose .env file should be updated."),
        ] = None,
        execute: Annotated[
            bool,
            typer.Option(
                "--execute",
                help="Apply provisioning, verification, and env updates after previewing the plan.",
            ),
        ] = False,
    ) -> None:
        """Preview or apply safe local MariaDB setup through the application boundary."""
        try:
            controller = database_setup
            if controller is None:
                try:
                    controller = current_application_from_context(context).container.resolve(
                        DatabaseSetupController
                    )
                except ServiceNotBound:
                    controller = None
            if controller is None:
                raise PlatformError(
                    PlatformReason.CONFIGURATION_INVALID,
                    stage="cli.db-setup.controller",
                )
            result = controller.setup(
                DatabaseSetupRequest(
                    mode=mode,
                    user=user,
                    password=password,
                    database=database,
                    host=host,
                    port=port,
                    project_root=Path.cwd() if project_root is None else project_root,
                    execute=execute,
                )
            )
            typer.echo(
                json.dumps(
                    result.to_dict(),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        except (AFError, TypeError, ValueError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None

    cli.add_typer(db_cli, name="db")

    return cli


app = create_cli()
