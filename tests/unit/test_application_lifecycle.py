from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import pytest

from af.framework.application import Application, ApplicationState, ProviderBootContext
from af.framework.container import Container, Scope, ServiceProvider
from af.framework.errors import (
    ApplicationBootError,
    ApplicationGraphError,
    ApplicationRegistrationError,
    AsyncLifecycleRequired,
    ScopeDisposed,
)


class _Value:
    pass


class _RecordingProvider(ServiceProvider):
    def __init__(
        self,
        provider_id: str,
        events: list[str],
        *,
        dependencies: tuple[str, ...] = (),
        deferred: bool = False,
        fail_boot: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self.dependencies = dependencies
        self.deferred = deferred
        self._events = events
        self._fail_boot = fail_boot

    def register(self, container: Container) -> None:
        self._events.append(f"register:{self.provider_id}")
        container.bind(
            f"value:{self.provider_id}",
            lambda _: _Value(),
            scope=Scope.APPLICATION,
        )

    def boot(self, container: Container, resources: ProviderBootContext) -> None:
        self._events.append(f"boot:{self.provider_id}")
        assert isinstance(container.resolve_token(f"value:{self.provider_id}"), _Value)
        resources.own(_RecordingResource(self._events, self.provider_id))
        if self._fail_boot:
            raise RuntimeError("private provider boot failure")

    def shutdown(self, container: Container) -> None:
        self._events.append(f"shutdown:{self.provider_id}")
        assert isinstance(container.resolve_token(f"value:{self.provider_id}"), _Value)


class _RecordingResource:
    def __init__(self, events: list[str], provider_id: str) -> None:
        self._events = events
        self._provider_id = provider_id

    def close(self) -> None:
        self._events.append(f"resource:{self._provider_id}")


def test_application_registers_all_providers_before_topological_boot() -> None:
    events: list[str] = []
    dependent = _RecordingProvider("dependent", events, dependencies=("foundation",))
    foundation = _RecordingProvider("foundation", events)
    application = Application(Container(), (dependent, foundation))

    application.start()

    assert application.state is ApplicationState.SERVING
    assert application.provider_order == ("foundation", "dependent")
    assert events == [
        "register:foundation",
        "register:dependent",
        "boot:foundation",
        "boot:dependent",
    ]
    application.shutdown()


def test_provider_cycle_fails_before_any_registration() -> None:
    events: list[str] = []
    application = Application(
        Container(),
        (
            _RecordingProvider("first", events, dependencies=("second",)),
            _RecordingProvider("second", events, dependencies=("first",)),
        ),
    )

    with pytest.raises(ApplicationGraphError, match="provider dependency graph"):
        application.register()

    assert events == []
    assert application.state is ApplicationState.FAILED


def test_provider_registration_cannot_resolve_or_construct_services() -> None:
    constructed = False

    class ResolvingProvider(ServiceProvider):
        provider_id = "resolving"

        def register(self, container: Container) -> None:
            nonlocal constructed

            def construct(_: Container) -> _Value:
                nonlocal constructed
                constructed = True
                return _Value()

            container.bind(_Value, construct)
            container.resolve(_Value)

    application = Application(Container(), (ResolvingProvider(),))

    with pytest.raises(ApplicationRegistrationError, match="provider registration failed"):
        application.register()

    assert constructed is False
    assert application.state is ApplicationState.FAILED


def test_partial_boot_cleans_failing_provider_then_prior_providers_once() -> None:
    events: list[str] = []
    application = Application(
        Container(),
        (
            _RecordingProvider("first", events),
            _RecordingProvider(
                "second",
                events,
                dependencies=("first",),
                fail_boot=True,
            ),
        ),
    )

    with pytest.raises(ApplicationBootError, match="application boot failed") as captured:
        application.start()

    assert captured.value.provider_id == "second"
    assert "private provider boot failure" not in str(captured.value)
    assert events == [
        "register:first",
        "register:second",
        "boot:first",
        "boot:second",
        "shutdown:second",
        "resource:second",
        "shutdown:first",
        "resource:first",
    ]
    assert application.state is ApplicationState.FAILED
    with pytest.raises(ScopeDisposed):
        application.container.resolve_token("value:first")
    application.shutdown()
    assert events.count("shutdown:first") == 1
    assert events.count("shutdown:second") == 1


def test_shutdown_is_reverse_order_and_idempotent() -> None:
    events: list[str] = []
    application = Application(
        Container(),
        (
            _RecordingProvider("first", events),
            _RecordingProvider("second", events, dependencies=("first",)),
        ),
    )
    application.start()

    application.drain()
    assert application.state is ApplicationState.DRAINING
    application.shutdown()
    application.shutdown()

    assert application.state is ApplicationState.SHUTDOWN
    assert events[-4:] == [
        "shutdown:second",
        "resource:second",
        "shutdown:first",
        "resource:first",
    ]


def test_deferred_provider_registers_but_boots_only_when_activated() -> None:
    events: list[str] = []
    application = Application(
        Container(),
        (
            _RecordingProvider("foundation", events),
            _RecordingProvider(
                "deferred",
                events,
                dependencies=("foundation",),
                deferred=True,
            ),
        ),
    )
    application.register()
    application.boot()

    assert events == [
        "register:foundation",
        "register:deferred",
        "boot:foundation",
    ]

    application.activate("deferred")
    application.activate("deferred")
    application.serve()

    assert events.count("boot:deferred") == 1
    application.shutdown()


def test_sync_lifecycle_rejects_async_hooks_without_starting_an_event_loop() -> None:
    awaited = False

    class AsyncProvider(ServiceProvider):
        provider_id = "async"

        def register(self, container: Container) -> None:
            del container

        def boot(
            self,
            container: Container,
            resources: ProviderBootContext,
        ) -> Awaitable[None]:
            del container, resources

            async def run() -> None:
                nonlocal awaited
                awaited = True

            return run()

    application = Application(Container(), (AsyncProvider(),))

    with pytest.raises(AsyncLifecycleRequired):
        application.start()

    assert awaited is False


def test_async_lifecycle_boots_and_shuts_down_async_provider() -> None:
    events: list[str] = []

    class AsyncResource:
        async def aclose(self) -> None:
            events.append("resource")

    class AsyncProvider(ServiceProvider):
        provider_id = "async"

        def register(self, container: Container) -> None:
            events.append("register")
            container.bind(_Value, lambda _: _Value(), scope=Scope.APPLICATION)

        async def boot(
            self,
            container: Container,
            resources: ProviderBootContext,
        ) -> None:
            assert isinstance(await container.resolve_async(_Value), _Value)
            events.append("boot")
            resources.own(AsyncResource())

        async def shutdown(self, container: Container) -> None:
            assert isinstance(await container.resolve_async(_Value), _Value)
            events.append("shutdown")

    async def scenario() -> None:
        application = Application(Container(), (AsyncProvider(),))
        await application.astart()
        assert application.state is ApplicationState.SERVING
        await application.ashutdown()
        await application.ashutdown()

    asyncio.run(scenario())

    assert events == ["register", "boot", "shutdown", "resource"]
