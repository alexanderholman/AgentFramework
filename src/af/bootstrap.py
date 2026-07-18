"""Composition root and service providers for AF."""

from __future__ import annotations

from dataclasses import dataclass

from af.application.tasks import TaskController, TaskService
from af.config import Settings
from af.domain.classifiers import (
    CharacterCountClassifier,
    ClassifierChain,
    HelloClassifier,
    UnknownClassifier,
)
from af.domain.procedures import CharacterCountProcedure, HelloProcedure, ModelProcedure
from af.domain.tasks import TaskRepository
from af.domain.verification import DeterministicVerifier, VerifierRouter
from af.framework.container import Container, Scope, ServiceProvider
from af.framework.contracts import ModelProvider, ReuseStore, TraceRepository, Verifier
from af.framework.errors import InvalidProviderConfiguration
from af.framework.pipeline import PipelineAgent
from af.framework.reuse import InMemoryReuseStore, ReplayRegressionGate
from af.framework.routing import LadderRouter, ProcedureRegistry, RoutingPolicy
from af.infrastructure.database import Database
from af.infrastructure.repositories import SqlAlchemyTaskRepository
from af.infrastructure.tracing import SqlAlchemyTraceRepository


class _PersistenceProvider(ServiceProvider):
    def __init__(
        self,
        settings: Settings,
        task_repository: TaskRepository | None,
        trace_repository: TraceRepository | None,
    ) -> None:
        self._settings = settings
        self._task_repository = task_repository
        self._trace_repository = trace_repository

    def register(self, container: Container) -> None:
        if self._task_repository is not None and self._trace_repository is not None:
            container.instance(TaskRepository, self._task_repository)
            container.instance(TraceRepository, self._trace_repository)
            return

        database = Database(self._settings.database_url)
        container.instance(Database, database)
        container.instance(
            TaskRepository,
            self._task_repository or SqlAlchemyTaskRepository(database),
        )
        container.instance(
            TraceRepository,
            self._trace_repository or SqlAlchemyTraceRepository(database),
        )


class _FrameworkProvider(ServiceProvider):
    def __init__(
        self,
        reuse_store: ReuseStore | None,
        model_provider: ModelProvider | None,
        model_verifier: Verifier | None,
    ) -> None:
        self._reuse_store = reuse_store
        self._model_provider = model_provider
        self._model_verifier = model_verifier

    def register(self, container: Container) -> None:
        registry = ProcedureRegistry()
        registry.register(HelloProcedure())
        registry.register(CharacterCountProcedure())
        if self._model_provider is not None:
            registry.register(ModelProcedure(self._model_provider))
        classifier = ClassifierChain(
            [HelloClassifier(), CharacterCountClassifier(), UnknownClassifier()]
        )
        verifier = DeterministicVerifier()
        verifier_router = VerifierRouter(verifier, self._model_verifier)
        reuse_store = self._reuse_store or InMemoryReuseStore()
        traces = container.resolve_token(TraceRepository)
        router = LadderRouter(registry, RoutingPolicy())
        agent = PipelineAgent(
            classifier=classifier,
            router=router,
            verifier=verifier_router,
            traces=traces,
            regression_gate=ReplayRegressionGate(),
            reuse_store=reuse_store,
        )
        container.instance(ProcedureRegistry, registry)
        container.instance(DeterministicVerifier, verifier)
        container.instance(ReuseStore, reuse_store)
        container.instance(PipelineAgent, agent)


class _ApplicationProvider(ServiceProvider):
    def register(self, container: Container) -> None:
        container.bind(
            TaskService,
            lambda current: TaskService(
                current.resolve(PipelineAgent),
                current.resolve_token(TaskRepository),
            ),
            scope=Scope.SINGLETON,
        )
        container.bind(
            TaskController,
            lambda current: TaskController(current.resolve(TaskService)),
            scope=Scope.TRANSIENT,
        )


@dataclass(frozen=True, slots=True)
class AFApplication:
    settings: Settings
    container: Container
    agent: PipelineAgent
    tasks: TaskController
    traces: TraceRepository
    verifier: DeterministicVerifier


def create_application(
    *,
    settings: Settings | None = None,
    task_repository: TaskRepository | None = None,
    trace_repository: TraceRepository | None = None,
    reuse_store: ReuseStore | None = None,
    model_provider: ModelProvider | None = None,
    model_verifier: Verifier | None = None,
) -> AFApplication:
    """Build an isolated application; tests and hosts can replace any provider."""
    if (model_provider is None) != (model_verifier is None):
        raise InvalidProviderConfiguration(
            "model_provider and model_verifier must be installed together"
        )
    resolved_settings = settings or Settings.from_environment()
    container = Container()
    container.instance(Settings, resolved_settings)
    providers: tuple[ServiceProvider, ...] = (
        _PersistenceProvider(resolved_settings, task_repository, trace_repository),
        _FrameworkProvider(reuse_store, model_provider, model_verifier),
        _ApplicationProvider(),
    )
    for provider in providers:
        provider.register(container)
    for provider in providers:
        provider.boot(container)

    return AFApplication(
        settings=resolved_settings,
        container=container,
        agent=container.resolve(PipelineAgent),
        tasks=container.resolve(TaskController),
        traces=container.resolve_token(TraceRepository),
        verifier=container.resolve(DeterministicVerifier),
    )
