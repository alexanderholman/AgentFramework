from __future__ import annotations

import asyncio
import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Barrier, Event, Lock, Thread, get_ident
from time import monotonic, sleep

import pytest

from af.framework.container import Container, Scope
from af.framework.context import ExecutionContext, current_context, maybe_current_context
from af.framework.errors import (
    AsyncDisposalRequired,
    AsyncResolutionRequired,
    CircularDependency,
    InvalidBinding,
    PlatformError,
    ScopeDisposalError,
    ScopeDisposed,
    ScopeUnavailable,
    ServiceNotBound,
    ShutdownTimeout,
)


def test_concurrent_singleton_resolution_invokes_factory_once() -> None:
    class Service:
        pass

    calls = 0
    calls_lock = Lock()
    barrier = Barrier(16)
    factory_entered = Event()
    release_factory = Event()

    def factory(_: Container) -> Service:
        nonlocal calls
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        with calls_lock:
            calls += 1
        return Service()

    def resolve(container: Container) -> Service:
        barrier.wait()
        return container.resolve(Service)

    container = Container()
    container.bind(Service, factory, scope=Scope.SINGLETON)

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(resolve, container) for _ in range(16)]
        assert factory_entered.wait(timeout=5)
        release_factory.set()
        values = [future.result(timeout=5) for future in futures]

    assert calls == 1
    assert all(value is values[0] for value in values)


def test_concurrent_async_resolution_invokes_async_factory_once() -> None:
    class Service:
        pass

    async def scenario() -> tuple[int, list[Service]]:
        calls = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def factory(_: Container) -> Service:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return Service()

        container = Container()
        container.bind_async(Service, factory, scope=Scope.SINGLETON)
        tasks = [asyncio.create_task(container.resolve_async(Service)) for _ in range(16)]
        await entered.wait()
        release.set()
        values = await asyncio.gather(*tasks)
        with pytest.raises(AsyncResolutionRequired):
            container.resolve(Service)
        return calls, values

    calls, values = asyncio.run(scenario())
    assert calls == 1
    assert all(value is values[0] for value in values)


def test_async_waiter_does_not_block_loop_on_sync_single_flight() -> None:
    class Service:
        pass

    entered = Event()
    release = Event()

    def factory(_: Container) -> Service:
        entered.set()
        assert release.wait(timeout=1)
        return Service()

    async def scenario(container: Container) -> Service:
        while not entered.is_set():
            await asyncio.sleep(0)
        asyncio.get_running_loop().call_later(0.05, release.set)
        return await container.resolve_async(Service)

    container = Container()
    container.bind(Service, factory, scope=Scope.SINGLETON)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(container.resolve, Service)
        async_value = asyncio.run(scenario(container))
        sync_value = first.result(timeout=5)

    assert async_value is sync_value


def test_concurrent_application_resolution_is_single_flight() -> None:
    class ApplicationService:
        pass

    calls = {ApplicationService: 0}
    entered = {ApplicationService: Event()}
    release = {ApplicationService: Event()}
    root = Container()

    def application_factory(_: Container) -> ApplicationService:
        entered[ApplicationService].set()
        assert release[ApplicationService].wait(timeout=5)
        calls[ApplicationService] += 1
        return ApplicationService()

    root.bind(ApplicationService, application_factory, scope=Scope.APPLICATION)
    application = root.application_scope()

    for service, scope in ((ApplicationService, application),):
        barrier = Barrier(8)

        def resolve(
            current_scope: Container = scope,
            current_service: type[object] = service,
            current_barrier: Barrier = barrier,
        ) -> object:
            current_barrier.wait()
            return current_scope.resolve(current_service)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(resolve) for _ in range(8)]
            assert entered[service].wait(timeout=5)
            release[service].set()
            values = [future.result(timeout=5) for future in futures]
        assert calls[service] == 1
        assert all(value is values[0] for value in values)


def test_async_tagged_resolution_supports_sync_and_async_members() -> None:
    class Service:
        def __init__(self, name: str) -> None:
            self.name = name

    async def create_async(_: Container) -> Service:
        await asyncio.sleep(0)
        return Service("async")

    container = Container()
    container.bind(Service, lambda _: Service("sync"), qualifier="sync")
    container.bind_async(Service, create_async, qualifier="async")
    container.tag(
        "services",
        container.key(Service, qualifier="sync"),
        container.key(Service, qualifier="async"),
    )

    values = asyncio.run(container.resolve_tagged_async("services"))
    assert tuple(value.name for value in values) == ("sync", "async")


def test_async_factory_resolver_supports_tagged_dependency_resolution() -> None:
    class Dependency:
        pass

    class Consumer:
        def __init__(self, dependencies: tuple[object, ...]) -> None:
            self.dependencies = dependencies

    first = Dependency()
    second = Dependency()

    async def create_consumer(current: Container) -> Consumer:
        return Consumer(await current.resolve_tagged_async("dependencies"))

    async def scenario() -> None:
        root = Container()
        root.instance(Dependency, first, qualifier="first")
        root.instance(Dependency, second, qualifier="second")
        root.tag(
            "dependencies",
            root.key(Dependency, qualifier="first"),
            root.key(Dependency, qualifier="second"),
        )
        root.bind_async(Consumer, create_consumer)

        assert (await root.resolve_async(Consumer)).dependencies == (first, second)
        root.close()

    asyncio.run(scenario())


def test_failed_cached_factories_are_not_poisoned_and_can_retry() -> None:
    class SyncService:
        pass

    class AsyncService:
        pass

    sync_calls = 0

    def create_sync(_: Container) -> SyncService:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise RuntimeError("first sync attempt")
        return SyncService()

    async def scenario(container: Container) -> tuple[int, AsyncService]:
        async_calls = 0

        async def create_async(_: Container) -> AsyncService:
            nonlocal async_calls
            async_calls += 1
            if async_calls == 1:
                raise RuntimeError("first async attempt")
            return AsyncService()

        container.bind_async(AsyncService, create_async, scope=Scope.SINGLETON)
        with pytest.raises(RuntimeError, match="first async attempt"):
            await container.resolve_async(AsyncService)
        value = await container.resolve_async(AsyncService)
        return async_calls, value

    container = Container()
    container.bind(SyncService, create_sync, scope=Scope.SINGLETON)
    with pytest.raises(RuntimeError, match="first sync attempt"):
        container.resolve(SyncService)
    assert isinstance(container.resolve(SyncService), SyncService)

    async_calls, async_value = asyncio.run(scenario(container))
    assert sync_calls == 2
    assert async_calls == 2
    assert isinstance(async_value, AsyncService)


def test_disposal_drains_inflight_construction_and_rejects_late_publication() -> None:
    class Resource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    entered = Event()
    release = Event()

    def factory(_: Container) -> Resource:
        entered.set()
        assert release.wait(timeout=5)
        return Resource()

    root = Container()
    root.bind(Resource, factory, scope=Scope.SINGLETON)
    with ThreadPoolExecutor(max_workers=2) as executor:
        resolving = executor.submit(root.resolve, Resource)
        assert entered.wait(timeout=5)
        closing = executor.submit(root.close)
        assert not closing.done()
        release.set()
        with pytest.raises(ScopeDisposed):
            resolving.result(timeout=5)
        closing.result(timeout=5)


def test_resource_recording_failure_retires_construction_lease() -> None:
    class RecordingFailure(BaseException):
        pass

    class Resource:
        def close(self) -> None:
            pass

    class FaultyContainer(Container):
        def _record_resource_locked(self, resource: object) -> None:
            del resource
            raise RecordingFailure("fault injection")

    root = FaultyContainer()
    root.bind(Resource, lambda _: Resource())

    with pytest.raises(RecordingFailure):
        root.resolve(Resource)

    assert root._active_constructions == 0  # type: ignore[attr-defined]
    root.close()


def test_sync_close_defers_when_inflight_factory_reveals_async_cleanup() -> None:
    class AsyncResource:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    entered = Event()
    release = Event()
    resource = AsyncResource()

    def factory(_: Container) -> AsyncResource:
        entered.set()
        assert release.wait(timeout=5)
        return resource

    root = Container()
    root.bind(AsyncResource, factory, scope=Scope.SINGLETON)
    with ThreadPoolExecutor(max_workers=2) as executor:
        resolving = executor.submit(root.resolve, AsyncResource)
        assert entered.wait(timeout=5)
        closing = executor.submit(root.close)
        assert not closing.done()
        release.set()
        with pytest.raises(ScopeDisposed):
            resolving.result(timeout=5)
        with pytest.raises(AsyncDisposalRequired):
            closing.result(timeout=5)

    assert not resource.closed
    repeated: Queue[BaseException | None] = Queue()

    def close_again() -> None:
        try:
            root.close()
        except BaseException as error:
            repeated.put(error)
        else:
            repeated.put(None)

    repeated_close = Thread(target=close_again, daemon=True)
    repeated_close.start()
    repeated_close.join(timeout=1)
    assert not repeated_close.is_alive()
    assert isinstance(repeated.get_nowait(), AsyncDisposalRequired)

    asyncio.run(root.aclose())
    assert resource.closed


def test_parent_sync_close_defers_for_descendant_async_construction() -> None:
    class AsyncResource:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        resource = AsyncResource()

        async def factory(_: Container) -> AsyncResource:
            entered.set()
            await release.wait()
            return resource

        root = Container()
        root.bind_async(AsyncResource, factory, scope=Scope.INVOCATION)
        invocation = root.application_scope().invocation_scope()
        await invocation.__aenter__()
        resolving = invocation.create_task(invocation.resolve_async(AsyncResource))
        await entered.wait()
        try:
            with pytest.raises(AsyncDisposalRequired):
                root.close()
        finally:
            release.set()

        assert await resolving is resource
        await root.aclose()
        await invocation.__aexit__(None, None, None)
        assert resource.closed

    asyncio.run(scenario())


def test_cancelled_aclose_completes_sync_to_async_cleanup_handoff() -> None:
    factory_entered = Event()
    release_factory = Event()
    sync_cleanup_started = Event()
    resolution_outcomes: Queue[BaseException | object] = Queue()
    close_outcomes: Queue[BaseException | None] = Queue()
    closed: list[str] = []

    class SignallingContainer(Container):
        def _prepare_sync_disposal(self) -> bool:
            sync_cleanup_started.set()
            return super()._prepare_sync_disposal()

    class AsyncResource:
        async def aclose(self) -> None:
            closed.append("resource")

    def factory(_: Container) -> AsyncResource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return AsyncResource()

    root = SignallingContainer()
    root.bind(AsyncResource, factory, scope=Scope.SINGLETON)

    def resolve() -> None:
        try:
            resolution_outcomes.put(root.resolve(AsyncResource))
        except BaseException as error:
            resolution_outcomes.put(error)

    def close() -> None:
        try:
            root.close()
        except BaseException as error:
            close_outcomes.put(error)
        else:
            close_outcomes.put(None)

    resolving = Thread(target=resolve, daemon=True)
    closing = Thread(target=close, daemon=True)
    resolving.start()
    assert factory_entered.wait(timeout=5)
    closing.start()
    assert sync_cleanup_started.wait(timeout=5)

    async def scenario() -> None:
        cleanup = asyncio.create_task(root.aclose())
        await asyncio.sleep(0)
        cleanup.cancel()
        await asyncio.sleep(0)
        try:
            assert not cleanup.done()
        finally:
            release_factory.set()
            await asyncio.to_thread(resolving.join, 5)
            await asyncio.to_thread(closing.join, 5)
        with pytest.raises(asyncio.CancelledError):
            await cleanup

    try:
        asyncio.run(scenario())
    finally:
        release_factory.set()
        resolving.join(timeout=5)
        closing.join(timeout=5)
        if not root._close_future.done():  # type: ignore[attr-defined]
            asyncio.run(root.aclose())

    assert isinstance(resolution_outcomes.get_nowait(), ScopeDisposed)
    assert isinstance(close_outcomes.get_nowait(), AsyncDisposalRequired)
    assert closed == ["resource"]


