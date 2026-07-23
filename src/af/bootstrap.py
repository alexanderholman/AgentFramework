"""Composition root and service providers for AF."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass

from af.application.agent_routing import AgentRoutingController, AgentRoutingService
from af.application.ideons import IdeonController, IdeonService
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
from af.framework.application import Application
from af.framework.container import (
    Container,
    ProviderRegistrar,
    ProviderResourceRegistry,
    Scope,
    ServiceProvider,
)
from af.framework.contracts import ReuseStore, Selector, TraceRepository
from af.framework.errors import AsyncLifecycleRequired, InvalidProviderConfiguration
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
from af.infrastructure.ideon_reference import create_reference_ideon_runtime
from af.infrastructure.repositories import SqlAlchemyTaskRepository
from af.infrastructure.tracing import SqlAlchemyTraceRepository


class _PersistenceProvider(ServiceProvider):
    provider_id = "af.persistence"

    def __init__(
        self,
        settings: Settings,
        task_repository: TaskRepository | None,
        trace_repository: TraceRepository | None,
    ) -> None:
        self._settings = settings
        self._task_repository = task_repository
        self._trace_repository = trace_repository

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
        if self._task_repository is not None and self._trace_repository is not None:
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
    ) -> None:
        self._reuse_store = reuse_store
        self._selector = selector
        self._selector_metadata = selector_metadata
        self._selector_confidence_threshold = selector_confidence_threshold

    def register(self, container: ProviderRegistrar) -> None:
        def create_registry(_: object) -> ProcedureRegistry:
            registry = ProcedureRegistry()
            registry.register(HelloProcedure())
            registry.register(CharacterCountProcedure())
            return registry

        def create_classifier(_: object) -> ClassifierChain:
            return ClassifierChain(
                [HelloClassifier(), CharacterCountClassifier(), UnknownClassifier()]
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
            lambda _: DeterministicVerifier(),
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


class _IdeonProvider(ServiceProvider):
    provider_id = "af.ideons"

    def __init__(self, service: IdeonService | None) -> None:
        self._service = service

    def register(self, container: ProviderRegistrar) -> None:
        service = self._service or create_reference_ideon_runtime().service
        container.bind(
            IdeonService,
            lambda _: service,
            scope=Scope.APPLICATION,
            owned=False,
        )


class _ApplicationProvider(ServiceProvider):
    provider_id = "af.application"
    dependencies = ("af.framework", "af.ideons")

    def register(self, container: ProviderRegistrar) -> None:
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
            AgentRoutingService,
            lambda current: AgentRoutingService(current.resolve(DeterministicAgentRouter)),
            scope=Scope.APPLICATION,
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
    ideons: IdeonController
    lifecycle: Application

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
    ideon_service: IdeonService | None = None,
    reuse_store: ReuseStore | None = None,
    selector: Selector | None = None,
    providers: Iterable[ServiceProvider] = (),
) -> AFApplication:
    """Build an isolated application; tests and hosts can replace any provider."""
    _assert_sync_bootstrap_context()
    build = _prepare_application(
        settings=settings,
        config=config,
        task_repository=task_repository,
        trace_repository=trace_repository,
        ideon_service=ideon_service,
        reuse_store=reuse_store,
        selector=selector,
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
    ideon_service: IdeonService | None = None,
    reuse_store: ReuseStore | None = None,
    selector: Selector | None = None,
    providers: Iterable[ServiceProvider] = (),
) -> AFApplication:
    """Build an application whose providers may use asynchronous lifecycle hooks."""
    build = _prepare_application(
        settings=settings,
        config=config,
        task_repository=task_repository,
        trace_repository=trace_repository,
        ideon_service=ideon_service,
        reuse_store=reuse_store,
        selector=selector,
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
    ideon_service: IdeonService | None,
    reuse_store: ReuseStore | None,
    selector: Selector | None,
    providers: Iterable[ServiceProvider],
) -> _ApplicationBuild:
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
    root = Container()
    root.instance(Settings, resolved_settings)
    root.instance(ConfigSnapshot, resolved_config)
    application_providers: tuple[ServiceProvider, ...] = (
        _PersistenceProvider(resolved_settings, task_repository, trace_repository),
        _FrameworkProvider(
            reuse_store,
            selector,
            selector_metadata,
            selector_confidence_threshold,
        ),
        _IdeonProvider(ideon_service),
        _ApplicationProvider(),
        *tuple(providers),
    )
    lifecycle = Application(root, application_providers)
    return _ApplicationBuild(resolved_settings, resolved_config, lifecycle)


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
    return AFApplication(
        settings=build.settings,
        config=build.config,
        container=container,
        agent=container.resolve(PipelineAgent),
        tasks=container.resolve(TaskController),
        traces=container.resolve_token(TraceRepository),
        verifier=container.resolve(DeterministicVerifier),
        agent_routes=container.resolve(AgentRoutingController),
        ideons=container.resolve(IdeonController),
        lifecycle=build.lifecycle,
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


def _assert_sync_bootstrap_context() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise AsyncLifecycleRequired(
        "create_application cannot run inside an event loop; use create_application_async"
    )
