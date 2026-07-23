"""Composition root and service providers for AF."""

from __future__ import annotations

from dataclasses import dataclass

from af.application.agent_routing import AgentRoutingController, AgentRoutingService
from af.application.tasks import TaskController, TaskService
from af.config import ConfigSnapshot, EnvironmentConfigSource, Settings, load_settings
from af.domain.agent_routing import DeterministicAgentRouter
from af.domain.classifiers import (
    CharacterCountClassifier,
    ClassifierChain,
    HelloClassifier,
    UnknownClassifier,
)
from af.domain.procedures import CharacterCountProcedure, HelloProcedure
from af.domain.tasks import TaskRepository
from af.domain.verification import DeterministicVerifier
from af.framework.container import Container, Scope, ServiceProvider
from af.framework.contracts import ReuseStore, Selector, TraceRepository
from af.framework.errors import InvalidProviderConfiguration
from af.framework.pipeline import PipelineAgent
from af.framework.reuse import InMemoryReuseStore, ReplayRegressionGate
from af.framework.routing import LadderRouter, ProcedureRegistry, RoutingPolicy
from af.framework.types import (
    SelectorLocality,
    SelectorMetadata,
    is_valid_selector_provider_id,
    parse_probability,
)
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
        selector: Selector | None,
        selector_metadata: SelectorMetadata | None,
        selector_confidence_threshold: float,
    ) -> None:
        self._reuse_store = reuse_store
        self._selector = selector
        self._selector_metadata = selector_metadata
        self._selector_confidence_threshold = selector_confidence_threshold

    def register(self, container: Container) -> None:
        registry = ProcedureRegistry()
        registry.register(HelloProcedure())
        registry.register(CharacterCountProcedure())
        classifier = ClassifierChain(
            [HelloClassifier(), CharacterCountClassifier(), UnknownClassifier()]
        )
        verifier = DeterministicVerifier()
        reuse_store = self._reuse_store or InMemoryReuseStore()
        traces = container.resolve_token(TraceRepository)
        router = LadderRouter(registry, RoutingPolicy())
        agent = PipelineAgent(
            classifier=classifier,
            registry=registry,
            router=router,
            verifier=verifier,
            traces=traces,
            regression_gate=ReplayRegressionGate(),
            reuse_store=reuse_store,
            selector=self._selector,
            selector_metadata=self._selector_metadata,
            selector_confidence_threshold=self._selector_confidence_threshold,
        )
        container.instance(ProcedureRegistry, registry)
        container.instance(DeterministicVerifier, verifier)
        container.instance(ReuseStore, reuse_store)
        container.instance(PipelineAgent, agent)
        container.instance(DeterministicAgentRouter, DeterministicAgentRouter())


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
        container.bind(
            AgentRoutingService,
            lambda current: AgentRoutingService(current.resolve(DeterministicAgentRouter)),
            scope=Scope.SINGLETON,
        )
        container.bind(
            AgentRoutingController,
            lambda current: AgentRoutingController(current.resolve(AgentRoutingService)),
            scope=Scope.TRANSIENT,
        )


@dataclass(frozen=True, slots=True)
class AFApplication:
    settings: Settings
    config: ConfigSnapshot
    container: Container
    agent: PipelineAgent
    tasks: TaskController
    traces: TraceRepository
    verifier: DeterministicVerifier
    agent_routes: AgentRoutingController


def create_application(
    *,
    settings: Settings | None = None,
    config: ConfigSnapshot | None = None,
    task_repository: TaskRepository | None = None,
    trace_repository: TraceRepository | None = None,
    reuse_store: ReuseStore | None = None,
    selector: Selector | None = None,
) -> AFApplication:
    """Build an isolated application; tests and hosts can replace any provider."""
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
    container = Container()
    container.instance(Settings, resolved_settings)
    container.instance(ConfigSnapshot, resolved_config)
    providers: tuple[ServiceProvider, ...] = (
        _PersistenceProvider(resolved_settings, task_repository, trace_repository),
        _FrameworkProvider(
            reuse_store,
            selector,
            selector_metadata,
            selector_confidence_threshold,
        ),
        _ApplicationProvider(),
    )
    for provider in providers:
        provider.register(container)
    container.freeze()
    for provider in providers:
        provider.boot(container)

    return AFApplication(
        settings=resolved_settings,
        config=resolved_config,
        container=container,
        agent=container.resolve(PipelineAgent),
        tasks=container.resolve(TaskController),
        traces=container.resolve_token(TraceRepository),
        verifier=container.resolve(DeterministicVerifier),
        agent_routes=container.resolve(AgentRoutingController),
    )


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