def test_async_cleanup_bounds_factory_that_suppresses_cancellation() -> None:
    class Service:
        pass

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def factory(_: Container) -> Service:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return Service()

        root = Container(shutdown_timeout=0.05)
        root.bind_async(Service, factory, scope=Scope.TRANSIENT)
        resolving = asyncio.create_task(root.resolve_async(Service))
        await entered.wait()
        closing = asyncio.create_task(root.aclose())
        await asyncio.sleep(0.1)
        try:
            assert closing.done()
        finally:
            release.set()
        with pytest.raises(ScopeDisposalError) as captured:
            await closing
        assert any(isinstance(error, ShutdownTimeout) for error in captured.value.failures)
        outcomes = await asyncio.gather(resolving, return_exceptions=True)
        assert isinstance(outcomes[0], ScopeDisposed)

    asyncio.run(scenario())


def test_late_child_flight_retires_from_shared_resolution_graph() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        resource_closed = asyncio.Event()

        class Service:
            async def aclose(self) -> None:
                resource_closed.set()

        async def factory(_: Container) -> Service:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return Service()

        root = Container(shutdown_timeout=0.05)
        root.bind_async(Service, factory, scope=Scope.INVOCATION)
        invocation = root.application_scope().invocation_scope()
        await invocation.__aenter__()
        resolving = invocation.create_task(invocation.resolve_async(Service))
        await entered.wait()
        with pytest.raises(ScopeDisposalError):
            await root.aclose()
        release.set()
        outcomes = await asyncio.gather(resolving, return_exceptions=True)
        with pytest.raises(ScopeDisposalError):
            await invocation.aclose()
        assert resource_closed.is_set()
        with pytest.raises(ScopeDisposalError):
            await invocation.__aexit__(None, None, None)

        assert isinstance(outcomes[0], (asyncio.CancelledError, ScopeDisposed))
        assert root._flight_services == {}  # type: ignore[attr-defined]
        assert root._wait_edges == {}  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_late_child_factory_releases_original_registry_guard_owner() -> None:
    class Service:
        pass

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def factory(_: Container) -> Service:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return Service()

        root = Container(shutdown_timeout=0.05)
        root.bind_async(Service, factory, scope=Scope.INVOCATION)
        invocation = root.application_scope().invocation_scope()
        await invocation.__aenter__()
        resolving = invocation.create_task(invocation.resolve_async(Service))
        await entered.wait()
        with pytest.raises(ScopeDisposalError):
            await invocation.aclose()
        release.set()
        await asyncio.gather(resolving, return_exceptions=True)
        await asyncio.sleep(0)
        with pytest.raises(ScopeDisposalError):
            await invocation.__aexit__(None, None, None)

        assert root._active_factories == 0  # type: ignore[attr-defined]
        assert invocation._active_factories == 0  # type: ignore[attr-defined]
        root.application_scope()
        await root.aclose()

    asyncio.run(scenario())


def test_sync_cleanup_bounds_blocked_factory_construction() -> None:
    class Service:
        def close(self) -> None:
            resource_closed.set()

    factory_entered = Event()
    release_factory = Event()
    resource_closed = Event()
    resolution_outcomes: Queue[BaseException | object] = Queue()
    close_outcomes: Queue[BaseException | None] = Queue()

    def factory(_: Container) -> Service:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return Service()

    root = Container(shutdown_timeout=0.05)
    root.bind(Service, factory, scope=Scope.SINGLETON)

    def resolve() -> None:
        try:
            resolution_outcomes.put(root.resolve(Service))
        except BaseException as error:
            resolution_outcomes.put(error)

    def close() -> None:
        try:
            root.close()
        except BaseException as error:
            close_outcomes.put(error)
        else:
            close_outcomes.put(None)

    resolving = Thread(target=resolve, daemon=True)
    closing = Thread(target=close, daemon=True)
    resolving.start()
    assert factory_entered.wait(timeout=5)
    closing.start()
    closing.join(timeout=0.5)
    try:
        assert not closing.is_alive()
    finally:
        release_factory.set()
    resolving.join(timeout=5)
    closing.join(timeout=5)

    close_error = close_outcomes.get_nowait()
    assert isinstance(close_error, ScopeDisposalError)
    assert any(isinstance(error, ShutdownTimeout) for error in close_error.failures)
    assert isinstance(resolution_outcomes.get_nowait(), ScopeDisposed)
    with pytest.raises(ScopeDisposalError):
        asyncio.run(root.aclose())
    assert resource_closed.wait(timeout=5)
    assert root._active_resolutions == 0  # type: ignore[attr-defined]
    assert root._active_constructions == 0  # type: ignore[attr-defined]


def test_late_async_resource_waits_for_explicit_async_cleanup_handoff() -> None:
    factory_entered = Event()
    release_factory = Event()
    resolution_outcomes: Queue[BaseException | object] = Queue()

    class Resource:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    resource = Resource()

    def factory(_: Container) -> Resource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return resource

    root = Container(shutdown_timeout=0.05)
    root.bind(Resource, factory, scope=Scope.SINGLETON)

    def resolve() -> None:
        try:
            resolution_outcomes.put(root.resolve(Resource))
        except BaseException as error:
            resolution_outcomes.put(error)

    resolving = Thread(target=resolve, daemon=True)
    resolving.start()
    assert factory_entered.wait(timeout=5)
    with pytest.raises(ScopeDisposalError):
        root.close()
    release_factory.set()
    resolving.join(timeout=5)

    assert isinstance(resolution_outcomes.get_nowait(), ScopeDisposed)
    assert not resource.closed
    with pytest.raises(ScopeDisposalError):
        asyncio.run(root.aclose())
    assert resource.closed


def test_immediate_late_cleanup_handoff_waits_for_unpublished_resource() -> None:
    factory_entered = Event()
    release_factory = Event()

    class Resource:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    resource = Resource()

    def factory(_: Container) -> Resource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return resource

    root = Container(shutdown_timeout=0.2)
    root.bind(Resource, factory)

    def resolve() -> None:
        with pytest.raises(ScopeDisposed):
            root.resolve(Resource)

    worker = Thread(target=resolve, daemon=True)
    worker.start()
    assert factory_entered.wait(timeout=5)
    with pytest.raises(ScopeDisposalError):
        root.close()

    async def handoff() -> None:
        cleanup = asyncio.create_task(root.aclose())
        await asyncio.sleep(0.01)
        assert not cleanup.done()
        assert not resource.closed
        release_factory.set()
        await asyncio.to_thread(worker.join, 5)
        with pytest.raises(ScopeDisposalError):
            await cleanup

    asyncio.run(handoff())
    assert resource.closed


def test_parent_late_cleanup_waits_for_detached_child_construction() -> None:
    factory_entered = Event()
    release_factory = Event()

    class Resource:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    resource = Resource()

    def factory(_: Container) -> Resource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return resource

    root = Container(shutdown_timeout=0.2)
    root.bind(Resource, factory, scope=Scope.APPLICATION)
    application = root.application_scope()

    def resolve() -> None:
        with pytest.raises(ScopeDisposed):
            application.resolve(Resource)

    worker = Thread(target=resolve, daemon=True)
    worker.start()
    assert factory_entered.wait(timeout=5)
    with pytest.raises(ScopeDisposalError):
        root.close()

    async def handoff() -> None:
        cleanup = asyncio.create_task(root.aclose())
        await asyncio.sleep(0.01)
        assert not cleanup.done()
        assert not resource.closed
        release_factory.set()
        await asyncio.to_thread(worker.join, 5)
        with pytest.raises(ScopeDisposalError):
            await cleanup

    asyncio.run(handoff())
    assert resource.closed
    assert application not in root._children  # type: ignore[attr-defined]


@pytest.mark.parametrize("mode", ["value", "failure"])
def test_parent_releases_child_after_non_resource_construction_retires(mode: str) -> None:
    factory_entered = Event()
    release_factory = Event()

    def factory(_: Container) -> object:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        if mode == "failure":
            raise RuntimeError("private construction failure")
        return object()

    root = Container(shutdown_timeout=0.05)
    root.bind("service", factory, scope=Scope.APPLICATION)
    application = root.application_scope()

    def resolve() -> None:
        with pytest.raises((ScopeDisposed, RuntimeError)):
            application.resolve_token("service")

    worker = Thread(target=resolve, daemon=True)
    worker.start()
    assert factory_entered.wait(timeout=5)
    with pytest.raises(ScopeDisposalError):
        root.close()
    release_factory.set()
    worker.join(timeout=5)

    with pytest.raises(ScopeDisposalError):
        asyncio.run(root.aclose())
    assert application not in root._children  # type: ignore[attr-defined]


def test_parent_releases_child_after_late_async_factory_failure() -> None:
    class Service:
        pass

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def factory(_: Container) -> Service:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            raise RuntimeError("private late factory failure")

        root = Container(shutdown_timeout=0.03)
        root.bind_async(Service, factory, scope=Scope.APPLICATION)
        application = root.application_scope()
        resolving = asyncio.create_task(application.resolve_async(Service))
        await entered.wait()

        with pytest.raises(ScopeDisposalError):
            await root.aclose()
        release.set()
        await asyncio.gather(resolving, return_exceptions=True)
        await asyncio.sleep(0)

        assert application not in root._children  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_parent_releases_child_after_late_async_publication_rejection() -> None:
    class Service:
        pass

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def factory(_: Container) -> Service:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return Service()

        root = Container(shutdown_timeout=0.03)
        root.bind_async(Service, factory, scope=Scope.APPLICATION)
        application = root.application_scope()
        resolving = asyncio.create_task(application.resolve_async(Service))
        await entered.wait()

        with pytest.raises(ScopeDisposalError):
            await root.aclose()
        release.set()
        await asyncio.gather(resolving, return_exceptions=True)
        await asyncio.sleep(0)

        assert application not in root._children  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_parent_late_cleanup_aggregates_child_failure() -> None:
    factory_entered = Event()
    release_factory = Event()

    class Resource:
        async def aclose(self) -> None:
            raise ValueError("private child cleanup failure")

    def factory(_: Container) -> Resource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return Resource()

    root = Container(shutdown_timeout=0.05)
    root.bind(Resource, factory, scope=Scope.APPLICATION)
    application = root.application_scope()

    def resolve() -> None:
        with pytest.raises(ScopeDisposed):
            application.resolve(Resource)

    worker = Thread(target=resolve)
    worker.start()
    assert factory_entered.wait(timeout=5)
    with pytest.raises(ScopeDisposalError):
        root.close()
    release_factory.set()
    worker.join(timeout=5)

    with pytest.raises(ScopeDisposalError) as captured:
        asyncio.run(root.aclose())
    assert any(isinstance(error, ValueError) for error in captured.value.failures)


