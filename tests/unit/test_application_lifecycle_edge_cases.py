from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from inspect import CORO_CLOSED, getcoroutinestate
from itertools import permutations
from threading import Event as ThreadEvent
from threading import Thread
from time import monotonic

import pytest

from af.framework.application import Application, ApplicationState, ProviderBootContext
from af.framework.container import (
    Container,
    ProviderRegistrar,
    ProviderResourceRegistry,
    Scope,
    ServiceProvider,
)
from af.framework.context import ExecutionContext, maybe_current_context, use_context
from af.framework.errors import (
    ApplicationBootError,
    ApplicationGraphError,
    ApplicationRegistrationError,
    ApplicationShutdownError,
    AsyncLifecycleRequired,
    InvalidBinding,
    ScopeDisposed,
    ShutdownTimeout,
)


class _RecordingProvider(ServiceProvider):
    def __init__(
        self,
        provider_id: str,
        events: list[str],
        *,
        dependencies: tuple[str, ...] = (),
        deferred: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self.dependencies = dependencies
        self.deferred = deferred
        self._events = events

    def register(self, registrar: ProviderRegistrar) -> None:
        del registrar
        self._events.append(f"register:{self.provider_id}")

    def boot(
        self,
        container: Container,
        resources: ProviderResourceRegistry,
    ) -> None:
        del container, resources
        self._events.append(f"boot:{self.provider_id}")

    def shutdown(self, container: Container) -> None:
        del container
        self._events.append(f"shutdown:{self.provider_id}")


class _AsyncRecordingResource:
    def __init__(self, events: list[str], provider_id: str) -> None:
        self._events = events
        self._provider_id = provider_id

    async def aclose(self) -> None:
        self._events.append(f"resource:{self._provider_id}")


def test_provider_order_uses_canonical_ids_across_input_permutations() -> None:
    expected_order = ("alpha", "middle", "zeta")
    observed_orders: list[tuple[str, ...]] = []
    observed_start_events: list[tuple[str, ...]] = []

    for input_order in permutations(expected_order):
        events: list[str] = []
        application = Application(
            Container(),
            tuple(_RecordingProvider(provider_id, events) for provider_id in input_order),
        )
        application.start()
        observed_orders.append(application.provider_order)
        observed_start_events.append(tuple(events))
        application.shutdown()

    expected_events = (
        "register:alpha",
        "register:middle",
        "register:zeta",
        "boot:alpha",
        "boot:middle",
        "boot:zeta",
    )
    assert observed_orders == [expected_order] * 6
    assert observed_start_events == [expected_events] * 6


@pytest.mark.parametrize("case", ["cycle", "missing", "duplicate", "self"])
def test_invalid_provider_graph_never_runs_registration(case: str) -> None:
    events: list[str] = []
    if case == "cycle":
        providers = (
            _RecordingProvider("first", events, dependencies=("second",)),
            _RecordingProvider("second", events, dependencies=("first",)),
        )
    elif case == "missing":
        providers = (_RecordingProvider("first", events, dependencies=("absent",)),)
    elif case == "duplicate":
        providers = (
            _RecordingProvider("same", events),
            _RecordingProvider("same", events),
        )
    else:
        providers = (_RecordingProvider("first", events, dependencies=("first",)),)
    application = Application(Container(), providers)

    with pytest.raises(ApplicationGraphError, match="provider dependency graph"):
        application.register()

    assert events == []
    assert application.state is ApplicationState.FAILED


def test_captured_root_cannot_resolve_during_provider_registration() -> None:
    root = Container()
    constructions = 0

    def construct(_: object) -> object:
        nonlocal constructions
        constructions += 1
        return object()

    root.bind("existing", construct)

    class CapturedRootProvider(ServiceProvider):
        provider_id = "captured-root"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar
            root.resolve_token("existing")

    application = Application(root, (CapturedRootProvider(),))

    with pytest.raises(ApplicationRegistrationError) as captured:
        application.register()

    assert isinstance(captured.value.protected_cause, InvalidBinding)
    assert constructions == 0
    assert application.state is ApplicationState.FAILED
    with pytest.raises(ScopeDisposed):
        root.resolve_token("existing")


def test_eager_provider_boots_its_deferred_dependency_first() -> None:
    events: list[str] = []
    application = Application(
        Container(),
        (
            _RecordingProvider("eager", events, dependencies=("deferred",)),
            _RecordingProvider("deferred", events, deferred=True),
        ),
    )

    application.start()
    boot_events = [event for event in events if event.startswith("boot:")]
    application.shutdown()

    assert boot_events == ["boot:deferred", "boot:eager"]


def test_async_boot_cancellation_rolls_back_then_reraises_cancellation() -> None:
    events: list[str] = []
    blocking_boot_entered = asyncio.Event()

    class FoundationProvider(ServiceProvider):
        provider_id = "foundation"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar
            events.append("register:foundation")

        async def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container
            events.append("boot:foundation")
            resources.own(_AsyncRecordingResource(events, "foundation"))

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown:foundation")

    class BlockingProvider(ServiceProvider):
        provider_id = "blocking"
        dependencies = ("foundation",)

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar
            events.append("register:blocking")

        async def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container
            events.append("boot:blocking")
            resources.own(_AsyncRecordingResource(events, "blocking"))
            blocking_boot_entered.set()
            await asyncio.Event().wait()

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown:blocking")

    async def scenario() -> None:
        application = Application(
            Container(),
            (BlockingProvider(), FoundationProvider()),
        )
        starting = asyncio.create_task(application.astart())
        boot_entered_waiter = asyncio.create_task(blocking_boot_entered.wait())
        done, _ = await asyncio.wait(
            {starting, boot_entered_waiter},
            timeout=1,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if starting in done:
            boot_entered_waiter.cancel()
            await asyncio.gather(boot_entered_waiter, return_exceptions=True)
            await starting
        if boot_entered_waiter not in done:
            starting.cancel()
            boot_entered_waiter.cancel()
            await asyncio.gather(starting, boot_entered_waiter, return_exceptions=True)
            pytest.fail("provider boot did not reach its cancellation point")
        starting.cancel()

        with pytest.raises(asyncio.CancelledError) as captured:
            await asyncio.wait_for(starting, timeout=1)

        assert captured.value.__context__ is None
        assert application.state is ApplicationState.FAILED
        assert events == [
            "register:foundation",
            "register:blocking",
            "boot:foundation",
            "boot:blocking",
            "shutdown:blocking",
            "resource:blocking",
            "shutdown:foundation",
            "resource:foundation",
        ]

    asyncio.run(scenario())


def test_async_shutdown_cancellation_finishes_cleanup_then_reraises() -> None:
    events: list[str] = []
    shutdown_entered = asyncio.Event()
    release_shutdown = asyncio.Event()

    class FoundationProvider(ServiceProvider):
        provider_id = "foundation"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar
            events.append("register:foundation")

        async def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container
            events.append("boot:foundation")
            resources.own(_AsyncRecordingResource(events, "foundation"))

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown:foundation")

    class BlockingProvider(ServiceProvider):
        provider_id = "blocking"
        dependencies = ("foundation",)

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar
            events.append("register:blocking")

        async def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container
            events.append("boot:blocking")
            resources.own(_AsyncRecordingResource(events, "blocking"))

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown:blocking")
            shutdown_entered.set()
            await release_shutdown.wait()
            events.append("shutdown:blocking:done")

    async def scenario() -> None:
        application = Application(
            Container(),
            (BlockingProvider(), FoundationProvider()),
        )
        await application.astart()
        closing = asyncio.create_task(application.ashutdown())
        await asyncio.wait_for(shutdown_entered.wait(), timeout=1)
        closing.cancel()
        release_shutdown.set()

        with pytest.raises(asyncio.CancelledError) as captured:
            await asyncio.wait_for(closing, timeout=1)

        assert captured.value.__context__ is None
        assert application.state is ApplicationState.SHUTDOWN
        await application.ashutdown()
        assert events == [
            "register:foundation",
            "register:blocking",
            "boot:foundation",
            "boot:blocking",
            "shutdown:blocking",
            "shutdown:blocking:done",
            "resource:blocking",
            "shutdown:foundation",
            "resource:foundation",
        ]

    asyncio.run(scenario())


def test_concurrent_deferred_activation_boots_and_shuts_down_once() -> None:
    events: list[str] = []
    boot_entered = asyncio.Event()
    release_boot = asyncio.Event()
    boot_calls = 0

    class DeferredProvider(ServiceProvider):
        provider_id = "deferred"
        deferred = True

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar
            events.append("register")

        async def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            nonlocal boot_calls
            del container, resources
            boot_calls += 1
            events.append("boot:start")
            boot_entered.set()
            await release_boot.wait()
            events.append("boot:done")

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    async def scenario() -> None:
        application = Application(Container(), (DeferredProvider(),))
        application.register()
        await application.aboot()

        first = asyncio.create_task(application.aactivate("deferred"))
        await asyncio.wait_for(boot_entered.wait(), timeout=1)
        second = asyncio.create_task(application.aactivate("deferred"))
        await asyncio.sleep(0)
        release_boot.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
        await application.ashutdown()

        assert boot_calls == 1
        assert events == ["register", "boot:start", "boot:done", "shutdown"]

    asyncio.run(scenario())


def test_async_registration_is_rejected_without_running_it() -> None:
    registered = False

    class AsyncRegisterProvider(ServiceProvider):
        provider_id = "async-register"

        async def register(self, registrar: ProviderRegistrar) -> None:
            nonlocal registered
            del registrar
            registered = True

    application = Application(Container(), (AsyncRegisterProvider(),))

    with pytest.raises(ApplicationRegistrationError) as captured:
        application.register()

    assert isinstance(captured.value.protected_cause, InvalidBinding)
    assert registered is False


@pytest.mark.parametrize("operation", ["freeze", "instance", "close"])
def test_captured_root_cannot_mutate_lifecycle_during_registration(operation: str) -> None:
    root = Container()

    class MutatingProvider(ServiceProvider):
        provider_id = "mutating"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar
            if operation == "freeze":
                root.freeze()
            elif operation == "instance":
                root.instance("captured", object())
            else:
                root.close()

    application = Application(root, (MutatingProvider(),))

    with pytest.raises(ApplicationRegistrationError) as captured:
        application.register()

    assert isinstance(captured.value.protected_cause, InvalidBinding)


def test_provider_cannot_adopt_a_container_owned_resource_twice() -> None:
    close_calls = 0

    class Resource:
        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    resource = Resource()

    class DuplicateOwnerProvider(ServiceProvider):
        provider_id = "duplicate-owner"

        def register(self, registrar: ProviderRegistrar) -> None:
            registrar.bind(
                Resource,
                lambda _: resource,
                scope=Scope.APPLICATION,
            )

        def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            resources.own(container.resolve(Resource))

    application = Application(Container(), (DuplicateOwnerProvider(),))

    with pytest.raises(ApplicationBootError) as captured:
        application.start()

    assert isinstance(captured.value.protected_cause, InvalidBinding)
    assert close_calls == 1


def test_sync_shutdown_can_handoff_container_async_resources() -> None:
    close_calls = 0

    class AsyncResource:
        async def aclose(self) -> None:
            nonlocal close_calls
            close_calls += 1

    class AsyncResourceProvider(ServiceProvider):
        provider_id = "async-resource"

        def register(self, registrar: ProviderRegistrar) -> None:
            registrar.bind(
                AsyncResource,
                lambda _: AsyncResource(),
                scope=Scope.APPLICATION,
            )

        def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del resources
            container.resolve(AsyncResource)

    application = Application(Container(), (AsyncResourceProvider(),))
    application.start()

    with pytest.raises(AsyncLifecycleRequired):
        application.shutdown()

    asyncio.run(application.ashutdown())
    assert close_calls == 1
    assert application.state is ApplicationState.SHUTDOWN


def test_sync_boot_failure_hands_async_context_cleanup_to_ashutdown() -> None:
    events: list[str] = []

    class AsyncResource:
        async def aclose(self) -> None:
            events.append("resource")

    class FailingProvider(ServiceProvider):
        provider_id = "failing"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container
            resources.own(AsyncResource())
            raise RuntimeError("private failure")

        def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    application = Application(Container(), (FailingProvider(),))

    with pytest.raises(ApplicationBootError):
        application.start()
    with pytest.raises(AsyncLifecycleRequired):
        application.shutdown()
    asyncio.run(application.ashutdown())

    assert events == ["shutdown", "resource"]


def test_shutdown_waits_for_inflight_deferred_activation() -> None:
    events: list[str] = []
    boot_entered = asyncio.Event()
    release_boot = asyncio.Event()

    class Resource:
        async def aclose(self) -> None:
            events.append("resource")

    class DeferredProvider(ServiceProvider):
        provider_id = "deferred-shutdown"
        deferred = True

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        async def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container
            resources.own(Resource())
            events.append("boot")
            boot_entered.set()
            await release_boot.wait()

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    async def scenario() -> None:
        application = Application(Container(), (DeferredProvider(),))
        application.register()
        await application.aboot()
        activating = asyncio.create_task(application.aactivate("deferred-shutdown"))
        await asyncio.wait_for(boot_entered.wait(), timeout=1)
        closing = asyncio.create_task(application.ashutdown())
        await asyncio.sleep(0)
        assert not closing.done()
        release_boot.set()
        await asyncio.gather(activating, closing)
        assert application.state is ApplicationState.SHUTDOWN

    asyncio.run(scenario())

    assert events == ["boot", "shutdown", "resource"]


def test_deferred_provider_can_activate_after_application_starts_serving() -> None:
    events: list[str] = []
    application = Application(
        Container(),
        (_RecordingProvider("deferred-runtime", events, deferred=True),),
    )
    application.start()

    application.activate("deferred-runtime")
    application.shutdown()

    assert events == [
        "register:deferred-runtime",
        "boot:deferred-runtime",
        "shutdown:deferred-runtime",
    ]


def test_provider_lifecycle_masks_invocation_context() -> None:
    observed_tenants: list[str | None] = []

    class ContextProvider(ServiceProvider):
        provider_id = "context-provider"
        deferred = True

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar
            context = maybe_current_context()
            observed_tenants.append(None if context is None else context.tenant_id)

        def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container, resources
            context = maybe_current_context()
            observed_tenants.append(None if context is None else context.tenant_id)

        def shutdown(self, container: Container) -> None:
            del container
            context = maybe_current_context()
            observed_tenants.append(None if context is None else context.tenant_id)

    with use_context(ExecutionContext(tenant_id="private")):
        application = Application(Container(), (ContextProvider(),))
        application.start()
        application.activate("context-provider")
        application.shutdown()

    assert observed_tenants == [None, None, None]


def test_concurrent_sync_activation_is_single_flight() -> None:
    boot_entered = ThreadEvent()
    release_boot = ThreadEvent()
    boot_calls = 0

    class DeferredProvider(ServiceProvider):
        provider_id = "sync-deferred"
        deferred = True

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            nonlocal boot_calls
            del container, resources
            boot_calls += 1
            boot_entered.set()
            assert release_boot.wait(timeout=1)

    application = Application(Container(), (DeferredProvider(),))
    application.start()
    first = Thread(target=application.activate, args=("sync-deferred",))
    second = Thread(target=application.activate, args=("sync-deferred",))
    first.start()
    assert boot_entered.wait(timeout=1)
    second.start()
    release_boot.set()
    first.join(timeout=1)
    second.join(timeout=1)
    application.shutdown()

    assert not first.is_alive()
    assert not second.is_alive()
    assert boot_calls == 1


def test_provider_resource_context_validates_ownership_callbacks_and_disposes_once() -> None:
    claimed: set[int] = set()
    released: list[object] = []

    def claim(value: object) -> bool:
        if id(value) in claimed:
            return False
        claimed.add(id(value))
        return True

    context = ProviderBootContext(
        allow_async=False,
        claim_resource=claim,
        release_resource=released.append,
    )
    resource = object()
    with pytest.raises(InvalidBinding, match="close callback"):
        context.own(resource, close=object())  # type: ignore[arg-type]
    with pytest.raises(InvalidBinding, match="aclose callback"):
        context.own(resource, aclose=object())  # type: ignore[arg-type]
    with pytest.raises(InvalidBinding, match="disposable"):
        context.own(resource)

    closed: list[str] = []
    context.own(resource, close=lambda: closed.append("closed"))
    context.own(resource, close=lambda: closed.append("duplicate"))
    assert context.requires_async_disposal() is False
    assert context.dispose() == ()
    assert context.dispose() == ()
    assert closed == ["closed"]
    assert released == [resource]
    with pytest.raises(InvalidBinding, match="inactive"):
        context.own(object(), close=lambda: None)


def test_provider_resource_context_reports_sync_and_async_disposal_failures() -> None:
    released: list[object] = []
    sync = ProviderBootContext(
        allow_async=False,
        claim_resource=lambda _: True,
        release_resource=released.append,
    )

    def fail() -> None:
        raise RuntimeError("private close failure")

    resource = object()
    sync.own(resource, close=fail)
    failures = sync.dispose()
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert released == [resource]

    async def scenario() -> None:
        events: list[str] = []
        context = ProviderBootContext(
            allow_async=True,
            claim_resource=lambda _: True,
            release_resource=lambda _: events.append("release"),
        )

        class AsyncResource:
            async def aclose(self) -> None:
                events.append("aclose")

        context.own(AsyncResource())
        assert context.requires_async_disposal() is True
        with pytest.raises(AsyncLifecycleRequired):
            context.dispose()
        assert await context.adispose() == ()
        assert await context.adispose() == ()
        assert events == ["aclose", "release"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "operation",
    ["boot", "serve", "drain", "activate", "unknown", "register-after-start"],
)
def test_invalid_lifecycle_transitions_fail_closed(operation: str) -> None:
    application = Application(Container(), (_RecordingProvider("provider", []),))
    if operation == "boot":
        action = application.boot
    elif operation == "serve":
        action = application.serve
    elif operation == "drain":
        action = application.drain
    elif operation == "activate":

        def action() -> None:
            application.activate("provider")
    else:
        application.start()
        if operation == "unknown":

            def action() -> None:
                application.activate("unknown")
        else:
            action = application.register
    with pytest.raises((InvalidBinding, ApplicationGraphError)):
        action()
    application.shutdown()


def test_empty_application_allows_idempotent_phases_and_shutdown_before_start() -> None:
    never_started = Application(Container(), ())
    never_started.shutdown()
    never_started.shutdown()
    assert never_started.state is ApplicationState.SHUTDOWN

    application = Application(Container(), ())
    application.register()
    application.register()
    application.boot()
    application.boot()
    application.serve()
    application.serve()
    application.drain()
    application.drain()
    application.shutdown()
    assert application.provider_order == ()
    assert application.cleanup_failures == ()


def test_shutdown_collects_provider_and_resource_failures_but_continues() -> None:
    events: list[str] = []

    class BrokenResource:
        def close(self) -> None:
            events.append("resource")
            raise RuntimeError("private resource failure")

    class BrokenProvider(ServiceProvider):
        provider_id = "broken"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def boot(self, container: Container, resources: ProviderResourceRegistry) -> None:
            del container
            resources.own(BrokenResource())

        def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")
            raise RuntimeError("private provider failure")

    application = Application(Container(), (BrokenProvider(),))
    application.start()
    with pytest.raises(ApplicationShutdownError) as captured:
        application.shutdown()
    assert len(captured.value.failures) == 2
    assert len(application.cleanup_failures) == 2
    assert events == ["shutdown", "resource"]
    assert application.state is ApplicationState.SHUTDOWN


def test_async_shutdown_collects_failures_and_is_idempotent() -> None:
    class BrokenResource:
        async def aclose(self) -> None:
            raise RuntimeError("private resource failure")

    class BrokenProvider(ServiceProvider):
        provider_id = "broken-async"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        async def boot(self, container: Container, resources: ProviderResourceRegistry) -> None:
            del container
            resources.own(BrokenResource())

        async def shutdown(self, container: Container) -> None:
            del container
            raise RuntimeError("private provider failure")

    async def scenario() -> None:
        application = Application(Container(), (BrokenProvider(),))
        await application.astart()
        with pytest.raises(ApplicationShutdownError) as captured:
            await application.ashutdown()
        assert len(captured.value.failures) == 2
        await application.ashutdown()
        application.shutdown()
        assert application.state is ApplicationState.SHUTDOWN

    asyncio.run(scenario())


def test_application_constructor_and_provider_metadata_are_strict() -> None:
    with pytest.raises(TypeError, match="Container"):
        Application(object(), ())  # type: ignore[arg-type]

    class ExplodingProvider(ServiceProvider):
        @property
        def provider_id(self) -> str:
            raise RuntimeError("private metadata failure")

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

    for provider in (
        object(),
        ExplodingProvider(),
        _RecordingProvider("Bad ID", []),
    ):
        application = Application(Container(), (provider,))  # type: ignore[arg-type]
        with pytest.raises(ApplicationGraphError):
            application.register()
        assert application.state is ApplicationState.FAILED


def test_async_boot_failure_rolls_back_every_layer_and_records_cleanup_failures() -> None:
    events: list[str] = []

    class Resource:
        async def aclose(self) -> None:
            events.append("resource")
            raise RuntimeError("private resource cleanup")

    class Foundation(ServiceProvider):
        provider_id = "foundation-failure"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        async def boot(self, container: Container, resources: ProviderResourceRegistry) -> None:
            del container
            events.append("boot:foundation")
            resources.own(Resource())

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown:foundation")
            raise RuntimeError("private foundation cleanup")

    class Failing(ServiceProvider):
        provider_id = "failing-async"
        dependencies = ("foundation-failure",)

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        async def boot(self, container: Container, resources: ProviderResourceRegistry) -> None:
            del container
            events.append("boot:failing")
            resources.own(Resource())
            raise RuntimeError("private boot failure")

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown:failing")
            raise RuntimeError("private failing cleanup")

    async def scenario() -> None:
        application = Application(Container(), (Failing(), Foundation()))
        with pytest.raises(ApplicationBootError) as captured:
            await application.astart()
        assert captured.value.provider_id == "failing-async"
        assert len(captured.value.cleanup_failures) == 4
        assert len(application.cleanup_failures) == 4
        assert application.state is ApplicationState.FAILED
        await application.ashutdown()

    asyncio.run(scenario())
    assert events == [
        "boot:foundation",
        "boot:failing",
        "shutdown:failing",
        "resource",
        "shutdown:foundation",
        "resource",
    ]


def test_sync_hooks_that_return_awaitables_are_rejected_and_closed() -> None:
    created: list[Coroutine[object, object, None]] = []

    class AwaitableBoot(ServiceProvider):
        provider_id = "awaitable-boot"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def boot(self, container: Container, resources: ProviderResourceRegistry):
            del container, resources

            async def deferred() -> None:
                await asyncio.sleep(0)

            awaitable = deferred()
            created.append(awaitable)
            return awaitable

    application = Application(Container(), (AwaitableBoot(),))
    with pytest.raises(AsyncLifecycleRequired):
        application.start()
    assert len(created) == 1
    assert getcoroutinestate(created[0]) == CORO_CLOSED
    assert application.state is ApplicationState.FAILED


def test_async_lifecycle_is_bound_to_one_event_loop() -> None:
    application = Application(Container(), ())
    asyncio.run(application.astart())
    with pytest.raises(InvalidBinding, match="event loop"):
        asyncio.run(application.ashutdown())
    application.shutdown()


def test_cancelled_async_start_waits_for_sync_boot_before_rollback() -> None:
    events: list[str] = []
    boot_entered = ThreadEvent()
    release_boot = ThreadEvent()

    class Resource:
        def close(self) -> None:
            events.append("resource")

    class Provider(ServiceProvider):
        provider_id = "sync-worker"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container
            resources.own(Resource())
            events.append("boot:start")
            boot_entered.set()
            assert release_boot.wait(timeout=1)
            events.append("boot:end")

        def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    async def scenario() -> None:
        application = Application(Container(), (Provider(),))
        starting = asyncio.create_task(application.astart())
        assert await asyncio.to_thread(boot_entered.wait, 1)
        starting.cancel()
        await asyncio.sleep(0)
        assert not starting.done()
        assert events == ["boot:start"]
        release_boot.set()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert application.state is ApplicationState.FAILED

    asyncio.run(scenario())
    assert events == ["boot:start", "boot:end", "shutdown", "resource"]


def test_async_shutdown_waits_for_inflight_sync_activation() -> None:
    events: list[str] = []
    boot_entered = ThreadEvent()
    release_boot = ThreadEvent()

    class Resource:
        def close(self) -> None:
            events.append("resource")

    class DeferredProvider(ServiceProvider):
        provider_id = "deferred-sync"
        deferred = True

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container
            resources.own(Resource())
            events.append("boot:start")
            boot_entered.set()
            assert release_boot.wait(timeout=1)
            events.append("boot:end")

        def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    async def scenario() -> None:
        application = Application(Container(), (DeferredProvider(),))
        application.start()
        with ThreadPoolExecutor(max_workers=1) as executor:
            activation = executor.submit(application.activate, "deferred-sync")
            assert await asyncio.to_thread(boot_entered.wait, 1)
            closing = asyncio.create_task(application.ashutdown())
            await asyncio.sleep(0)
            assert not closing.done()
            with pytest.raises(InvalidBinding, match="shutdown"):
                await application.aactivate("deferred-sync")
            release_boot.set()
            await closing
            activation.result(timeout=1)
        assert application.state is ApplicationState.SHUTDOWN

    asyncio.run(scenario())
    assert events == ["boot:start", "boot:end", "shutdown", "resource"]


def test_async_shutdown_budget_covers_inflight_sync_activation() -> None:
    events: list[str] = []
    boot_entered = ThreadEvent()
    release_boot = ThreadEvent()

    class DeferredProvider(ServiceProvider):
        provider_id = "bounded-deferred-sync"
        deferred = True

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container, resources
            events.append("boot")
            boot_entered.set()
            assert release_boot.wait(timeout=1)

        def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    async def scenario() -> None:
        application = Application(
            Container(shutdown_timeout=0.02),
            (DeferredProvider(),),
        )
        application.start()
        with ThreadPoolExecutor(max_workers=1) as executor:
            activation = executor.submit(application.activate, "bounded-deferred-sync")
            assert await asyncio.to_thread(boot_entered.wait, 1)
            started = monotonic()
            try:
                with pytest.raises(ApplicationShutdownError) as captured:
                    await application.ashutdown()
            finally:
                release_boot.set()
            assert monotonic() - started < 0.5
            assert tuple(type(error) for error in captured.value.failures) == (ShutdownTimeout,)
            activation.result(timeout=1)
        await application.ashutdown()
        assert application.state is ApplicationState.SHUTDOWN

    asyncio.run(scenario())
    assert events == ["boot", "shutdown"]


def test_mixed_sync_and_async_activation_is_single_flight() -> None:
    events: list[str] = []
    boot_entered = ThreadEvent()
    release_boot = ThreadEvent()

    class DeferredProvider(ServiceProvider):
        provider_id = "mixed-deferred"
        deferred = True

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container, resources
            events.append("boot")
            boot_entered.set()
            assert release_boot.wait(timeout=1)

        def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    async def scenario() -> None:
        application = Application(Container(), (DeferredProvider(),))
        application.start()
        with ThreadPoolExecutor(max_workers=1) as executor:
            activation = executor.submit(application.activate, "mixed-deferred")
            assert await asyncio.to_thread(boot_entered.wait, 1)
            with pytest.raises(InvalidBinding, match="synchronous"):
                await application.aactivate("mixed-deferred")
            release_boot.set()
            activation.result(timeout=1)
        await application.ashutdown()

    asyncio.run(scenario())
    assert events == ["boot", "shutdown"]


def test_async_shutdown_joins_inflight_sync_shutdown() -> None:
    events: list[str] = []
    shutdown_entered = ThreadEvent()
    release_shutdown = ThreadEvent()

    class Provider(ServiceProvider):
        provider_id = "mixed-shutdown"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")
            shutdown_entered.set()
            assert release_shutdown.wait(timeout=1)

    async def scenario() -> None:
        application = Application(Container(), (Provider(),))
        application.start()
        with ThreadPoolExecutor(max_workers=1) as executor:
            sync_shutdown = executor.submit(application.shutdown)
            assert await asyncio.to_thread(shutdown_entered.wait, 1)
            async_shutdown = asyncio.create_task(application.ashutdown())
            await asyncio.sleep(0)
            assert not async_shutdown.done()
            release_shutdown.set()
            await async_shutdown
            sync_shutdown.result(timeout=1)
        assert application.state is ApplicationState.SHUTDOWN

    asyncio.run(scenario())
    assert events == ["shutdown"]


def test_provider_claim_inside_container_factory_prevents_double_disposal() -> None:
    close_calls = 0

    class Resource:
        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    class Provider(ServiceProvider):
        provider_id = "factory-claim"

        def __init__(self) -> None:
            self.resources: ProviderResourceRegistry | None = None

        def register(self, registrar: ProviderRegistrar) -> None:
            def create(_: object) -> Resource:
                resource = Resource()
                assert self.resources is not None
                self.resources.own(resource)
                return resource

            registrar.bind(Resource, create, scope=Scope.APPLICATION)

        def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            self.resources = resources
            container.resolve(Resource)

    application = Application(Container(), (Provider(),))
    application.start()
    application.shutdown()
    assert close_calls == 1


def test_cleanup_cancellation_preserves_other_failure_evidence() -> None:
    class FailingResource:
        async def aclose(self) -> None:
            raise RuntimeError("private cleanup failure")

    class CancellingResource:
        async def aclose(self) -> None:
            raise asyncio.CancelledError("cleanup cancelled")

    class Provider(ServiceProvider):
        provider_id = "cleanup-evidence"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        async def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container
            resources.own(FailingResource())
            resources.own(CancellingResource())

    async def scenario() -> None:
        application = Application(Container(), (Provider(),))
        await application.astart()
        with pytest.raises(asyncio.CancelledError, match="cleanup cancelled"):
            await application.ashutdown()
        assert any(
            isinstance(error, RuntimeError) and str(error) == "private cleanup failure"
            for error in application.cleanup_failures
        )
        assert application.state is ApplicationState.SHUTDOWN

    asyncio.run(scenario())


def test_provider_cleanup_respects_the_application_shutdown_budget() -> None:
    shutdown_entered = ThreadEvent()
    release_shutdown = ThreadEvent()

    class Provider(ServiceProvider):
        provider_id = "bounded-cleanup"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def shutdown(self, container: Container) -> None:
            del container
            shutdown_entered.set()
            release_shutdown.wait(timeout=1)

    application = Application(Container(shutdown_timeout=0.02), (Provider(),))
    application.start()
    started = monotonic()
    try:
        with pytest.raises(ApplicationShutdownError) as captured:
            application.shutdown()
    finally:
        release_shutdown.set()
    assert monotonic() - started < 0.5
    assert any(type(error).__name__ == "ShutdownTimeout" for error in captured.value.failures)


def test_sync_shutdown_awaitable_is_handed_off_without_reinvocation() -> None:
    calls = 0
    events: list[str] = []

    class Provider(ServiceProvider):
        provider_id = "dynamic-async-shutdown"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def shutdown(self, container: Container):
            del container
            nonlocal calls
            calls += 1

            async def complete() -> None:
                events.append("shutdown")

            return complete()

    application = Application(Container(), (Provider(),))
    application.start()
    with pytest.raises(AsyncLifecycleRequired):
        application.shutdown()
    assert calls == 1
    assert events == []
    asyncio.run(application.ashutdown())
    assert calls == 1
    assert events == ["shutdown"]


def test_sync_rollback_awaitable_is_handed_off_without_reinvocation() -> None:
    calls = 0
    events: list[str] = []

    class Foundation(ServiceProvider):
        provider_id = "dynamic-foundation"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def shutdown(self, container: Container):
            del container
            nonlocal calls
            calls += 1

            async def complete() -> None:
                events.append("shutdown")

            return complete()

    class Failing(ServiceProvider):
        provider_id = "dynamic-failing"
        dependencies = ("dynamic-foundation",)

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container, resources
            raise RuntimeError("private boot failure")

    application = Application(Container(), (Foundation(), Failing()))
    with pytest.raises(ApplicationBootError):
        application.start()
    assert calls == 1
    asyncio.run(application.ashutdown())
    assert calls == 1
    assert events == ["shutdown"]


def test_resource_close_returning_awaitable_is_reported_in_both_modes() -> None:
    def dynamic_close():
        async def complete() -> None:
            return None

        return complete()

    sync = ProviderBootContext(
        allow_async=False,
        claim_resource=lambda _: True,
        release_resource=lambda _: None,
    )
    sync.own(object(), close=dynamic_close)
    sync_failures = sync.dispose()
    assert any(isinstance(error, AsyncLifecycleRequired) for error in sync_failures)

    async def scenario() -> None:
        context = ProviderBootContext(
            allow_async=True,
            claim_resource=lambda _: True,
            release_resource=lambda _: None,
        )
        context.own(object(), close=dynamic_close)
        failures = await context.adispose()
        assert any(isinstance(error, InvalidBinding) for error in failures)

    asyncio.run(scenario())


def test_async_provider_cleanup_respects_the_application_shutdown_budget() -> None:
    class Provider(ServiceProvider):
        provider_id = "bounded-async-cleanup"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        async def shutdown(self, container: Container) -> None:
            del container
            await asyncio.Event().wait()

    async def scenario() -> None:
        application = Application(Container(shutdown_timeout=0.02), (Provider(),))
        await application.astart()
        started = monotonic()
        with pytest.raises(ApplicationShutdownError) as captured:
            await application.ashutdown()
        assert monotonic() - started < 0.5
        assert any(type(error).__name__ == "ShutdownTimeout" for error in captured.value.failures)

    asyncio.run(scenario())


def test_async_shutdown_budget_covers_inflight_async_activation() -> None:
    events: list[str] = []
    boot_entered = asyncio.Event()
    release_boot = asyncio.Event()

    class Provider(ServiceProvider):
        provider_id = "bounded-async-activation"
        deferred = True

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        async def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container, resources
            events.append("boot")
            boot_entered.set()
            await release_boot.wait()

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    async def scenario() -> None:
        application = Application(
            Container(shutdown_timeout=0.02),
            (Provider(),),
        )
        await application.astart()
        activation = asyncio.create_task(application.aactivate("bounded-async-activation"))
        await boot_entered.wait()
        started = monotonic()
        try:
            with pytest.raises(ApplicationShutdownError) as captured:
                await application.ashutdown()
        finally:
            release_boot.set()
        assert monotonic() - started < 0.5
        assert tuple(type(error) for error in captured.value.failures) == (ShutdownTimeout,)
        await activation
        await application.ashutdown()
        assert application.state is ApplicationState.SHUTDOWN

    asyncio.run(scenario())
    assert events == ["boot", "shutdown"]


def test_sync_activation_is_rejected_while_async_activation_is_inflight() -> None:
    boot_entered = asyncio.Event()
    release_boot = asyncio.Event()

    class Provider(ServiceProvider):
        provider_id = "async-gated"
        deferred = True

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        async def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container, resources
            boot_entered.set()
            await release_boot.wait()

    async def scenario() -> None:
        application = Application(Container(), (Provider(),))
        await application.astart()
        activating = asyncio.create_task(application.aactivate("async-gated"))
        await boot_entered.wait()
        with pytest.raises(InvalidBinding, match="asynchronous provider activation"):
            application.activate("async-gated")
        release_boot.set()
        await activating
        await application.ashutdown()

    asyncio.run(scenario())


def test_registration_returning_awaitable_fails_before_boot() -> None:
    created: list[Coroutine[object, object, None]] = []

    class Provider(ServiceProvider):
        provider_id = "dynamic-register"

        def register(self, registrar: ProviderRegistrar):
            del registrar

            async def deferred() -> None:
                await asyncio.sleep(0)

            awaitable = deferred()
            created.append(awaitable)
            return awaitable

    application = Application(Container(), (Provider(),))
    with pytest.raises(ApplicationRegistrationError):
        application.register()
    assert len(created) == 1
    assert getcoroutinestate(created[0]) == CORO_CLOSED
    assert application.state is ApplicationState.FAILED
