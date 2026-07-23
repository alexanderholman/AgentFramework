"""Isolated application host and deterministic test overrides."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import TracebackType
from typing import Self, cast

from af.application.bus import (
    ApplicationBus,
    ApplicationResult,
    Handler,
    HandlerRegistry,
    InvalidDispatchContract,
    Message,
    Middleware,
)
from af.application.middleware import InProcessTestAdapter
from af.bootstrap import AFApplication, create_application, create_application_async
from af.config import ConfigSnapshot, Settings
from af.framework.application import ApplicationState
from af.framework.container import (
    Container,
    ProviderRegistrar,
    Qualifier,
    Scope,
    ServiceProvider,
)
from af.framework.context import ExecutionContext

__all__ = (
    "BindingOverride",
    "TestApplication",
    "TestApplicationBuilder",
)

_CORE_PROVIDER_IDS = (
    "af.persistence",
    "af.framework",
    "af.ideons",
    "af.application",
)
_BUS_PROVIDER_ID = "af.testing.bus"
_OVERRIDE_PROVIDER_ID = "af.testing.overrides"
_MAX_DECLARATIONS = 256


@dataclass(frozen=True, slots=True, repr=False)
class BindingOverride:
    """One explicit replacement binding installed after application providers."""

    token: object
    value: object
    qualifier: Qualifier | str | None = None
    owned: bool = False

    def __post_init__(self) -> None:
        if type(self.owned) is not bool:
            raise ValueError("test override ownership must be boolean")

    def __repr__(self) -> str:
        return (
            "BindingOverride(token='[REDACTED]', value='[REDACTED]', "
            f"qualifier={self.qualifier!r}, owned={self.owned!r})"
        )


def _value_factory(value: object) -> Callable[[object], object]:
    def factory(_: object) -> object:
        return value

    return factory


class _BusProvider(ServiceProvider):
    provider_id = _BUS_PROVIDER_ID

    def __init__(
        self,
        handlers: tuple[tuple[type[Message], Handler], ...],
        middleware: tuple[Middleware, ...],
        dependencies: tuple[str, ...],
    ) -> None:
        self.dependencies = dependencies
        self._handlers = handlers
        self._middleware = middleware

    def register(self, container: ProviderRegistrar) -> None:
        registry = HandlerRegistry()
        for message_type, handler in self._handlers:
            registry.register(message_type, handler)
        middleware = self._middleware
        container.bind(
            HandlerRegistry,
            _value_factory(registry),
            scope=Scope.APPLICATION,
            owned=False,
        )
        container.bind(
            ApplicationBus,
            lambda current: ApplicationBus(
                current.resolve(HandlerRegistry),
                middleware=middleware,
            ),
            scope=Scope.APPLICATION,
        )
        container.bind(
            InProcessTestAdapter,
            lambda current: InProcessTestAdapter(current.resolve(ApplicationBus)),
            scope=Scope.APPLICATION,
        )


class _OverrideProvider(ServiceProvider):
    provider_id = _OVERRIDE_PROVIDER_ID
    dependencies = (_BUS_PROVIDER_ID,)

    def __init__(self, overrides: tuple[BindingOverride, ...]) -> None:
        self._overrides = overrides

    def register(self, container: ProviderRegistrar) -> None:
        for override in self._overrides:
            container.bind(
                override.token,
                _value_factory(override.value),
                qualifier=override.qualifier,
                scope=Scope.APPLICATION,
                owned=override.owned,
                replace=True,
            )


class TestApplication:
    """Owned isolated application used by framework and extension tests."""

    def __init__(self, application: AFApplication) -> None:
        if not isinstance(application, AFApplication):
            raise TypeError("test application requires an AFApplication")
        self._application = application

    @property
    def application(self) -> AFApplication:
        return self._application

    @property
    def container(self) -> Container:
        return self._application.container

    @property
    def state(self) -> ApplicationState:
        return self._application.lifecycle.state

    def resolve[T](self, token: type[T], *, qualifier: Qualifier | str | None = None) -> T:
        return self.container.resolve(token, qualifier=qualifier)

    def resolve_token(
        self,
        token: object,
        *,
        qualifier: Qualifier | str | None = None,
    ) -> object:
        return cast(object, self.container.resolve_token(token, qualifier=qualifier))

    async def dispatch(
        self,
        message: object,
        *,
        context: ExecutionContext,
    ) -> ApplicationResult[object]:
        adapter = self.resolve(InProcessTestAdapter)
        return await adapter.dispatch(message, context=context)

    def shutdown(self) -> None:
        self._application.shutdown()

    async def ashutdown(self) -> None:
        await self._application.ashutdown()

    def assert_clean(self) -> None:
        lifecycle = self._application.lifecycle
        if lifecycle.state is not ApplicationState.SHUTDOWN:
            raise AssertionError("test application has not completed shutdown")
        if lifecycle.cleanup_failures:
            raise AssertionError("test application reported cleanup failures")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.shutdown()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.ashutdown()


class TestApplicationBuilder:
    """Build fresh AF applications with explicit providers, handlers, and overrides."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        config: ConfigSnapshot | None = None,
    ) -> None:
        if settings is not None and config is not None:
            raise ValueError("provide test settings or config, not both")
        self._settings = settings or (None if config is not None else Settings())
        self._config = config
        self._providers: list[ServiceProvider] = []
        self._handlers: list[tuple[type[Message], Handler]] = []
        self._handler_types: set[type[Message]] = set()
        self._middleware: list[Middleware] = []
        self._overrides: list[BindingOverride] = []

    def provider(self, provider: ServiceProvider) -> Self:
        if not isinstance(provider, ServiceProvider):
            raise TypeError("test provider must be a ServiceProvider")
        self._append_bounded(self._providers, provider, "test providers")
        return self

    def providers(self, providers: Iterable[ServiceProvider]) -> Self:
        for provider in providers:
            self.provider(provider)
        return self

    def handler(self, message_type: type[Message], handler: Handler) -> Self:
        if not callable(handler):
            raise InvalidDispatchContract("test message handler must be callable")
        if message_type in self._handler_types:
            raise InvalidDispatchContract("test message handler is already registered")
        self._append_bounded(self._handlers, (message_type, handler), "test handlers")
        self._handler_types.add(message_type)
        return self

    def middleware(self, middleware: Middleware) -> Self:
        if not callable(middleware):
            raise InvalidDispatchContract("test application middleware must be callable")
        self._append_bounded(self._middleware, middleware, "test middleware")
        return self

    def override(
        self,
        token: object,
        value: object,
        *,
        qualifier: Qualifier | str | None = None,
        owned: bool = False,
    ) -> Self:
        override = BindingOverride(token, value, qualifier, owned)
        self._append_bounded(self._overrides, override, "test overrides")
        return self

    def build(self) -> TestApplication:
        application = create_application(
            settings=self._settings,
            config=self._config,
            providers=self._composition_providers(),
        )
        return TestApplication(application)

    async def abuild(self) -> TestApplication:
        application = await create_application_async(
            settings=self._settings,
            config=self._config,
            providers=self._composition_providers(),
        )
        return TestApplication(application)

    def _composition_providers(self) -> tuple[ServiceProvider, ...]:
        declared = tuple(self._providers)
        custom_ids: list[str] = []
        for provider in declared:
            provider_id = getattr(provider, "provider_id", None)
            if type(provider_id) is str and provider_id:
                custom_ids.append(provider_id)
        bus = _BusProvider(
            tuple(self._handlers),
            tuple(self._middleware),
            (*_CORE_PROVIDER_IDS, *custom_ids),
        )
        overrides = _OverrideProvider(tuple(self._overrides))
        return (*declared, bus, overrides)

    @staticmethod
    def _append_bounded[T](values: list[T], value: T, name: str) -> None:
        if len(values) >= _MAX_DECLARATIONS:
            raise ValueError(f"{name} exceed the supported limit")
        values.append(value)