def test_child_late_cancellation_does_not_skip_older_sibling() -> None:
    entered = [Event(), Event()]
    releases = [Event(), Event()]
    older_closed = Event()

    class OlderResource:
        async def aclose(self) -> None:
            older_closed.set()

    class NewerResource:
        async def aclose(self) -> None:
            raise asyncio.CancelledError

    root = Container(shutdown_timeout=0.05)
    values = (OlderResource(), NewerResource())
    for index, value in enumerate(values):

        def factory(
            _: Container,
            current: int = index,
            result: OlderResource | NewerResource = value,
        ) -> OlderResource | NewerResource:
            entered[current].set()
            assert releases[current].wait(timeout=5)
            return result

        root.bind(f"resource-{index}", factory, scope=Scope.APPLICATION)

    applications = [root.application_scope(), root.application_scope()]

    def resolve(index: int) -> None:
        with pytest.raises(ScopeDisposed):
            applications[index].resolve_token(f"resource-{index}")

    workers = [Thread(target=resolve, args=(index,), daemon=True) for index in range(2)]
    for worker in workers:
        worker.start()
    assert all(signal.wait(timeout=5) for signal in entered)
    with pytest.raises(ScopeDisposalError):
        root.close()
    for release, worker in zip(releases, workers, strict=True):
        release.set()
        worker.join(timeout=5)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(root.aclose())
    assert older_closed.is_set()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(root.aclose())


def test_parent_async_cleanup_joins_active_child_sync_cleanup() -> None:
    calls = 0
    sync_cleanup_entered = Event()
    release_sync_cleanup = Event()
    close_outcomes: Queue[BaseException | None] = Queue()

    class DelayedRoot(Container):
        def __init__(self) -> None:
            super().__init__()
            self.parent_waiting = asyncio.Event()
            self.release_parent = asyncio.Event()

        async def _wait_for_constructions_async(self) -> bool:
            self.parent_waiting.set()
            await self.release_parent.wait()
            return True

    class Resource:
        def close(self) -> None:
            nonlocal calls
            calls += 1
            sync_cleanup_entered.set()
            assert release_sync_cleanup.wait(timeout=5)

    root = DelayedRoot()
    root.bind(Resource, lambda _: Resource(), scope=Scope.APPLICATION)
    application = root.application_scope()
    application.resolve(Resource)

    def close_child() -> None:
        try:
            application.close()
        except BaseException as error:
            close_outcomes.put(error)
        else:
            close_outcomes.put(None)

    async def scenario() -> None:
        parent_cleanup = asyncio.create_task(root.aclose())
        await root.parent_waiting.wait()
        child_cleanup = Thread(target=close_child, daemon=True)
        child_cleanup.start()
        assert await asyncio.to_thread(sync_cleanup_entered.wait, 5)
        root.release_parent.set()
        await asyncio.sleep(0.01)
        assert calls == 1
        assert not parent_cleanup.done()
        release_sync_cleanup.set()
        await asyncio.to_thread(child_cleanup.join, 5)
        await parent_cleanup

    asyncio.run(scenario())
    assert calls == 1
    assert close_outcomes.get_nowait() is None


@pytest.mark.parametrize("cancel_parent", [False, True])
def test_parent_async_cleanup_takes_over_relinquished_child_preflight(
    cancel_parent: bool,
) -> None:
    factory_entered = Event()
    release_factory = Event()
    child_close_outcomes: Queue[BaseException | None] = Queue()

    class Resource:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    resource = Resource()

    def factory(_: Container) -> Resource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return resource

    root = Container()
    root.bind(Resource, factory, scope=Scope.APPLICATION)
    application = root.application_scope()

    def resolve() -> None:
        with pytest.raises(ScopeDisposed):
            application.resolve(Resource)

    resolver = Thread(target=resolve, daemon=True)
    resolver.start()
    assert factory_entered.wait(timeout=5)

    def close_child() -> None:
        try:
            application.close()
        except BaseException as error:
            child_close_outcomes.put(error)
        else:
            child_close_outcomes.put(None)

    async def scenario() -> None:
        child_cleanup = Thread(target=close_child, daemon=True)
        child_cleanup.start()
        deadline = monotonic() + 5
        while not application._sync_cleanup_active and monotonic() < deadline:  # type: ignore[attr-defined]
            await asyncio.sleep(0.001)
        assert application._sync_cleanup_active  # type: ignore[attr-defined]
        parent_cleanup = asyncio.create_task(application._aclose_from_parent())  # type: ignore[attr-defined]
        await asyncio.sleep(0.01)
        assert not parent_cleanup.done()
        if cancel_parent:
            parent_cleanup.cancel()
            await asyncio.sleep(0.01)
            assert not parent_cleanup.done()
        release_factory.set()
        await asyncio.to_thread(resolver.join, 5)
        await asyncio.to_thread(child_cleanup.join, 5)
        if cancel_parent:
            with pytest.raises(asyncio.CancelledError):
                await parent_cleanup
        else:
            await asyncio.wait_for(parent_cleanup, timeout=1)

    asyncio.run(scenario())
    assert isinstance(child_close_outcomes.get_nowait(), AsyncDisposalRequired)
    assert resource.closed


@pytest.mark.parametrize("mode", ["failure", "timeout"])
def test_late_async_cleanup_handoff_records_protected_outcome(mode: str) -> None:
    factory_entered = Event()
    release_factory = Event()
    release_cleanup: asyncio.Event | None = None

    class Resource:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True
            if mode == "failure":
                raise ValueError("private late cleanup failure")
            assert release_cleanup is not None
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                await release_cleanup.wait()

    resource = Resource()

    def factory(_: Container) -> Resource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return resource

    root = Container(shutdown_timeout=0.03)
    root.bind(Resource, factory, scope=Scope.SINGLETON)

    def resolve() -> None:
        with pytest.raises(ScopeDisposed):
            root.resolve(Resource)

    resolving = Thread(target=resolve, daemon=True)
    resolving.start()
    assert factory_entered.wait(timeout=5)
    with pytest.raises(ScopeDisposalError):
        root.close()
    release_factory.set()
    resolving.join(timeout=5)

    async def drain() -> None:
        nonlocal release_cleanup
        release_cleanup = asyncio.Event()
        with pytest.raises(ScopeDisposalError):
            await root.aclose()
        if mode == "timeout":
            assert not root._late_async_resources  # type: ignore[attr-defined]
            release_cleanup.set()
            await asyncio.sleep(0)
            with pytest.raises(ScopeDisposalError):
                await root.aclose()
        else:
            assert any(
                isinstance(error, ValueError)
                for error in root._disposal_failures  # type: ignore[attr-defined]
            )

    asyncio.run(drain())


def test_late_async_cleanup_handoff_is_single_flight_and_cancellation_safe() -> None:
    factory_entered = Event()
    release_factory = Event()

    class Resource:
        def __init__(self) -> None:
            self.calls = 0
            self.cleanup_entered: asyncio.Event | None = None
            self.release_cleanup: asyncio.Event | None = None

        async def aclose(self) -> None:
            self.calls += 1
            assert self.cleanup_entered is not None
            assert self.release_cleanup is not None
            self.cleanup_entered.set()
            await self.release_cleanup.wait()

    resource = Resource()

    def factory(_: Container) -> Resource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return resource

    root = Container(shutdown_timeout=0.2)
    root.bind(Resource, factory, scope=Scope.SINGLETON)

    def resolve() -> None:
        with pytest.raises(ScopeDisposed):
            root.resolve(Resource)

    resolving = Thread(target=resolve)
    resolving.start()
    assert factory_entered.wait(timeout=5)
    with pytest.raises(ScopeDisposalError):
        root.close()
    release_factory.set()
    resolving.join(timeout=5)
    assert not resolving.is_alive()

    async def drain() -> None:
        resource.cleanup_entered = asyncio.Event()
        resource.release_cleanup = asyncio.Event()
        first = asyncio.create_task(root.aclose())
        await resource.cleanup_entered.wait()
        second = asyncio.create_task(root.aclose())
        await asyncio.sleep(0)
        assert resource.calls == 1
        assert not second.done()

        first.cancel()
        await asyncio.sleep(0)
        assert not second.done()
        resource.release_cleanup.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)

        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert isinstance(outcomes[1], ScopeDisposalError)
        assert resource.calls == 1
        assert not root._late_async_resources  # type: ignore[attr-defined]

    asyncio.run(drain())


def test_late_async_cleanup_deduplicates_and_reverses_construction_order() -> None:
    entered = [Event(), Event(), Event()]
    releases = [Event(), Event(), Event()]
    outcomes: Queue[BaseException | object] = Queue()
    closed: list[str] = []

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        async def aclose(self) -> None:
            closed.append(self.name)

    shared = Resource("shared")
    second = Resource("second")
    values = (shared, shared, second)
    root = Container(shutdown_timeout=0.03)

    for index, value in enumerate(values):

        def factory(_: Container, current: int = index, result: Resource = value) -> Resource:
            entered[current].set()
            assert releases[current].wait(timeout=5)
            return result

        root.bind(f"resource-{index}", factory)

    def resolve(token: str) -> None:
        try:
            outcomes.put(root.resolve(token))
        except BaseException as error:
            outcomes.put(error)

    workers = [
        Thread(target=resolve, args=(f"resource-{index}",), daemon=True) for index in range(3)
    ]
    for worker in workers:
        worker.start()
    assert all(signal.wait(timeout=5) for signal in entered)
    with pytest.raises(ScopeDisposalError):
        root.close()
    for release, worker in zip(releases, workers, strict=True):
        release.set()
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert all(isinstance(outcomes.get_nowait(), ScopeDisposed) for _ in workers)
    with pytest.raises(ScopeDisposalError):
        asyncio.run(root.aclose())
    assert closed == ["second", "shared"]


def test_late_cleanup_waits_for_all_constructions_before_ordering_batch() -> None:
    entered = [Event(), Event()]
    releases = [Event(), Event()]
    closed: list[str] = []

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        async def aclose(self) -> None:
            closed.append(self.name)

    root = Container(shutdown_timeout=0.2)
    for index, name in enumerate(("first", "second")):

        def factory(_: Container, current: int = index, value: str = name) -> Resource:
            entered[current].set()
            assert releases[current].wait(timeout=5)
            return Resource(value)

        root.bind(name, factory)

    def resolve(token: str) -> None:
        with pytest.raises(ScopeDisposed):
            root.resolve(token)

    workers = [Thread(target=resolve, args=(name,), daemon=True) for name in ("first", "second")]
    for worker in workers:
        worker.start()
    assert all(signal.wait(timeout=5) for signal in entered)
    with pytest.raises(ScopeDisposalError):
        root.close()
    releases[0].set()
    workers[0].join(timeout=5)

    async def drain() -> None:
        cleanup = asyncio.create_task(root.aclose())
        await asyncio.sleep(0.01)
        assert closed == []
        releases[1].set()
        await asyncio.to_thread(workers[1].join, 5)
        with pytest.raises(ScopeDisposalError):
            await cleanup

    asyncio.run(drain())
    assert closed == ["second", "first"]


