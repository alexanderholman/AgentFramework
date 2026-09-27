"""Composition root and service providers for AF."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import cast

from sqlalchemy.engine import make_url

from af.application.account_controller import AccountController
from af.application.agent_routing import AgentRoutingController, AgentRoutingService
from af.application.authentication import AuthenticationService
from af.application.bus import ApplicationBus
from af.application.capability_preview import CapabilitySnapshot
from af.application.data_maintenance import DataMaintenanceController
from af.application.database_setup import (
    ConnectionUrlWriter,
    ConnectionUrlWriterToken,
    DatabaseConnectionVerifier,
    DatabaseConnectionVerifierToken,
    DatabaseSetupController,
    DatabaseSetupService,
    LocalDatabaseProvisioner,
    LocalDatabaseProvisionerToken,
)
from af.application.decisions import DecisionController
from af.application.dispatch import build_application_endpoints
from af.application.engineering import LocalEngineeringService
from af.application.engineering_controller import EngineeringController
from af.application.human_decisions import HumanDecisionService
from af.application.human_interpreter import HumanInterpreter
from af.application.ideons import IdeonController, IdeonService
from af.application.locales import Localizer
from af.application.oauth_controller import OAuthController
from af.application.observability_middleware import ObservationMiddleware
from af.application.operations import (
    OperationsController,
    OperationsService,
    RepositoryOperationsTaskReader,
)
from af.application.program_jobs import ProgramJobHandler, ProgramJobService
from af.application.resource_leases import ResourceLeaseService
from af.application.search import SearchController, SearchService
from af.application.self_development import SelfDevelopmentController
from af.application.self_improvement import SelfImprovementService
from af.application.self_stages import SelfStageController
from af.application.tasks import TaskController, TaskService
from af.application.worker_generation import WorkerGenerationFactory
from af.application.worker_supervision import WorkerSupervisorFactory
from af.application.workflow_control import WorkflowController
from af.application.workflows import WorkflowService
from af.bootstrap_engineering import LocalEngineeringProvider
from af.bootstrap_extensions import (
    create_account_controller,
    create_authentication_service,
    create_capability_snapshot,
    create_decision_controller,
    create_human_decision_service,
    create_oauth_controller,
    create_stream_driver,
)
from af.bootstrap_observability import create_observability_products
from af.bootstrap_scheduling import SchedulerRuntime
from af.bootstrap_self_development import LocalSelfDevelopmentProvider
from af.config import (
    ConfigSnapshot,
    EnvironmentConfigSource,
    ResourceLeaseBackend,
    Settings,
    load_settings,
)
from af.domain.agent_routing import DeterministicAgentRouter
from af.domain.catalogue_procedures import CatalogueProvider
from af.domain.classifier_topology import default_classifier_hierarchy
from af.domain.classifiers import (
    CharacterCountClassifier,
    ClassifierChain,
    HelloClassifier,
    TimeClassifier,
    UnknownClassifier,
)
from af.domain.procedures import (
    CharacterCountProcedure,
    CurrentTimeProcedure,
    HelloProcedure,
)
from af.domain.resources import LeaseRepository
from af.domain.science_procedures import ScienceProvider
from af.domain.tasks import TaskRepository
from af.domain.verification import DeterministicVerifier
from af.framework.application import Application
from af.framework.classification_hierarchy import HierarchyRouter
from af.framework.container import (
    Container,
    ProviderRegistrar,
    ProviderResourceRegistry,
    Resolver,
    Scope,
    ServiceProvider,
)
from af.framework.contracts import Classifier as FrameworkClassifier
from af.framework.contracts import ReuseStore, Selector, TraceRepository
from af.framework.errors import AsyncLifecycleRequired, InvalidProviderConfiguration
from af.framework.pipeline import PipelineAgent
from af.framework.procedure_contributors import resolve_procedure_contributions
from af.framework.reuse import InMemoryReuseStore, ReplayRegressionGate
from af.framework.routing import LadderRouter, ProcedureRegistry, RoutingPolicy
from af.framework.types import (
    SelectorLocality,
    SelectorMetadata,
    is_valid_selector_provider_id,
    parse_probability,
)
from af.infrastructure.data_maintenance import SQLiteDataMaintenance
from af.infrastructure.database import Database
from af.infrastructure.db_setup import (
    AtomicEnvFileWriter,
    LocalMariaDBProvisioner,
    MariaDBConnectionVerifier,
)
from af.infrastructure.doctor import diagnose
from af.infrastructure.ideon_reference import create_reference_ideon_runtime
from af.infrastructure.job_events import SqlAlchemyJobEventRepository
from af.infrastructure.local_keys import LocalAuthenticationKey, workflow_key
from af.infrastructure.locale_packs import create_starter_localizer
from af.infrastructure.model_selectors import LocalModelSelector
from af.infrastructure.program_job_results import SqlAlchemyProgramJobResultRepository
from af.infrastructure.programs import AsyncSubprocessExecutor
from af.infrastructure.repositories import SqlAlchemyTaskRepository
from af.infrastructure.resource_leases import InMemoryLeaseRepository
from af.infrastructure.self_operation_fixtures import create_fixture_self_improvement_service
from af.infrastructure.sqlite import SQLiteDriver
from af.infrastructure.sqlite_resource_leases import SQLiteLeaseRepository
from af.infrastructure.sqlite_streams import SQLiteStreamDriver
from af.infrastructure.tracing import SqlAlchemyTraceRepository
from af.infrastructure.worker_optimization import (
    load_worker_optimization_profile,
    worker_queue_storage_identity,
)
from af.infrastructure.workflow_factory import create_workflow_service, reference_composition
from af.interfaces.observability_products import ObservabilityProducts
from af.queue.database_manager import database_queue_registration
from af.queue.manager import QueueManager
from af.search.embedded_manager import embedded_search_registration
from af.search.manager import SearchManager, bind_search_manager


class _PersistenceProvider(ServiceProvider):
    provider_id = "af.persistence"

    def __init__(
        self,
        settings: Settings,
        task_repository: TaskRepository | None,
        trace_repository: TraceRepository | None,
        lease_repository: LeaseRepository | None,
    ) -> None:
        self._settings = settings
        self._task_repository = task_repository
        self._trace_repository = trace_repository
        self._lease_repository = lease_repository

    def register(self, container: ProviderRegistrar) -> None:
        if self._task_repository is not None:
            task_repository = self._task_repository
            container.bind(
                TaskRepository,
                lambda _: task_repository,
                scope=Scope.APPLICATION,
                owned=False,
            )
        if self._trace_repository is not None:
            trace_repository = self._trace_repository
            container.bind(
                TraceRepository,
                lambda _: trace_repository,
                scope=Scope.APPLICATION,
                owned=False,
            )
        if self._lease_repository is not None:
            lease_repository = self._lease_repository
            container.bind(
                LeaseRepository,
                lambda _: lease_repository,
                scope=Scope.APPLICATION,
                owned=False,
            )
        elif self._settings.resource_lease_backend is ResourceLeaseBackend.SQLITE:
            container.bind(
                SQLiteDriver,
                lambda _: SQLiteDriver(Path(self._settings.resource_lease_sqlite_path)),
                scope=Scope.APPLICATION,
            )
            container.bind(
                LeaseRepository,
                lambda current: SQLiteLeaseRepository(current.resolve(SQLiteDriver)),
                scope=Scope.APPLICATION,
            )
        else:
            container.bind(
                LeaseRepository,
                lambda _: InMemoryLeaseRepository(),
                scope=Scope.APPLICATION,
            )
        if (
            self._task_repository is not None
            and self._trace_repository is not None
            and self._lease_repository is not None
            and self._settings.worker_queue_sqlite_path is None
        ):
            container.bind(
                Database, lambda _: Database("sqlite:///:memory:"), scope=Scope.APPLICATION
            )
            return

        container.bind(
            Database,
            lambda _: Database(self._settings.database_url),
            scope=Scope.APPLICATION,
        )
        if self._task_repository is None:
            container.bind(
                TaskRepository,
                lambda current: SqlAlchemyTaskRepository(current.resolve(Database)),
                scope=Scope.APPLICATION,
            )
        if self._trace_repository is None:
            container.bind(
                TraceRepository,
                lambda current: SqlAlchemyTraceRepository(current.resolve(Database)),
                scope=Scope.APPLICATION,
            )

    def boot(
        self,
        container: Container,
        resources: ProviderResourceRegistry,
    ) -> None:
        del resources
        if (
            self._lease_repository is None
            and self._settings.resource_lease_backend is ResourceLeaseBackend.SQLITE
        ):
            container.resolve_token(LeaseRepository)
        if self._task_repository is None or self._trace_repository is None:
            container.resolve(Database)


class _FrameworkProvider(ServiceProvider):
    provider_id = "af.framework"
    dependencies = ("af.persistence",)

    def __init__(
        self,
        reuse_store: ReuseStore | None,
        selector: Selector | None,
        selector_metadata: SelectorMetadata | None,
        selector_confidence_threshold: float,
        classifier_hierarchy: HierarchyRouter | None,
    ) -> None:
        self._classifier_hierarchy = classifier_hierarchy
        self._reuse_store = reuse_store
        self._selector = selector
        self._selector_metadata = selector_metadata
        self._selector_confidence_threshold = selector_confidence_threshold

    def register(self, container: ProviderRegistrar) -> None:
        def create_registry(resolver: Resolver) -> ProcedureRegistry:
            registry = ProcedureRegistry()
            registry.register(HelloProcedure())
            registry.register(CharacterCountProcedure())
            registry.register(CurrentTimeProcedure())
            for contribution in resolve_procedure_contributions(resolver):
                registry.register(contribution.procedure)
            return registry

        def create_classifier(resolver: Resolver) -> ClassifierChain:
            classifiers: list[FrameworkClassifier] = [
                HelloClassifier(),
                CharacterCountClassifier(),
                TimeClassifier(),
            ]
            classifiers.extend(
                contribution.classifier
                for contribution in resolve_procedure_contributions(resolver)
            )
            classifiers.append(UnknownClassifier())
            return ClassifierChain(
                classifiers,
                hierarchy=self._classifier_hierarchy
                or default_classifier_hierarchy(
                    resolver.resolve(ProcedureRegistry), resolve_procedure_contributions(resolver)
                ),
            )

        container.bind(
            ProcedureRegistry,
            create_registry,
            scope=Scope.APPLICATION,
        )
        container.bind(
            ClassifierChain,
            create_classifier,
            scope=Scope.APPLICATION,
        )
        container.bind(
            DeterministicVerifier,
            lambda resolver: DeterministicVerifier(
                contribution.verifier for contribution in resolve_procedure_contributions(resolver)
            ),
            scope=Scope.APPLICATION,
        )
        if self._reuse_store is None:
            container.bind(
                ReuseStore,
                lambda _: InMemoryReuseStore(),
                scope=Scope.APPLICATION,
            )
        else:
            reuse_store = self._reuse_store
            container.bind(
                ReuseStore,
                lambda _: reuse_store,
                scope=Scope.APPLICATION,
                owned=False,
            )
        container.bind(
            LadderRouter,
            lambda current: LadderRouter(
                current.resolve(ProcedureRegistry),
                RoutingPolicy(),
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            PipelineAgent,
            lambda current: PipelineAgent(
                classifier=current.resolve(ClassifierChain),
                registry=current.resolve(ProcedureRegistry),
                router=current.resolve(LadderRouter),
                verifier=current.resolve(DeterministicVerifier),
                traces=current.resolve_token(TraceRepository),
                regression_gate=ReplayRegressionGate(),
                reuse_store=current.resolve_token(ReuseStore),
                selector=self._selector,
                selector_metadata=self._selector_metadata,
                selector_confidence_threshold=self._selector_confidence_threshold,
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            DeterministicAgentRouter,
            lambda _: DeterministicAgentRouter(),
            scope=Scope.APPLICATION,
        )


class _SearchProvider(ServiceProvider):
    provider_id = "af.search"
    dependencies = ("af.persistence",)

    def __init__(
        self,
        settings: Settings,
        *,
        profile: str,
        ephemeral: bool,
    ) -> None:
        self._settings = settings
        self._profile = profile
        self._ephemeral = ephemeral

    def register(self, container: ProviderRegistrar) -> None:
        bind_search_manager(
            container,
            connections={
                "primary": {
                    "driver": "embedded",
                    "database": _search_database(self._settings, ephemeral=self._ephemeral),
                }
            },
            default="primary",
            deployment_profile=self._profile,
            policy_version="af-search-v1",
            registrations=(embedded_search_registration(),),
        )


class _IdeonProvider(ServiceProvider):
    provider_id = "af.ideons"

    def __init__(self, service: IdeonService | None) -> None:
        self._service = service

    def register(self, container: ProviderRegistrar) -> None:
        if self._service is not None:
            service = self._service
            container.bind(
                IdeonService,
                lambda _: service,
                scope=Scope.APPLICATION,
                owned=False,
            )
            return
        container.bind(
            IdeonService,
            lambda _: create_reference_ideon_runtime().service,
            scope=Scope.APPLICATION,
        )

    def boot(
        self,
        container: Container,
        resources: ProviderResourceRegistry,
    ) -> None:
        del resources
        container.resolve(IdeonService)


class _ApplicationProvider(ServiceProvider):
    provider_id = "af.application"
    dependencies = ("af.framework", "af.ideons", "af.search")

    def __init__(self, *, self_development_configured: bool = False) -> None:
        self._self_development_configured = self_development_configured

    def register(self, container: ProviderRegistrar) -> None:
        if not self._self_development_configured:
            container.bind(
                SelfDevelopmentController,
                lambda _: SelfDevelopmentController(),
                scope=Scope.APPLICATION,
            )
        container.bind(
            SQLiteStreamDriver,
            lambda current: create_stream_driver(current.resolve(Database)),
            scope=Scope.APPLICATION,
        )
        container.bind(
            ObservabilityProducts,
            lambda current: create_observability_products(
                current.resolve(Settings), current.resolve(Database)
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            EngineeringController,
            lambda current: EngineeringController(
                current.resolve(LocalEngineeringService)
                if current.resolve(Settings).engineering_repository is not None
                else None
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            HumanDecisionService,
            lambda current: create_human_decision_service(
                current.resolve(Database),
                current.resolve(LocalAuthenticationKey).value,
                current.resolve(PipelineAgent),
                current.resolve(ClassifierChain),
                current.resolve(ProcedureRegistry),
                current.resolve(LadderRouter),
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            AccountController,
            lambda current: create_account_controller(
                current.resolve(Settings),
                current.resolve(Database),
                current.resolve(AuthenticationService),
                current.resolve(LocalAuthenticationKey).value,
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            HumanInterpreter,
            lambda current: HumanInterpreter(current.resolve(IdeonController)),
            scope=Scope.APPLICATION,
        )
        container.bind(
            SelfStageController, lambda _: SelfStageController(), scope=Scope.APPLICATION
        )
        container.bind(
            SchedulerRuntime,
            lambda current: SchedulerRuntime(
                current.resolve(Settings),
                current.resolve(Database),
                current.resolve(TaskController),
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            OAuthController,
            lambda current: create_oauth_controller(
                current.resolve(Settings),
                current.resolve(Database),
                current.resolve(AuthenticationService),
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            LocalAuthenticationKey,
            lambda current: LocalAuthenticationKey(
                workflow_key(current.resolve(Database).engine.url)
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            AuthenticationService,
            lambda current: create_authentication_service(current.resolve(Database)),
            scope=Scope.APPLICATION,
        )
        container.bind(
            CapabilitySnapshot,
            lambda current: create_capability_snapshot(current.resolve(Settings)),
            scope=Scope.APPLICATION,
        )
        container.bind(Localizer, lambda _: create_starter_localizer(), scope=Scope.APPLICATION)
        container.bind(
            DecisionController,
            lambda current: create_decision_controller(
                current.resolve(Settings), current.resolve(Database)
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            DataMaintenanceController,
            lambda current: DataMaintenanceController(
                SQLiteDataMaintenance(
                    current.resolve(Database),
                    authentication_key=current.resolve(LocalAuthenticationKey).value,
                )
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            WorkflowService,
            lambda current: create_workflow_service(
                current.resolve(Database),
                authentication_key=current.resolve(LocalAuthenticationKey).value,
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            WorkflowController,
            lambda current: WorkflowController(
                current.resolve(WorkflowService), (reference_composition().definition,)
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            IdeonController,
            lambda current: IdeonController(current.resolve(IdeonService)),
            scope=Scope.TRANSIENT,
        )
        container.bind(
            TaskService,
            lambda current: TaskService(
                current.resolve(PipelineAgent),
                current.resolve_token(TaskRepository),
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            TaskController,
            lambda current: TaskController(current.resolve(TaskService)),
            scope=Scope.TRANSIENT,
        )
        container.bind(
            SearchService,
            lambda current: SearchService(
                current.resolve(SearchManager),
                current.resolve_token(TaskRepository),
                current.resolve_token(TraceRepository),
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            SearchController,
            lambda current: SearchController(current.resolve(SearchService)),
            scope=Scope.TRANSIENT,
        )
        container.bind(
            AgentRoutingService,
            lambda current: AgentRoutingService(current.resolve(DeterministicAgentRouter)),
            scope=Scope.APPLICATION,
        )
        container.bind(
            AgentRoutingController,
            lambda current: AgentRoutingController(current.resolve(AgentRoutingService)),
            scope=Scope.TRANSIENT,
        )
        container.bind(
            LocalDatabaseProvisionerToken,
            lambda _: LocalMariaDBProvisioner(),
            scope=Scope.APPLICATION,
        )
        container.bind(
            DatabaseConnectionVerifierToken,
            lambda _: MariaDBConnectionVerifier(),
            scope=Scope.APPLICATION,
        )
        container.bind(
            ConnectionUrlWriterToken,
            lambda _: AtomicEnvFileWriter(),
            scope=Scope.APPLICATION,
        )
        container.bind(
            DatabaseSetupService,
            lambda current: DatabaseSetupService(
                cast(LocalDatabaseProvisioner, current.resolve(LocalDatabaseProvisionerToken)),
                cast(
                    DatabaseConnectionVerifier,
                    current.resolve(DatabaseConnectionVerifierToken),
                ),
                cast(ConnectionUrlWriter, current.resolve(ConnectionUrlWriterToken)),
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            DatabaseSetupController,
            lambda current: DatabaseSetupController(current.resolve(DatabaseSetupService)),
            scope=Scope.TRANSIENT,
        )
        container.bind(
            ResourceLeaseService,
            lambda current: ResourceLeaseService(current.resolve_token(LeaseRepository)),
            scope=Scope.APPLICATION,
        )
        container.bind(
            WorkerSupervisorFactory,
            lambda current: WorkerSupervisorFactory(
                current.resolve(ResourceLeaseService),
                host_id=current.resolve(Settings).resource_lease_maintenance_host_id,
                maintenance_interval=timedelta(
                    seconds=current.resolve(Settings).resource_lease_maintenance_interval_seconds
                ),
            ),
            scope=Scope.APPLICATION,
        )


class _QueueWorkerProvider(ServiceProvider):
    provider_id = "af.queue-workers"
    dependencies = ("af.application",)

    def __init__(self, settings: Settings, *, profile: str) -> None:
        self._settings = settings
        self._profile = profile

    def register(self, container: ProviderRegistrar) -> None:
        if self._settings.worker_queue_sqlite_path is None:
            return
        container.bind(
            QueueManager,
            lambda _: self._create_manager(),
            scope=Scope.APPLICATION,
        )
        container.bind(
            SqlAlchemyJobEventRepository,
            lambda resolver: SqlAlchemyJobEventRepository(resolver.resolve(Database)),
            scope=Scope.APPLICATION,
        )
        container.bind(
            SqlAlchemyProgramJobResultRepository,
            lambda resolver: SqlAlchemyProgramJobResultRepository(resolver.resolve(Database)),
            scope=Scope.APPLICATION,
        )
        container.bind(
            ProgramJobService,
            lambda resolver: ProgramJobService(
                resolver.resolve(QueueManager),
                resolver.resolve(SqlAlchemyJobEventRepository),
                resolver.resolve(SqlAlchemyProgramJobResultRepository),
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            ProgramJobHandler,
            lambda resolver: ProgramJobHandler(
                AsyncSubprocessExecutor(),
                resolver.resolve(SqlAlchemyJobEventRepository),
                resolver.resolve(SqlAlchemyProgramJobResultRepository),
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            WorkerGenerationFactory,
            lambda _: self._create_factory(),
            scope=Scope.APPLICATION,
        )

    def boot(
        self,
        container: Container,
        resources: ProviderResourceRegistry,
    ) -> None:
        del resources
        if self._settings.worker_queue_sqlite_path is not None:
            container.resolve(ProgramJobService)
            container.resolve(ProgramJobHandler)
            container.resolve(WorkerGenerationFactory)

    def _create_manager(self) -> QueueManager:
        queue_path = self._settings.worker_queue_sqlite_path
        if queue_path is None:
            raise InvalidProviderConfiguration("worker queue configuration is incomplete")
        storage_identity = worker_queue_storage_identity(queue_path)
        registration = database_queue_registration(
            database_factory=lambda: SQLiteDriver(Path(queue_path)),
            storage_identity=storage_identity,
        )
        manager = QueueManager(
            connections={
                "primary": {
                    "driver": "database",
                    "storage_identity": storage_identity,
                }
            },
            default="primary",
            deployment_profile=self._profile,
            policy_version="af-worker-v1",
        )
        manager.install(registration)
        return manager

    def _create_factory(self) -> WorkerGenerationFactory:
        queue_path = self._settings.worker_queue_sqlite_path
        profile_path = self._settings.worker_optimization_profile_path
        if queue_path is None or profile_path is None:
            raise InvalidProviderConfiguration("worker queue configuration is incomplete")

        def create_manager() -> QueueManager:
            return self._create_manager()

        optimizer = load_worker_optimization_profile(profile_path)
        probe = create_manager()
        try:
            reference = probe.reference()
            binding = probe.binding()
        finally:
            probe.close()
        if optimizer.driver_name != reference.driver_name or optimizer.binding != binding:
            raise InvalidProviderConfiguration(
                "worker optimization profile does not match the configured backend"
            )
        return WorkerGenerationFactory(
            create_manager,
            optimizers={optimizer.driver_name: optimizer},
        )


@dataclass(frozen=True, slots=True)
class AFApplication:
    settings: Settings
    config: ConfigSnapshot
    container: Container
    agent: PipelineAgent
    bus: ApplicationBus
    tasks: TaskController
    search: SearchController
    traces: TraceRepository
    leases: ResourceLeaseService
    worker_supervisors: WorkerSupervisorFactory
    verifier: DeterministicVerifier
    agent_routes: AgentRoutingController
    ideons: IdeonController
    operations: OperationsController
    program_jobs: ProgramJobService | None
    program_job_handler: ProgramJobHandler | None
    workflows: WorkflowService
    lifecycle: Application

    @property
    def streams(self) -> SQLiteStreamDriver:
        """Lazily open the reference persistent stream adapter."""
        return self.container.resolve(SQLiteStreamDriver)

    def shutdown(self) -> None:
        _shutdown_lifecycle_at_sync_boundary(self.lifecycle)

    async def ashutdown(self) -> None:
        await self.lifecycle.ashutdown()


@dataclass(frozen=True, slots=True)
class _ApplicationBuild:
    settings: Settings
    config: ConfigSnapshot
    lifecycle: Application


def create_application(
    *,
    settings: Settings | None = None,
    config: ConfigSnapshot | None = None,
    task_repository: TaskRepository | None = None,
    trace_repository: TraceRepository | None = None,
    lease_repository: LeaseRepository | None = None,
    ideon_service: IdeonService | None = None,
    reuse_store: ReuseStore | None = None,
    selector: Selector | None = None,
    classifier_hierarchy: HierarchyRouter | None = None,
    providers: Iterable[ServiceProvider] = (),
) -> AFApplication:
    """Build an isolated application; tests and hosts can replace any provider."""
    _assert_sync_bootstrap_context()
    build = _prepare_application(
        settings=settings,
        config=config,
        task_repository=task_repository,
        trace_repository=trace_repository,
        lease_repository=lease_repository,
        ideon_service=ideon_service,
        reuse_store=reuse_store,
        selector=selector,
        classifier_hierarchy=classifier_hierarchy,
        providers=providers,
    )
    try:
        build.lifecycle.start()
        return _assemble_application(build)
    except BaseException:
        with suppress(BaseException):
            _shutdown_lifecycle_at_sync_boundary(build.lifecycle)
        raise


async def create_application_async(
    *,
    settings: Settings | None = None,
    config: ConfigSnapshot | None = None,
    task_repository: TaskRepository | None = None,
    trace_repository: TraceRepository | None = None,
    lease_repository: LeaseRepository | None = None,
    ideon_service: IdeonService | None = None,
    reuse_store: ReuseStore | None = None,
    selector: Selector | None = None,
    classifier_hierarchy: HierarchyRouter | None = None,
    providers: Iterable[ServiceProvider] = (),
) -> AFApplication:
    """Build an application whose providers may use asynchronous lifecycle hooks."""
    build = _prepare_application(
        settings=settings,
        config=config,
        task_repository=task_repository,
        trace_repository=trace_repository,
        lease_repository=lease_repository,
        ideon_service=ideon_service,
        reuse_store=reuse_store,
        selector=selector,
        classifier_hierarchy=classifier_hierarchy,
        providers=providers,
    )
    try:
        await build.lifecycle.astart()
        return _assemble_application(build)
    except BaseException:
        try:
            await build.lifecycle.ashutdown()
        except asyncio.CancelledError:
            raise
        except BaseException:
            pass
        raise


def _prepare_application(
    *,
    settings: Settings | None,
    config: ConfigSnapshot | None,
    task_repository: TaskRepository | None,
    trace_repository: TraceRepository | None,
    lease_repository: LeaseRepository | None,
    ideon_service: IdeonService | None,
    reuse_store: ReuseStore | None,
    selector: Selector | None,
    classifier_hierarchy: HierarchyRouter | None,
    providers: Iterable[ServiceProvider],
) -> _ApplicationBuild:
    if classifier_hierarchy is not None and type(classifier_hierarchy) is not HierarchyRouter:
        raise TypeError("classifier hierarchy must be a configured HierarchyRouter")
    if settings is not None and config is not None:
        raise ValueError("provide settings or config, not both")
    resolved_config = (
        config
        if config is not None
        else ConfigSnapshot.explicit(settings)
        if settings is not None
        else load_settings(EnvironmentConfigSource())
    )
    resolved_settings = resolved_config.settings
    if selector is None and resolved_settings.selector_provider != "disabled":
        try:
            selector = LocalModelSelector(
                model=resolved_settings.selector_model or "",
                endpoint=resolved_settings.selector_endpoint,
                protocol=resolved_settings.selector_provider,
                timeout=resolved_settings.selector_timeout_seconds,
            )
        except ValueError:
            raise InvalidProviderConfiguration("local selector configuration is invalid") from None
    selector_metadata = _resolve_selector_metadata(selector)
    selector_confidence_threshold = _resolve_selector_confidence_threshold(
        resolved_settings.selector_confidence_threshold
    )
    if (
        selector_metadata is not None
        and selector_metadata.locality is SelectorLocality.REMOTE
        and not resolved_settings.allow_remote_inference
    ):
        raise InvalidProviderConfiguration(
            "remote selector requires AF_ALLOW_REMOTE_INFERENCE=true"
        )
    root = Container()
    root.instance(Settings, resolved_settings)
    root.instance(ConfigSnapshot, resolved_config)
    application_providers: tuple[ServiceProvider, ...] = (
        _PersistenceProvider(
            resolved_settings, task_repository, trace_repository, lease_repository
        ),
        _FrameworkProvider(
            reuse_store,
            selector,
            selector_metadata,
            selector_confidence_threshold,
            classifier_hierarchy,
        ),
        ScienceProvider(),
        CatalogueProvider(),
        _IdeonProvider(ideon_service),
        _SearchProvider(
            resolved_settings,
            profile=(
                resolved_config.profile.value if resolved_config.profile is not None else "local"
            ),
            ephemeral=task_repository is not None or trace_repository is not None,
        ),
        _ApplicationProvider(
            self_development_configured=resolved_settings.self_development_unit is not None
        ),
        _QueueWorkerProvider(
            resolved_settings,
            profile=(
                resolved_config.profile.value if resolved_config.profile is not None else "local"
            ),
        ),
        *(
            (
                LocalEngineeringProvider(
                    Path(resolved_settings.engineering_repository).expanduser().resolve(),
                    selector_configured=selector is not None,
                ),
            )
            if resolved_settings.engineering_repository is not None
            else ()
        ),
        *(
            (
                LocalSelfDevelopmentProvider(
                    Path(resolved_settings.engineering_repository or "").expanduser().resolve(),
                    Path(resolved_settings.self_development_unit).expanduser().resolve(),
                ),
            )
            if resolved_settings.self_development_unit is not None
            else ()
        ),
        *tuple(providers),
    )
    lifecycle = Application(root, application_providers)
    return _ApplicationBuild(resolved_settings, resolved_config, lifecycle)


def diagnose_application(settings: Settings) -> dict[str, object]:
    """Compose non-mutating local checks without booting the application."""
    return diagnose(settings)


def _shutdown_lifecycle_at_sync_boundary(lifecycle: Application) -> None:
    try:
        lifecycle.shutdown()
        return
    except AsyncLifecycleRequired:
        pass
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(lifecycle.ashutdown())
        return
    raise AsyncLifecycleRequired(
        "application shutdown requires awaiting AFApplication.ashutdown"
    ) from None


def _assemble_application(build: _ApplicationBuild) -> AFApplication:
    container = build.lifecycle.container
    operations = OperationsController(
        OperationsService(
            RepositoryOperationsTaskReader(container.resolve_token(TaskRepository)),
            lifecycle_state=lambda: build.lifecycle.state,
        )
    )
    endpoints = build_application_endpoints(
        container.resolve(TaskController),
        container.resolve(AgentRoutingController),
        container.resolve(IdeonController),
        operations,
        middleware=(
            ObservationMiddleware(
                inspector=container.resolve(ObservabilityProducts).inspector,
                metrics=container.resolve(ObservabilityProducts).metrics,
            ),
        )
        if build.settings.inspector_enabled or build.settings.metrics_enabled
        else (),
    )
    return AFApplication(
        settings=build.settings,
        config=build.config,
        container=container,
        agent=container.resolve(PipelineAgent),
        bus=endpoints.bus,
        tasks=endpoints.tasks,
        search=container.resolve(SearchController),
        traces=container.resolve_token(TraceRepository),
        leases=container.resolve(ResourceLeaseService),
        worker_supervisors=container.resolve(WorkerSupervisorFactory),
        verifier=container.resolve(DeterministicVerifier),
        agent_routes=endpoints.agent_routes,
        ideons=endpoints.ideons,
        operations=cast(OperationsController, endpoints.operations),
        program_jobs=(
            container.resolve(ProgramJobService)
            if build.settings.worker_queue_sqlite_path is not None
            else None
        ),
        program_job_handler=(
            container.resolve(ProgramJobHandler)
            if build.settings.worker_queue_sqlite_path is not None
            else None
        ),
        workflows=container.resolve(WorkflowService),
        lifecycle=build.lifecycle,
    )


def _search_database(settings: Settings, *, ephemeral: bool) -> str:
    if ephemeral:
        return ":memory:"
    parsed = make_url(settings.database_url)
    database_name = parsed.database
    if (
        parsed.get_backend_name() == "sqlite"
        and isinstance(database_name, str)
        and database_name not in ("", ":memory:")
    ):
        database_path = Path(database_name).expanduser()
        suffix = database_path.suffix or ".sqlite3"
        stem = database_path.stem or "af"
        return str(database_path.with_name(f"{stem}-search{suffix}"))
    return "storage/af-search.sqlite3"


def _resolve_selector_metadata(selector: Selector | None) -> SelectorMetadata | None:
    if selector is None:
        return None
    # Raise after the except block so the sanitized configuration error keeps no
    # provider exception context or cause.
    metadata_is_invalid = False
    try:
        provider_id = selector.name
        locality = selector.locality
    except Exception:
        metadata_is_invalid = True
        provider_id = ""
        locality = None
    if metadata_is_invalid:
        raise InvalidProviderConfiguration("selector provider metadata is invalid")
    if not is_valid_selector_provider_id(provider_id) or not isinstance(locality, SelectorLocality):
        raise InvalidProviderConfiguration("selector provider metadata is invalid")
    return SelectorMetadata(provider_id=provider_id, locality=locality)


def _resolve_selector_confidence_threshold(value: object) -> float:
    threshold = parse_probability(value)
    if threshold is None:
        raise InvalidProviderConfiguration(
            "selector confidence threshold must be a finite probability in [0, 1]"
        )
    return threshold


def _assert_sync_bootstrap_context() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise AsyncLifecycleRequired(
        "create_application cannot run inside an event loop; use create_application_async"
    )


def create_offline_improvement_service(application: AFApplication) -> SelfImprovementService:
    """Explicit fixture composition; no repository mutation or publication authority."""
    return create_fixture_self_improvement_service(
        application.container.resolve(Database),
        authentication_key=application.container.resolve(LocalAuthenticationKey).value,
    )