def test_late_cleanup_cancellation_waits_for_remaining_constructions() -> None:
    entered = [Event(), Event()]
    releases = [Event(), Event()]
    closed: list[str] = []

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        async def aclose(self) -> None:
            closed.append(self.name)

    root = Container(shutdown_timeout=0.2)
    for index, name in enumerate(("first", "second")):

        def factory(_: Container, current: int = index, value: str = name) -> Resource:
            entered[current].set()
            assert releases[current].wait(timeout=5)
            return Resource(value)

        root.bind(name, factory)

    def resolve(token: str) -> None:
        with pytest.raises(ScopeDisposed):
            root.resolve(token)

    workers = [Thread(target=resolve, args=(name,), daemon=True) for name in ("first", "second")]
    for worker in workers:
        worker.start()
    assert all(signal.wait(timeout=5) for signal in entered)
    with pytest.raises(ScopeDisposalError):
        root.close()
    releases[0].set()
    workers[0].join(timeout=5)

    async def drain() -> None:
        cleanup = asyncio.create_task(root.aclose())
        await asyncio.sleep(0.01)
        cleanup.cancel()
        await asyncio.sleep(0)
        assert not cleanup.done()
        assert closed == []
        releases[1].set()
        await asyncio.to_thread(workers[1].join, 5)
        with pytest.raises(asyncio.CancelledError):
            await cleanup

    asyncio.run(drain())
    assert closed == ["second", "first"]


def test_async_cleanup_preserves_existing_shutdown_failure_evidence() -> None:
    class Resource:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    async def scenario() -> None:
        root = Container()
        resource = Resource()
        root.instance(Resource, resource, owned=True)
        root._disposal_failures = (ShutdownTimeout(),)  # type: ignore[attr-defined]

        with pytest.raises(ScopeDisposalError) as captured:
            await root.aclose()

        assert resource.closed
        assert any(isinstance(error, ShutdownTimeout) for error in captured.value.failures)

    asyncio.run(scenario())


def test_completed_late_cleanup_releases_owned_resource_reference() -> None:
    factory_entered = Event()
    release_factory = Event()

    class Resource:
        async def aclose(self) -> None:
            pass

    resource = Resource()
    resource_reference = weakref.ref(resource)
    holder = [resource]

    def factory(_: Container) -> Resource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return holder[0]

    root = Container(shutdown_timeout=0.03)
    root.bind(Resource, factory)

    def resolve() -> None:
        with pytest.raises(ScopeDisposed):
            root.resolve(Resource)

    worker = Thread(target=resolve, daemon=True)
    worker.start()
    assert factory_entered.wait(timeout=5)
    with pytest.raises(ScopeDisposalError):
        root.close()
    release_factory.set()
    worker.join(timeout=5)
    holder.clear()
    del resource

    with pytest.raises(ScopeDisposalError):
        asyncio.run(root.aclose())
    gc.collect()

    assert resource_reference() is None


def test_late_synchronous_cleanup_failure_is_reported_by_handoff() -> None:
    factory_entered = Event()
    release_factory = Event()

    class Resource:
        def close(self) -> None:
            raise ValueError("private late sync cleanup failure")

    def factory(_: Container) -> Resource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return Resource()

    root = Container(shutdown_timeout=0.03)
    root.bind(Resource, factory)

    def resolve() -> None:
        with pytest.raises(ScopeDisposed):
            root.resolve(Resource)

    worker = Thread(target=resolve, daemon=True)
    worker.start()
    assert factory_entered.wait(timeout=5)
    with pytest.raises(ScopeDisposalError):
        root.close()
    release_factory.set()
    worker.join(timeout=5)

    with pytest.raises(ScopeDisposalError) as captured:
        asyncio.run(root.aclose())
    assert any(isinstance(error, ValueError) for error in captured.value.failures)


def test_late_disposer_cancellation_retains_cancellation_precedence() -> None:
    factory_entered = Event()
    release_factory = Event()

    class Resource:
        async def aclose(self) -> None:
            raise asyncio.CancelledError

    def factory(_: Container) -> Resource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return Resource()

    root = Container(shutdown_timeout=0.03)
    root.bind(Resource, factory)

    def resolve() -> None:
        with pytest.raises(ScopeDisposed):
            root.resolve(Resource)

    worker = Thread(target=resolve, daemon=True)
    worker.start()
    assert factory_entered.wait(timeout=5)
    with pytest.raises(ScopeDisposalError):
        root.close()
    release_factory.set()
    worker.join(timeout=5)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(root.aclose())


def test_late_disposer_cancellation_does_not_skip_older_resources() -> None:
    entered = [Event(), Event()]
    releases = [Event(), Event()]
    older_closed = Event()

    class OlderResource:
        async def aclose(self) -> None:
            older_closed.set()

    class NewerResource:
        async def aclose(self) -> None:
            raise asyncio.CancelledError

    values = (OlderResource(), NewerResource())
    root = Container(shutdown_timeout=0.03)
    for index, value in enumerate(values):

        def factory(
            _: Container,
            current: int = index,
            result: OlderResource | NewerResource = value,
        ) -> OlderResource | NewerResource:
            entered[current].set()
            assert releases[current].wait(timeout=5)
            return result

        root.bind(f"resource-{index}", factory)

    def resolve(token: str) -> None:
        with pytest.raises(ScopeDisposed):
            root.resolve(token)

    workers = [
        Thread(target=resolve, args=(f"resource-{index}",), daemon=True) for index in range(2)
    ]
    for worker in workers:
        worker.start()
    assert all(signal.wait(timeout=5) for signal in entered)
    with pytest.raises(ScopeDisposalError):
        root.close()
    for release, worker in zip(releases, workers, strict=True):
        release.set()
        worker.join(timeout=5)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(root.aclose())
    assert older_closed.is_set()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(root.aclose())


def test_event_loop_factory_uses_explicit_late_async_cleanup_handoff() -> None:
    factory_entered = Event()
    release_factory = Event()
    close_outcome: Queue[BaseException | None] = Queue()

    class Resource:
        def __init__(self) -> None:
            self.calls = 0

        async def aclose(self) -> None:
            self.calls += 1

    resource = Resource()

    def factory(_: Container) -> Resource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return resource

    root = Container(shutdown_timeout=0.03)
    root.bind(Resource, factory)

    def close() -> None:
        assert factory_entered.wait(timeout=5)
        try:
            root.close()
        except BaseException as error:
            close_outcome.put(error)
        else:
            close_outcome.put(None)
        finally:
            release_factory.set()

    async def scenario() -> None:
        closer = Thread(target=close, daemon=True)
        closer.start()
        with pytest.raises(ScopeDisposed):
            root.resolve(Resource)
        closer.join(timeout=5)
        assert not closer.is_alive()
        assert isinstance(close_outcome.get_nowait(), ScopeDisposalError)
        assert resource.calls == 0
        assert root._late_async_resources  # type: ignore[attr-defined]
        with pytest.raises(ScopeDisposalError):
            await root.aclose()
        assert resource.calls == 1

    asyncio.run(scenario())


def test_late_cleanup_batch_obeys_exhausted_hierarchy_deadline() -> None:
    class AsyncResource:
        def __init__(self) -> None:
            self.calls = 0

        async def aclose(self) -> None:
            self.calls += 1

    class SyncResource:
        def close(self) -> None:
            pass

    async def scenario() -> None:
        root = Container()
        resource = AsyncResource()
        root.instance(AsyncResource, resource, owned=True)
        root.instance(SyncResource, SyncResource(), owned=True)
        records = tuple(root._resources)  # type: ignore[attr-defined]
        root._shutdown_deadline = monotonic() - 1  # type: ignore[attr-defined]

        await root._dispose_late_async_resources(records)  # type: ignore[attr-defined]

        assert resource.calls == 0
        assert any(
            isinstance(error, ShutdownTimeout)
            for error in root._disposal_failures  # type: ignore[attr-defined]
        )

    asyncio.run(scenario())


def test_resource_finishing_after_cleanup_snapshot_is_disposed_immediately() -> None:
    factory_entered = Event()
    release_factory = Event()
    resolution_outcomes: Queue[BaseException | object] = Queue()

    class LateResource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    late_resource = LateResource()

    class SnapshotBlocker:
        def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
            self.entered = entered
            self.release = release

        async def aclose(self) -> None:
            self.entered.set()
            await self.release.wait()

    class EarlyTimeoutContainer(Container):
        async def _wait_for_constructions_async(self) -> bool:
            return False

    def factory(_: Container) -> LateResource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return late_resource

    async def scenario() -> None:
        snapshot_reached = asyncio.Event()
        release_snapshot = asyncio.Event()
        root = EarlyTimeoutContainer(shutdown_timeout=0.2)
        root.bind(LateResource, factory, scope=Scope.SINGLETON)
        root.bind(
            SnapshotBlocker,
            lambda _: SnapshotBlocker(snapshot_reached, release_snapshot),
        )

        def resolve() -> None:
            try:
                resolution_outcomes.put(root.resolve(LateResource))
            except BaseException as error:
                resolution_outcomes.put(error)

        resolving = Thread(target=resolve, daemon=True)
        resolving.start()
        assert factory_entered.wait(timeout=5)
        root.resolve(SnapshotBlocker)
        closing = asyncio.create_task(root.aclose())
        await snapshot_reached.wait()
        release_factory.set()
        await asyncio.to_thread(resolving.join, 5)
        release_snapshot.set()
        with pytest.raises(ScopeDisposalError):
            await closing

        assert isinstance(resolution_outcomes.get_nowait(), ScopeDisposed)
        assert late_resource.closed

    asyncio.run(scenario())


def test_sync_cleanup_bounds_blocked_synchronous_disposer() -> None:
    disposer_entered = Event()
    release_disposer = Event()
    close_outcomes: Queue[BaseException | None] = Queue()

    class Resource:
        def close(self) -> None:
            disposer_entered.set()
            assert release_disposer.wait(timeout=5)

    root = Container(shutdown_timeout=0.05)
    root.bind(Resource, lambda _: Resource())
    root.resolve(Resource)

    def close() -> None:
        try:
            root.close()
        except BaseException as error:
            close_outcomes.put(error)
        else:
            close_outcomes.put(None)

    closing = Thread(target=close, daemon=True)
    closing.start()
    assert disposer_entered.wait(timeout=5)
    closing.join(timeout=0.5)
    try:
        assert not closing.is_alive()
    finally:
        release_disposer.set()
    closing.join(timeout=5)

    close_error = close_outcomes.get_nowait()
    assert isinstance(close_error, ScopeDisposalError)
    assert any(isinstance(error, ShutdownTimeout) for error in close_error.failures)


def test_expired_cleanup_budget_does_not_spawn_one_worker_per_resource() -> None:
    release = Event()
    started = 0
    started_lock = Lock()

    class Resource:
        def close(self) -> None:
            nonlocal started
            with started_lock:
                started += 1
            assert release.wait(timeout=5)

    root = Container(shutdown_timeout=0.03)
    for index in range(8):
        root.instance(Resource, Resource(), qualifier=str(index), owned=True)

    try:
        with pytest.raises(ScopeDisposalError):
            root.close()
        with started_lock:
            assert started == 1
    finally:
        release.set()


def test_async_cleanup_deadline_does_not_wait_for_saturated_default_executor() -> None:
    async def scenario() -> None:
        release = Event()
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        blocker = loop.run_in_executor(executor, release.wait)
        loop.call_later(0.3, release.set)
        root = Container(shutdown_timeout=0.05)
        started = loop.time()
        try:
            await root.aclose()
            assert loop.time() - started < 0.2
        finally:
            release.set()
            await blocker
            executor.shutdown(wait=True)

    asyncio.run(scenario())


def test_concurrent_parent_and_child_cleanup_share_earliest_deadline() -> None:
    async def scenario() -> None:
        root_waiting = asyncio.Event()
        release_root = asyncio.Event()

        class DelayedRoot(Container):
            async def _wait_for_constructions_async(self) -> bool:
                root_waiting.set()
                await release_root.wait()
                return True

        class Resource:
            async def aclose(self) -> None:
                await asyncio.Event().wait()

        root = DelayedRoot(shutdown_timeout=0.05)
        root.bind(Resource, lambda _: Resource(), scope=Scope.APPLICATION)
        application = root.application_scope()
        application.resolve(Resource)
        started = asyncio.get_running_loop().time()
        parent_cleanup = asyncio.create_task(root.aclose())
        await root_waiting.wait()
        await asyncio.sleep(0.03)
        child_cleanup = asyncio.create_task(application.aclose())
        release_root.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(parent_cleanup, child_cleanup, return_exceptions=True),
            timeout=0.2,
        )

        assert asyncio.get_running_loop().time() - started < 0.08
        assert all(isinstance(outcome, ScopeDisposalError) for outcome in outcomes)

    asyncio.run(scenario())


def test_new_shutdown_operation_resets_expired_unowned_deadline() -> None:
    lock_acquired = Event()
    release_lock = Event()

    class Resource:
        def close(self) -> None:
            pass

    root = Container(shutdown_timeout=0.03)
    root.bind(Resource, lambda _: Resource())
    root.resolve(Resource)

    def hold_hierarchy_lock() -> None:
        with root._shutdown_lock:  # type: ignore[attr-defined]
            lock_acquired.set()
            assert release_lock.wait(timeout=5)

    holder = Thread(target=hold_hierarchy_lock, daemon=True)
    holder.start()
    assert lock_acquired.wait(timeout=5)
    with pytest.raises(ScopeDisposalError):
        root.close()
    release_lock.set()
    holder.join(timeout=5)

    root.close()


def test_new_shutdown_operation_resets_expired_descendant_deadline() -> None:
    class Resource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    root = Container()
    root.bind(Resource, lambda _: Resource(), scope=Scope.APPLICATION)
    application = root.application_scope()
    resource = application.resolve(Resource)
    application._shutdown_deadline = monotonic() - 1  # type: ignore[attr-defined]

    root.close()

    assert resource.closed


def test_parent_sync_close_defers_when_descendant_reveals_async_cleanup() -> None:
    class AsyncResource:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    entered = Event()
    release = Event()
    resource = AsyncResource()

    def factory(_: Container) -> AsyncResource:
        entered.set()
        assert release.wait(timeout=5)
        return resource

    root = Container()
    root.bind(AsyncResource, factory, scope=Scope.APPLICATION)
    application = root.application_scope()

    def resolve() -> AsyncResource:
        return application.resolve(AsyncResource)

    with ThreadPoolExecutor(max_workers=2) as executor:
        resolving = executor.submit(resolve)
        assert entered.wait(timeout=5)
        closing = executor.submit(root.close)
        assert not closing.done()
        release.set()
        with pytest.raises(ScopeDisposed):
            resolving.result(timeout=5)
        with pytest.raises(AsyncDisposalRequired):
            closing.result(timeout=5)

    assert not resource.closed
    asyncio.run(root.aclose())
    assert resource.closed


def test_parent_sync_preflight_does_not_partially_clean_siblings() -> None:
    class AsyncResource:
        async def aclose(self) -> None:
            pass

    class GoodResource:
        def close(self) -> None:
            good_closed.set()

    factory_entered = Event()
    release_factory = Event()
    good_closed = Event()

    def async_resource_factory(_: Container) -> AsyncResource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return AsyncResource()

    root = Container()
    root.bind(AsyncResource, async_resource_factory, scope=Scope.APPLICATION)
    root.bind(GoodResource, lambda _: GoodResource(), scope=Scope.APPLICATION)
    target_application = root.application_scope()
    good_application = root.application_scope()
    good_application.resolve(GoodResource)

    def resolve_target() -> AsyncResource:
        return target_application.resolve(AsyncResource)

    with ThreadPoolExecutor(max_workers=2) as executor:
        resolving = executor.submit(resolve_target)
        assert factory_entered.wait(timeout=5)
        closing = executor.submit(root.close)
        try:
            assert not good_closed.wait(timeout=0.2)
        finally:
            release_factory.set()
        with pytest.raises(ScopeDisposed):
            resolving.result(timeout=5)
        with pytest.raises(AsyncDisposalRequired):
            closing.result(timeout=5)

    assert not good_closed.is_set()
    asyncio.run(root.aclose())
    assert good_closed.is_set()


def test_descendant_cannot_publish_while_ancestor_shutdown_is_in_progress() -> None:
    class TargetResource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class BlockingResource:
        def close(self) -> None:
            blocker_entered.set()
            assert release_blocker.wait(timeout=5)

    factory_entered = Event()
    release_factory = Event()
    blocker_entered = Event()
    release_blocker = Event()
    target = TargetResource()

    def target_factory(_: Container) -> TargetResource:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return target

    root = Container()
    root.bind(TargetResource, target_factory, scope=Scope.APPLICATION)
    root.bind(BlockingResource, lambda _: BlockingResource(), scope=Scope.APPLICATION)
    target_application = root.application_scope()
    blocking_application = root.application_scope()
    blocking_application.resolve(BlockingResource)

    def resolve_target() -> TargetResource:
        return target_application.resolve(TargetResource)

    with ThreadPoolExecutor(max_workers=2) as executor:
        resolving = executor.submit(resolve_target)
        assert factory_entered.wait(timeout=5)
        closing = executor.submit(root.close)
        deadline = monotonic() + 5
        while root._state == "active" and monotonic() < deadline:  # type: ignore[attr-defined]
            sleep(0.001)
        assert root._state == "disposing"  # type: ignore[attr-defined]
        release_factory.set()
        try:
            with pytest.raises(ScopeDisposed):
                resolving.result(timeout=5)
        finally:
            release_blocker.set()
        closing.result(timeout=5)

    assert target.closed


def test_broader_lifetime_result_is_not_delivered_through_disposed_requester() -> None:
    class Service:
        pass

    entered = Event()
    release = Event()

    def factory(_: Container) -> Service:
        entered.set()
        assert release.wait(timeout=5)
        return Service()

    root = Container()
    root.bind(Service, factory, scope=Scope.SINGLETON)
    application = root.application_scope()

    def resolve() -> Service:
        return application.resolve(Service)

    with ThreadPoolExecutor(max_workers=2) as executor:
        resolving = executor.submit(resolve)
        assert entered.wait(timeout=5)
        closing = executor.submit(application.close)
        assert not closing.done()
        release.set()
        with pytest.raises(ScopeDisposed):
            resolving.result(timeout=5)
        closing.result(timeout=5)

    root.close()


def test_invocation_aclose_cancels_resolution_owned_by_broader_cache() -> None:
    class Service:
        pass

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def factory(_: Container) -> Service:
            entered.set()
            await release.wait()
            return Service()

        root = Container()
        root.bind_async(Service, factory, scope=Scope.SINGLETON)
        invocation = root.application_scope().invocation_scope()
        await invocation.__aenter__()
        resolving = invocation.create_task(invocation.resolve_async(Service))
        await entered.wait()

        await invocation.aclose()
        assert resolving.cancelled()
        await invocation.__aexit__(None, None, None)

        release.set()
        assert isinstance(await root.resolve_async(Service), Service)
        root.close()

    asyncio.run(scenario())


def test_concurrent_close_callers_join_one_cleanup_flight() -> None:
    entered = Event()
    release = Event()
    calls = 0

    class Resource:
        def close(self) -> None:
            nonlocal calls
            calls += 1
            entered.set()
            assert release.wait(timeout=5)

    root = Container()
    root.instance(Resource, Resource(), owned=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(root.close)
        assert entered.wait(timeout=5)
        second = executor.submit(root.close)
        assert not second.done()
        release.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert calls == 1


def test_concurrent_child_and_parent_close_share_one_cleanup_flight() -> None:
    entered = Event()
    release = Event()
    calls = 0
    outcomes: Queue[BaseException | None] = Queue()

    class Resource:
        def close(self) -> None:
            nonlocal calls
            calls += 1
            entered.set()
            assert release.wait(timeout=5)

    root = Container()
    root.bind(Resource, lambda _: Resource(), scope=Scope.APPLICATION)
    application = root.application_scope()
    application.resolve(Resource)

    def close(scope: Container) -> None:
        try:
            scope.close()
        except BaseException as error:
            outcomes.put(error)
        else:
            outcomes.put(None)

    child_closing = Thread(target=close, args=(application,), daemon=True)
    parent_closing = Thread(target=close, args=(root,), daemon=True)
    child_closing.start()
    assert entered.wait(timeout=5)
    parent_closing.start()
    try:
        sleep(0.05)
        assert calls == 1
    finally:
        release.set()
    child_closing.join(timeout=5)
    parent_closing.join(timeout=5)

    assert not child_closing.is_alive()
    assert not parent_closing.is_alive()
    assert calls == 1
    assert outcomes.get_nowait() is None
    assert outcomes.get_nowait() is None


def test_concurrent_sync_and_async_close_share_one_cleanup_flight() -> None:
    entered = Event()
    release = Event()
    calls = 0
    sync_outcome: Queue[BaseException | None] = Queue()

    class Resource:
        def close(self) -> None:
            nonlocal calls
            calls += 1
            entered.set()
            assert release.wait(timeout=5)

    root = Container()
    root.instance(Resource, Resource(), owned=True)

    def close_sync() -> None:
        try:
            root.close()
        except BaseException as error:
            sync_outcome.put(error)
        else:
            sync_outcome.put(None)

    sync_closing = Thread(target=close_sync, daemon=True)
    sync_closing.start()
    assert entered.wait(timeout=5)

    async def close_async() -> None:
        closing = asyncio.create_task(root.aclose())
        await asyncio.sleep(0.05)
        assert calls == 1
        release.set()
        await closing

    asyncio.run(close_async())
    sync_closing.join(timeout=5)

    assert not sync_closing.is_alive()
    assert calls == 1
    assert sync_outcome.get_nowait() is None


def test_sync_disposer_reentrant_close_fails_without_deadlock() -> None:
    outcomes: Queue[BaseException | None] = Queue()
    root = Container()

    class Resource:
        def close(self) -> None:
            root.close()

    root.instance(Resource, Resource(), owned=True)

    def close() -> None:
        try:
            root.close()
        except BaseException as error:
            outcomes.put(error)
        else:
            outcomes.put(None)

    closing = Thread(target=close, daemon=True)
    closing.start()
    closing.join(timeout=1)

    assert not closing.is_alive()
    outcome = outcomes.get_nowait()
    assert isinstance(outcome, ScopeDisposalError)
    assert isinstance(outcome.failures[0], InvalidBinding)


def test_descendant_disposer_cannot_reenter_ancestor_shutdown() -> None:
    calls = 0
    root = Container()

    class Resource:
        def close(self) -> None:
            nonlocal calls
            calls += 1
            root.close()

    root.bind(Resource, lambda _: Resource(), scope=Scope.INVOCATION)
    invocation = root.application_scope().invocation_scope()

    with pytest.raises(ScopeDisposalError) as captured, invocation:
        invocation.resolve(Resource)

    assert calls == 1
    assert isinstance(captured.value.failures[0], InvalidBinding)
    root.close()


def test_async_disposer_reentrant_aclose_fails_without_deadlock() -> None:
    outcomes: Queue[BaseException | None] = Queue()
    root = Container()

    class Resource:
        async def aclose(self) -> None:
            await root.aclose()

    root.instance(Resource, Resource(), owned=True)

    def close() -> None:
        try:
            asyncio.run(root.aclose())
        except BaseException as error:
            outcomes.put(error)
        else:
            outcomes.put(None)

    closing = Thread(target=close, daemon=True)
    closing.start()
    closing.join(timeout=1)

    assert not closing.is_alive()
    outcome = outcomes.get_nowait()
    assert isinstance(outcome, ScopeDisposalError)
    assert isinstance(outcome.failures[0], InvalidBinding)


def test_unexpected_async_cleanup_failure_always_settles_close_future() -> None:
    class CleanupFailure(BaseException):
        pass

    class FaultyContainer(Container):
        async def _wait_for_constructions_async(self) -> bool:
            raise CleanupFailure("fault injection")

    outcomes: Queue[BaseException | None] = Queue()
    root = FaultyContainer()

    def close() -> None:
        try:
            asyncio.run(root.aclose())
        except BaseException as error:
            outcomes.put(error)
        else:
            outcomes.put(None)

    closing = Thread(target=close, daemon=True)
    closing.start()
    closing.join(timeout=1)

    assert not closing.is_alive()
    outcome = outcomes.get_nowait()
    assert isinstance(outcome, ScopeDisposalError)
    assert isinstance(outcome.failures[0], CleanupFailure)


def test_scope_owned_task_cannot_await_its_own_scope_shutdown() -> None:
    outcomes: Queue[BaseException | None] = Queue()

    def run() -> None:
        async def scenario() -> None:
            invocation = Container().application_scope().invocation_scope()
            invocation.__enter__()

            async def worker() -> None:
                await invocation.aclose()

            task = invocation.create_task(worker())
            with pytest.raises(InvalidBinding, match="owned task"):
                await task
            with pytest.raises(ScopeDisposalError) as captured:
                await invocation.aclose()
            assert isinstance(captured.value.failures[0], InvalidBinding)
            with pytest.raises(ScopeDisposalError):
                invocation.__exit__(None, None, None)

        try:
            asyncio.run(scenario())
        except BaseException as error:
            outcomes.put(error)
        else:
            outcomes.put(None)

    closing = Thread(target=run, daemon=True)
    closing.start()
    closing.join(timeout=1)

    assert not closing.is_alive()
    assert outcomes.get_nowait() is None


def test_entered_invocation_rejects_cleanup_from_foreign_event_loop() -> None:
    ready = Event()
    loop_queue: Queue[asyncio.AbstractEventLoop] = Queue()
    release_queue: Queue[asyncio.Event] = Queue()
    invocation_queue: Queue[Container] = Queue()
    outcomes: Queue[BaseException | None] = Queue()

    def run_owner_loop() -> None:
        async def scenario() -> None:
            release = asyncio.Event()
            loop = asyncio.get_running_loop()
            invocation = Container().application_scope().invocation_scope()
            invocation.__enter__()
            loop_queue.put(loop)
            release_queue.put(release)
            invocation_queue.put(invocation)

            async def worker() -> None:
                ready.set()
                await release.wait()

            task = invocation.create_task(worker())
            await task
            await invocation.aclose()
            invocation.__exit__(None, None, None)

        try:
            asyncio.run(scenario())
        except BaseException as error:
            outcomes.put(error)
        else:
            outcomes.put(None)

    owner = Thread(target=run_owner_loop, daemon=True)
    owner.start()
    assert ready.wait(timeout=5)
    loop = loop_queue.get_nowait()
    release = release_queue.get_nowait()
    invocation = invocation_queue.get_nowait()
    try:
        with pytest.raises(InvalidBinding, match="external shutdown"):
            asyncio.run(invocation.aclose())
    finally:
        loop.call_soon_threadsafe(release.set)
    owner.join(timeout=5)

    assert not owner.is_alive()
    assert outcomes.get_nowait() is None


def test_async_resource_retains_event_loop_affinity_through_cleanup() -> None:
    ready = Event()
    loop_queue: Queue[asyncio.AbstractEventLoop] = Queue()
    release_queue: Queue[asyncio.Event] = Queue()
    outcomes: Queue[BaseException | None] = Queue()
    root = Container()

    class Resource:
        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            self.loop = loop
            self.closed = False

        async def aclose(self) -> None:
            if asyncio.get_running_loop() is not self.loop:
                raise RuntimeError("wrong cleanup loop")
            self.closed = True

    resource_queue: Queue[Resource] = Queue()

    async def factory(_: Container) -> Resource:
        resource = Resource(asyncio.get_running_loop())
        resource_queue.put(resource)
        return resource

    root.bind_async(Resource, factory, scope=Scope.SINGLETON)

    def run_owner_loop() -> None:
        async def scenario() -> None:
            release = asyncio.Event()
            loop = asyncio.get_running_loop()
            await root.resolve_async(Resource)
            loop_queue.put(loop)
            release_queue.put(release)
            ready.set()
            await release.wait()
            await root.aclose()

        try:
            asyncio.run(scenario())
        except BaseException as error:
            outcomes.put(error)
        else:
            outcomes.put(None)

    owner = Thread(target=run_owner_loop, daemon=True)
    owner.start()
    assert ready.wait(timeout=5)
    loop = loop_queue.get_nowait()
    release = release_queue.get_nowait()
    resource = resource_queue.get_nowait()
    try:
        with pytest.raises(InvalidBinding, match="event loop"):
            asyncio.run(root.aclose())
    finally:
        loop.call_soon_threadsafe(release.set)
    owner.join(timeout=5)

    assert not owner.is_alive()
    assert outcomes.get_nowait() is None
    assert resource.closed


@pytest.mark.parametrize("registration", ["resolve", "instance"])
def test_sync_ownership_acquired_on_loop_retains_async_cleanup_affinity(
    registration: str,
) -> None:
    ready = Event()
    loop_queue: Queue[asyncio.AbstractEventLoop] = Queue()
    release_queue: Queue[asyncio.Event] = Queue()
    resource_queue: Queue[object] = Queue()
    outcomes: Queue[BaseException | None] = Queue()
    root = Container()

    class Resource:
        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            self.loop = loop
            self.closed = False

        async def aclose(self) -> None:
            if asyncio.get_running_loop() is not self.loop:
                raise RuntimeError("wrong cleanup loop")
            self.closed = True

    if registration == "resolve":
        root.bind(Resource, lambda _: Resource(asyncio.get_running_loop()))

    def run_owner_loop() -> None:
        async def scenario() -> None:
            release = asyncio.Event()
            loop = asyncio.get_running_loop()
            if registration == "resolve":
                resource = root.resolve(Resource)
            else:
                resource = Resource(loop)
                root.instance(Resource, resource, owned=True)
            resource_queue.put(resource)
            loop_queue.put(loop)
            release_queue.put(release)
            ready.set()
            await release.wait()
            await root.aclose()

        try:
            asyncio.run(scenario())
        except BaseException as error:
            outcomes.put(error)
        else:
            outcomes.put(None)

    owner = Thread(target=run_owner_loop, daemon=True)
    owner.start()
    assert ready.wait(timeout=5)
    loop = loop_queue.get_nowait()
    release = release_queue.get_nowait()
    resource = resource_queue.get_nowait()
    try:
        with pytest.raises(InvalidBinding, match="event loop"):
            asyncio.run(root.aclose())
    finally:
        loop.call_soon_threadsafe(release.set)
    owner.join(timeout=5)

    assert not owner.is_alive()
    assert outcomes.get_nowait() is None
    assert isinstance(resource, Resource)
    assert resource.closed


def test_failed_cross_loop_instance_registration_publishes_no_state() -> None:
    ready = Event()
    release_queue: Queue[asyncio.Event] = Queue()
    loop_queue: Queue[asyncio.AbstractEventLoop] = Queue()
    outcomes: Queue[BaseException | None] = Queue()
    root = Container()

    class Resource:
        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            self.loop = loop
            self.closed = False

        async def aclose(self) -> None:
            if asyncio.get_running_loop() is not self.loop:
                raise RuntimeError("wrong cleanup loop")
            self.closed = True

    def run_owner_loop() -> None:
        async def scenario() -> None:
            release = asyncio.Event()
            root.instance(Resource, Resource(asyncio.get_running_loop()), owned=True)
            loop_queue.put(asyncio.get_running_loop())
            release_queue.put(release)
            ready.set()
            await release.wait()
            await root.aclose()

        try:
            asyncio.run(scenario())
        except BaseException as error:
            outcomes.put(error)
        else:
            outcomes.put(None)

    owner = Thread(target=run_owner_loop, daemon=True)
    owner.start()
    assert ready.wait(timeout=5)
    loop = loop_queue.get_nowait()
    release = release_queue.get_nowait()

    async def reject_foreign_instance() -> Resource:
        resource = Resource(asyncio.get_running_loop())
        with pytest.raises(InvalidBinding, match="event loop"):
            root.instance(Resource, resource, qualifier="foreign", owned=True)
        return resource

    foreign = asyncio.run(reject_foreign_instance())
    with pytest.raises(ServiceNotBound):
        root.resolve(Resource, qualifier="foreign")
    assert len(root._resources) == 1  # type: ignore[attr-defined]
    assert not foreign.closed

    loop.call_soon_threadsafe(release.set)
    owner.join(timeout=5)
    assert not owner.is_alive()
    assert outcomes.get_nowait() is None


def test_child_async_resolution_checks_singleton_cache_owner_loop() -> None:
    ready = Event()
    release_queue: Queue[asyncio.Event] = Queue()
    loop_queue: Queue[asyncio.AbstractEventLoop] = Queue()
    outcomes: Queue[BaseException | None] = Queue()
    root = Container()

    class Resource:
        async def aclose(self) -> None:
            pass

    root.bind(Resource, lambda _: Resource(), scope=Scope.SINGLETON)
    invocation = root.application_scope().invocation_scope()

    def run_owner_loop() -> None:
        async def scenario() -> None:
            release = asyncio.Event()
            root.resolve(Resource)
            loop_queue.put(asyncio.get_running_loop())
            release_queue.put(release)
            ready.set()
            await release.wait()
            await root.aclose()

        try:
            asyncio.run(scenario())
        except BaseException as error:
            outcomes.put(error)
        else:
            outcomes.put(None)

    owner = Thread(target=run_owner_loop, daemon=True)
    owner.start()
    assert ready.wait(timeout=5)
    loop = loop_queue.get_nowait()
    release = release_queue.get_nowait()

    async def reject_foreign_loop() -> None:
        async with invocation:
            with pytest.raises(InvalidBinding, match="event loop"):
                invocation.resolve(Resource)
            with pytest.raises(InvalidBinding, match="event loop"):
                await invocation.resolve_async(Resource)

    try:
        asyncio.run(reject_foreign_loop())
    finally:
        loop.call_soon_threadsafe(release.set)
    owner.join(timeout=5)

    assert not owner.is_alive()
    assert outcomes.get_nowait() is None


def test_factory_cannot_close_the_scope_owning_its_construction() -> None:
    class Service:
        pass

    outcomes: Queue[BaseException | object] = Queue()
    root = Container()

    def factory(_: Container) -> Service:
        root.close()
        return Service()

    root.bind(Service, factory)

    def resolve() -> None:
        try:
            outcomes.put(root.resolve(Service))
        except BaseException as error:
            outcomes.put(error)

    resolving = Thread(target=resolve, daemon=True)
    resolving.start()
    resolving.join(timeout=1)

    assert not resolving.is_alive()
    assert isinstance(outcomes.get_nowait(), InvalidBinding)
    root.close()


def test_sync_close_does_not_block_the_loop_running_async_cleanup() -> None:
    outcomes: Queue[BaseException | None] = Queue()

    def run() -> None:
        async def scenario() -> None:
            entered = asyncio.Event()
            release = asyncio.Event()

            class Resource:
                async def aclose(self) -> None:
                    entered.set()
                    await release.wait()

            root = Container()
            root.instance(Resource, Resource(), owned=True)
            closing = asyncio.create_task(root.aclose())
            await entered.wait()
            try:
                with pytest.raises(AsyncDisposalRequired):
                    root.close()
            finally:
                release.set()
            await closing

        try:
            asyncio.run(scenario())
        except BaseException as error:
            outcomes.put(error)
        else:
            outcomes.put(None)

    closing = Thread(target=run, daemon=True)
    closing.start()
    closing.join(timeout=1)

    assert not closing.is_alive()
    assert outcomes.get_nowait() is None


def test_failed_async_construction_can_retry_on_a_later_event_loop() -> None:
    class Service:
        pass

    calls = 0

    async def factory(_: Container) -> Service:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("retry on next loop")
        return Service()

    container = Container()
    container.bind_async(Service, factory, scope=Scope.SINGLETON)
    with pytest.raises(RuntimeError, match="next loop"):
        asyncio.run(container.resolve_async(Service))

    assert isinstance(asyncio.run(container.resolve_async(Service)), Service)
    assert calls == 2


def test_eager_async_flight_completion_does_not_leave_phantom_inflight_work() -> None:
    class Service:
        pass

    async def factory(_: Container) -> Service:
        return Service()

    async def scenario() -> Container:
        loop = asyncio.get_running_loop()
        previous_factory = loop.get_task_factory()
        loop.set_task_factory(asyncio.eager_task_factory)
        try:
            container = Container()
            container.bind_async(Service, factory, scope=Scope.SINGLETON)
            assert isinstance(await container.resolve_async(Service), Service)
            return container
        finally:
            loop.set_task_factory(previous_factory)

    container = asyncio.run(scenario())
    container.close()


def test_eager_owned_task_is_registered_before_user_coroutine_runs() -> None:
    async def scenario() -> None:
        invocation = Container().application_scope().invocation_scope()
        invocation.__enter__()
        loop = asyncio.get_running_loop()
        previous_factory = loop.get_task_factory()
        loop.set_task_factory(asyncio.eager_task_factory)

        async def worker() -> None:
            assert asyncio.current_task() in invocation._tasks  # type: ignore[attr-defined]

        try:
            task = invocation.create_task(worker())
            await task
        finally:
            loop.set_task_factory(previous_factory)
            await invocation.aclose()
            invocation.__exit__(None, None, None)

    asyncio.run(scenario())


def test_eager_async_cleanup_does_not_resurrect_completed_task_reference() -> None:
    async def scenario() -> None:
        root = Container()
        loop = asyncio.get_running_loop()
        previous_factory = loop.get_task_factory()
        loop.set_task_factory(asyncio.eager_task_factory)
        try:
            await root.aclose()
            assert root._cleanup_task is None  # type: ignore[attr-defined]
        finally:
            loop.set_task_factory(previous_factory)

    asyncio.run(scenario())


def test_sync_task_creation_without_running_loop_closes_coroutine() -> None:
    invocation = Container().application_scope().invocation_scope()
    invocation.__enter__()

    async def worker() -> None:
        pass

    coroutine = worker()
    try:
        with pytest.raises(ScopeUnavailable, match="event loop"):
            invocation.create_task(coroutine)
        assert coroutine.cr_frame is None
    finally:
        invocation.__exit__(None, None, None)


def test_disposed_scope_does_not_retain_async_cleanup_event_loop() -> None:
    root = Container()
    loop = asyncio.new_event_loop()
    loop_reference = weakref.ref(loop)
    loop.run_until_complete(root.aclose())
    loop.close()
    del loop
    gc.collect()

    assert loop_reference() is None


def test_cross_thread_dependency_cycle_fails_instead_of_deadlocking() -> None:
    class ServiceA:
        pass

    class ServiceB:
        pass

    barrier = Barrier(2)
    outcomes: Queue[BaseException | object] = Queue()
    container = Container()

    def create_a(current: Container) -> object:
        barrier.wait()
        return current.resolve(ServiceB)

    def create_b(current: Container) -> object:
        barrier.wait()
        return current.resolve(ServiceA)

    container.bind(ServiceA, create_a, scope=Scope.SINGLETON)
    container.bind(ServiceB, create_b, scope=Scope.SINGLETON)

    def resolve(service: type[object]) -> None:
        try:
            outcomes.put(container.resolve(service))
        except BaseException as error:
            outcomes.put(error)

    threads = [
        Thread(target=resolve, args=(ServiceA,), daemon=True),
        Thread(target=resolve, args=(ServiceB,), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    assert all(isinstance(outcomes.get_nowait(), CircularDependency) for _ in threads)


def test_cross_scope_owner_cycle_fails_instead_of_deadlocking() -> None:
    class SingletonService:
        pass

    class ApplicationService:
        pass

    barrier = Barrier(2)
    outcomes: Queue[BaseException | object] = Queue()
    root = Container()

    def create_singleton(_: Container) -> object:
        barrier.wait(timeout=5)
        return application.resolve(ApplicationService)

    def create_application(_: Container) -> object:
        barrier.wait(timeout=5)
        return root.resolve(SingletonService)

    root.bind(SingletonService, create_singleton, scope=Scope.SINGLETON)
    root.bind(ApplicationService, create_application, scope=Scope.APPLICATION)
    application = root.application_scope()

    def resolve(container: Container, service: type[object]) -> None:
        try:
            outcomes.put(container.resolve(service))
        except BaseException as error:
            outcomes.put(error)

    threads = [
        Thread(target=resolve, args=(root, SingletonService), daemon=True),
        Thread(target=resolve, args=(application, ApplicationService), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert not any(thread.is_alive() for thread in threads)
    assert all(isinstance(outcomes.get_nowait(), CircularDependency) for _ in threads)


def test_cross_task_dependency_cycle_fails_instead_of_deadlocking() -> None:
    class ServiceA:
        pass

    class ServiceB:
        pass

    async def scenario() -> list[object]:
        a_entered = asyncio.Event()
        b_entered = asyncio.Event()
        container = Container()

        async def create_a(current: Container) -> object:
            a_entered.set()
            await b_entered.wait()
            return await current.resolve_async(ServiceB)

        async def create_b(current: Container) -> object:
            b_entered.set()
            await a_entered.wait()
            return await current.resolve_async(ServiceA)

        container.bind_async(ServiceA, create_a, scope=Scope.SINGLETON)
        container.bind_async(ServiceB, create_b, scope=Scope.SINGLETON)
        return await asyncio.wait_for(
            asyncio.gather(
                container.resolve_async(ServiceA),
                container.resolve_async(ServiceB),
                return_exceptions=True,
            ),
            timeout=5,
        )

    outcomes = asyncio.run(scenario())
    assert all(isinstance(outcome, CircularDependency) for outcome in outcomes)


def test_duplicate_wait_edges_remain_until_every_waiter_retires() -> None:
    container = Container()
    first_flight = object()
    second_flight = object()
    first_key = container.key("first")
    second_key = container.key("second")
    container._flight_services[first_flight] = first_key  # type: ignore[attr-defined]
    container._flight_services[second_flight] = second_key  # type: ignore[attr-defined]

    assert container._add_wait_edge(first_flight, second_flight) is None  # type: ignore[attr-defined]
    assert container._add_wait_edge(first_flight, second_flight) is None  # type: ignore[attr-defined]
    container._remove_wait_edge(first_flight, second_flight)  # type: ignore[attr-defined]

    cycle = container._add_wait_edge(second_flight, first_flight)  # type: ignore[attr-defined]
    assert cycle == (second_key, first_key, second_key)


def test_invocation_scope_rejects_concurrent_context_manager_entry() -> None:
    async def scenario() -> None:
        invocation = (
            Container()
            .application_scope()
            .invocation_scope(context=ExecutionContext(tenant_id="one"))
        )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def owner() -> None:
            async with invocation:
                entered.set()
                await release.wait()

        task = asyncio.create_task(owner())
        await entered.wait()
        with pytest.raises(InvalidBinding, match="already entered"):
            async with invocation:
                pass
        release.set()
        await task

    asyncio.run(scenario())


def test_context_manager_entry_rechecks_state_after_concurrent_close() -> None:
    validated = Event()
    resume_shutdown = Event()
    entry_started = Event()
    entry_outcomes: Queue[BaseException | None] = Queue()
    close_outcomes: Queue[BaseException | None] = Queue()

    class RacingContainer(Container):
        def _assert_no_external_entry_owner_in_tree(
            self,
            task: asyncio.Task[object] | None,
            thread_id: int,
        ) -> None:
            super()._assert_no_external_entry_owner_in_tree(task, thread_id)
            validated.set()
            assert resume_shutdown.wait(timeout=5)

    root = RacingContainer()
    invocation = root.application_scope().invocation_scope()

    def close() -> None:
        try:
            root.close()
        except BaseException as error:
            close_outcomes.put(error)
        else:
            close_outcomes.put(None)

    def enter() -> None:
        entry_started.set()
        try:
            invocation.__enter__()
        except BaseException as error:
            entry_outcomes.put(error)
        else:
            entry_outcomes.put(None)

    closer = Thread(target=close, daemon=True)
    entrant = Thread(target=enter, daemon=True)
    closer.start()
    assert validated.wait(timeout=5)
    entrant.start()
    assert entry_started.wait(timeout=5)
    sleep(0.01)
    assert entrant.is_alive()
    resume_shutdown.set()
    closer.join(timeout=5)
    entrant.join(timeout=5)

    assert not closer.is_alive()
    assert not entrant.is_alive()
    assert close_outcomes.get_nowait() is None
    assert isinstance(entry_outcomes.get_nowait(), ScopeDisposed)


def test_async_shutdown_revalidates_owner_after_await_boundary() -> None:
    class GatedContainer(Container):
        def __init__(self) -> None:
            super().__init__()
            self.drain_reached = asyncio.Event()
            self.resume_drain = asyncio.Event()

        async def _drain_late_async_resources(self, loop: asyncio.AbstractEventLoop) -> None:
            self.drain_reached.set()
            await self.resume_drain.wait()
            await super()._drain_late_async_resources(loop)

    async def scenario() -> None:
        root = GatedContainer()
        invocation = root.application_scope().invocation_scope()
        cleanup = asyncio.create_task(root.aclose())
        await root.drain_reached.wait()
        await invocation.__aenter__()
        root.resume_drain.set()

        with pytest.raises(InvalidBinding, match="external shutdown"):
            await cleanup
        await invocation.__aexit__(None, None, None)
        root.close()

    asyncio.run(scenario())


def test_async_invocation_entry_does_not_block_loop_during_sync_shutdown() -> None:
    cleanup_entered = Event()
    release_cleanup = Event()

    class Resource:
        def close(self) -> None:
            cleanup_entered.set()
            assert release_cleanup.wait(timeout=5)

    root = Container(shutdown_timeout=0.2)
    root.instance(Resource, Resource(), owned=True)
    invocation = root.application_scope().invocation_scope()
    closer = Thread(target=root.close, daemon=True)
    closer.start()
    assert cleanup_entered.wait(timeout=5)

    async def scenario() -> None:
        loop_progressed = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.call_later(0.01, loop_progressed.set)
        loop.call_later(0.05, release_cleanup.set)
        with pytest.raises(ScopeDisposed):
            await invocation.__aenter__()
        await asyncio.wait_for(loop_progressed.wait(), timeout=0.1)

    asyncio.run(scenario())
    closer.join(timeout=5)
    assert not closer.is_alive()


def test_async_shutdown_lock_wait_is_bounded_by_hierarchy_deadline() -> None:
    lock_acquired = Event()
    release_lock = Event()
    root = Container(shutdown_timeout=0.02)

    def hold_lock() -> None:
        with root._shutdown_lock:  # type: ignore[attr-defined]
            lock_acquired.set()
            assert release_lock.wait(timeout=5)

    holder = Thread(target=hold_lock, daemon=True)
    holder.start()
    assert lock_acquired.wait(timeout=5)
    try:
        with pytest.raises(ScopeDisposalError) as captured:
            asyncio.run(root.aclose())
    finally:
        release_lock.set()
        holder.join(timeout=5)

    assert any(isinstance(error, ShutdownTimeout) for error in captured.value.failures)


def test_shutdown_cancellation_precedes_post_lock_validation_failure() -> None:
    class CancelledValidationContainer(Container):
        def __init__(self) -> None:
            super().__init__()
            self.validations = 0

        def _assert_no_external_entry_owner_in_tree(
            self,
            task: asyncio.Task[object] | None,
            thread_id: int,
        ) -> None:
            del task, thread_id
            self.validations += 1
            if self.validations == 2:
                raise InvalidBinding("post-lock validation failure")

        async def _acquire_shutdown_lock_async(self) -> asyncio.CancelledError | None:
            self._shutdown_lock.acquire()  # type: ignore[attr-defined]
            return asyncio.CancelledError()

    async def scenario() -> None:
        root = CancelledValidationContainer()
        with pytest.raises(asyncio.CancelledError):
            await root.aclose()

    asyncio.run(scenario())


def test_async_contextual_factory_uses_immediate_consumer() -> None:
    class Store:
        pass

    class Consumer:
        def __init__(self, store: Store) -> None:
            self.store = store

    default = Store()
    contextual = Store()

    async def create_consumer(current: Container) -> Consumer:
        return Consumer(await current.resolve_async(Store))

    async def create_contextual(_: Container) -> Store:
        return contextual

    container = Container()
    container.bind(Store, lambda _: default)
    container.bind_async(Consumer, create_consumer)
    container.bind_contextual_async(Consumer, Store, create_contextual)

    consumer = asyncio.run(container.resolve_async(Consumer))
    assert consumer.store is contextual
    assert container.resolve(Store) is default


def test_freeze_rejects_active_factory_before_publishing_registry() -> None:
    cleared = Event()
    release_freeze = Event()
    consumer_entered = Event()
    resolve_dependency = Event()

    class BlockingContextualDict(dict[object, object]):
        def clear(self) -> None:
            super().clear()
            cleared.set()
            assert release_freeze.wait(timeout=5)

    class Store:
        pass

    class Consumer:
        def __init__(self, store: Store) -> None:
            self.store = store

    default = Store()
    contextual = Store()

    def create_consumer(current: Container) -> Consumer:
        consumer_entered.set()
        assert resolve_dependency.wait(timeout=5)
        return Consumer(current.resolve(Store))

    container = Container(_contextual=BlockingContextualDict())  # type: ignore[arg-type]
    container.bind(Store, lambda _: default)
    container.bind(Consumer, create_consumer)
    container.bind_contextual(Consumer, Store, lambda _: contextual)

    with ThreadPoolExecutor(max_workers=2) as executor:
        resolving = executor.submit(container.resolve, Consumer)
        assert consumer_entered.wait(timeout=5)
        freezing = executor.submit(container.freeze)
        with pytest.raises(InvalidBinding, match="factory"):
            freezing.result(timeout=5)
        assert not cleared.is_set()
        resolve_dependency.set()
        assert resolving.result(timeout=5).store is contextual

    release_freeze.set()
    container.freeze()
    assert cleared.is_set()


def test_registry_mutation_rechecks_factory_activity_at_publication() -> None:
    mutation_checked = Event()
    publish_mutation = Event()
    factory_entered = Event()
    release_factory = Event()
    mutation_outcomes: Queue[BaseException | None] = Queue()

    class PausingContainer(Container):
        pause_mutation = False

        def _assert_registry_mutation_outside_factory(self) -> None:
            super()._assert_registry_mutation_outside_factory()
            if self.pause_mutation:
                mutation_checked.set()
                assert publish_mutation.wait(timeout=5)

    class Service:
        pass

    class AddedDuringFactory:
        pass

    def create_service(_: Container) -> Service:
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return Service()

    container = PausingContainer()
    container.bind(Service, create_service)
    container.pause_mutation = True

    def mutate() -> None:
        try:
            container.bind(AddedDuringFactory, lambda _: AddedDuringFactory())
        except BaseException as error:
            mutation_outcomes.put(error)
        else:
            mutation_outcomes.put(None)

    mutating = Thread(target=mutate, daemon=True)
    resolving = Thread(target=container.resolve, args=(Service,), daemon=True)
    mutating.start()
    assert mutation_checked.wait(timeout=5)
    resolving.start()
    assert factory_entered.wait(timeout=5)
    publish_mutation.set()
    mutating.join(timeout=5)
    release_factory.set()
    resolving.join(timeout=5)

    assert not mutating.is_alive()
    assert not resolving.is_alive()
    assert isinstance(mutation_outcomes.get_nowait(), InvalidBinding)
    with pytest.raises(ServiceNotBound):
        container.resolve(AddedDuringFactory)


def test_foreign_task_cannot_resolve_through_entered_context_scope() -> None:
    class Service:
        def __init__(self, tenant_id: str | None) -> None:
            self.tenant_id = tenant_id

    async def scenario() -> None:
        release_foreign = asyncio.Event()
        root = Container()
        root.bind(
            Service,
            lambda _: Service(
                None if maybe_current_context() is None else maybe_current_context().tenant_id
            ),
            scope=Scope.INVOCATION,
        )
        invocation = root.application_scope().invocation_scope(
            context=ExecutionContext(tenant_id="owner")
        )

        async def foreign() -> Service:
            await release_foreign.wait()
            return invocation.resolve(Service)

        foreign_task = asyncio.create_task(foreign())
        async with invocation:
            release_foreign.set()
            with pytest.raises(ScopeUnavailable, match="active invocation"):
                await foreign_task
            assert invocation.resolve(Service).tenant_id == "owner"

    asyncio.run(scenario())


def test_foreign_task_cannot_read_entered_invocation_context() -> None:
    async def scenario() -> None:
        release_foreign = asyncio.Event()
        invocation = (
            Container()
            .application_scope()
            .invocation_scope(context=ExecutionContext(tenant_id="owner"))
        )

        async def foreign() -> ExecutionContext:
            await release_foreign.wait()
            return invocation.context

        foreign_task = asyncio.create_task(foreign())
        async with invocation:
            release_foreign.set()
            with pytest.raises(ScopeUnavailable, match="active invocation"):
                await foreign_task
            assert invocation.context.tenant_id == "owner"

    asyncio.run(scenario())


def test_raw_child_task_does_not_inherit_invocation_authority() -> None:
    class Service:
        pass

    async def scenario() -> None:
        root = Container()
        root.bind(Service, lambda _: Service(), scope=Scope.INVOCATION)
        invocation = root.application_scope().invocation_scope(
            context=ExecutionContext(tenant_id="owner")
        )

        async def raw_child() -> None:
            with pytest.raises(PlatformError):
                current_context()
            with pytest.raises(ScopeUnavailable, match="active invocation"):
                invocation.resolve(Service)

        async with invocation:
            await asyncio.create_task(raw_child())

    asyncio.run(scenario())


def test_invocation_cannot_adopt_task_from_another_active_context() -> None:
    async def scenario() -> None:
        application = Container().application_scope()
        first = application.invocation_scope(context=ExecutionContext(tenant_id="first"))
        second = application.invocation_scope(context=ExecutionContext(tenant_id="second"))

        async def worker() -> str | None:
            context = maybe_current_context()
            return None if context is None else context.tenant_id

        async with first, second:
            with pytest.raises(ScopeUnavailable, match="active invocation"):
                first.create_task(worker())

    asyncio.run(scenario())


def test_factory_resolver_rejects_use_without_its_resolution_frame() -> None:
    class Store:
        pass

    class Consumer:
        pass

    default = Store()
    contextual = Store()
    outcomes: Queue[object] = Queue()

    def create_consumer(current: Container) -> Consumer:
        def resolve_without_context() -> None:
            try:
                outcomes.put(current.resolve(Store))
            except BaseException as error:
                outcomes.put(error)

        worker = Thread(target=resolve_without_context, daemon=True)
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()
        return Consumer()

    root = Container()
    root.bind(Store, lambda _: default)
    root.bind(Consumer, create_consumer)
    root.bind_contextual(Consumer, Store, lambda _: contextual)

    root.resolve(Consumer)
    outcome = outcomes.get_nowait()
    assert isinstance(outcome, InvalidBinding)
    assert "resolver" in str(outcome)


def test_async_resolution_constructs_sync_factory_on_caller_thread() -> None:
    class Resource:
        def __init__(self) -> None:
            self.created_on = get_ident()
            self.closed_on: int | None = None

        def close(self) -> None:
            self.closed_on = get_ident()

    async def scenario() -> None:
        caller_thread = get_ident()
        root = Container()
        root.bind(Resource, lambda _: Resource(), scope=Scope.SINGLETON)

        resource = await root.resolve_async(Resource)
        assert resource.created_on == caller_thread
        await root.aclose()
        assert resource.closed_on is not None

    asyncio.run(scenario())


def test_sync_factory_rejection_cancels_returned_pending_task() -> None:
    class Service:
        pass

    async def scenario() -> None:
        release = asyncio.Event()
        returned_task = asyncio.create_task(release.wait())
        root = Container()
        root.bind(Service, lambda _: returned_task)

        with pytest.raises(AsyncResolutionRequired):
            await root.resolve_async(Service)
        await asyncio.sleep(0)

        assert returned_task.cancelled()
        root.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failed", [False, True])
def test_sync_factory_rejection_preserves_error_for_future_on_closed_loop(
    failed: bool,
) -> None:
    class Service:
        pass

    loop = asyncio.new_event_loop()
    returned = loop.create_future()
    if failed:
        returned.set_exception(RuntimeError("private future failure"))
    loop.close()
    root = Container()
    root.bind(Service, lambda _: returned)

    with pytest.raises(AsyncResolutionRequired):
        root.resolve(Service)

    assert returned.done() or returned.cancelled()
    root.close()
