from __future__ import annotations

import asyncio
import gc
import traceback
import weakref
from time import monotonic, sleep

import pytest

from af.framework.container import Container, Scope
from af.framework.context import (
    ExecutionContext,
    current_context,
    maybe_current_context,
    use_context,
)
from af.framework.errors import (
    AsyncDisposalRequired,
    InvalidBinding,
    PlatformError,
    ScopeDisposalError,
    ScopeDisposed,
    ScopeUnavailable,
    ShutdownTimeout,
)


def test_scope_disposes_owned_resources_once_in_reverse_creation_order() -> None:
    closed: list[str] = []

    class Dependency:
        def close(self) -> None:
            closed.append("dependency")

    class Consumer:
        def __init__(self, dependency: Dependency) -> None:
            self.dependency = dependency

        def close(self) -> None:
            closed.append("consumer")

    root = Container()
    root.bind(Dependency, lambda _: Dependency(), scope=Scope.TRANSIENT)
    root.bind(
        Consumer,
        lambda current: Consumer(current.resolve(Dependency)),
        scope=Scope.INVOCATION,
    )
    application = root.application_scope()
    invocation = application.invocation_scope()

    with invocation:
        assert invocation.resolve(Consumer) is invocation.resolve(Consumer)

    assert closed == ["consumer", "dependency"]
    invocation.close()
    assert closed == ["consumer", "dependency"]
    with pytest.raises(ScopeDisposed):
        invocation.resolve(Consumer)


def test_async_close_unwinds_mixed_resources_and_sync_close_fails_before_cleanup() -> None:
    closed: list[str] = []

    class SyncResource:
        def close(self) -> None:
            closed.append("sync")

    class AsyncResource:
        async def aclose(self) -> None:
            await asyncio.sleep(0)
            closed.append("async")

    root = Container()
    root.bind(SyncResource, lambda _: SyncResource(), scope=Scope.INVOCATION)
    root.bind(AsyncResource, lambda _: AsyncResource(), scope=Scope.INVOCATION)
    invocation = root.application_scope().invocation_scope()

    with pytest.raises(AsyncDisposalRequired), invocation:
        invocation.resolve(SyncResource)
        invocation.resolve(AsyncResource)
    assert closed == []

    asyncio.run(invocation.aclose())
    assert closed == ["async", "sync"]


def test_scoped_bindings_reject_resolution_without_the_required_scope() -> None:
    class ApplicationService:
        pass

    class InvocationService:
        pass

    root = Container()
    root.bind(ApplicationService, lambda _: ApplicationService(), scope=Scope.APPLICATION)
    root.bind(InvocationService, lambda _: InvocationService(), scope=Scope.INVOCATION)

    with pytest.raises(ScopeUnavailable):
        root.resolve(ApplicationService)
    with pytest.raises(ScopeUnavailable):
        root.resolve(InvocationService)
    with pytest.raises(ScopeUnavailable):
        root.invocation_scope()
    with pytest.raises(ScopeUnavailable):
        root.application_scope().resolve(InvocationService)

    hierarchy_root = Container()
    application = hierarchy_root.application_scope()
    invocation = application.invocation_scope()
    with pytest.raises(ScopeUnavailable):
        application.application_scope()
    with pytest.raises(ScopeUnavailable):
        invocation.invocation_scope()


def test_invocation_context_must_be_typed_and_present_for_context_access() -> None:
    application = Container().application_scope()
    with pytest.raises(InvalidBinding, match="ExecutionContext"):
        application.invocation_scope(context=object())  # type: ignore[arg-type]

    invocation = application.invocation_scope()
    with invocation, pytest.raises(ScopeUnavailable, match="no execution context"):
        _ = invocation.context


def test_contextless_invocation_rejects_resolution_until_entered() -> None:
    class Service:
        pass

    root = Container()
    root.bind(Service, lambda _: Service(), scope=Scope.INVOCATION)
    invocation = root.application_scope().invocation_scope()

    with pytest.raises(ScopeUnavailable, match="active invocation"):
        invocation.resolve(Service)
    with invocation:
        assert isinstance(invocation.resolve(Service), Service)


def test_invocation_scope_owns_ambient_execution_context_lifetime() -> None:
    execution = ExecutionContext(tenant_id="tenant-one")
    invocation = Container().application_scope().invocation_scope(context=execution)

    with invocation:
        assert invocation.context is execution
        assert current_context() is execution

    with pytest.raises(ScopeDisposed):
        _ = invocation.context
    with pytest.raises(PlatformError):
        current_context()


def test_external_disposal_rejects_an_entered_invocation_owner() -> None:
    async def scenario() -> None:
        execution = ExecutionContext(tenant_id="revoked-tenant")
        invocation = Container().application_scope().invocation_scope(context=execution)
        entered = asyncio.Event()
        release_owner = asyncio.Event()

        async def owner() -> None:
            async with invocation:
                assert current_context() is execution
                entered.set()
                await release_owner.wait()
                assert current_context() is execution

        owner_task = asyncio.create_task(owner())
        await entered.wait()
        with pytest.raises(InvalidBinding, match="external shutdown"):
            await invocation.aclose()
        release_owner.set()
        await owner_task

    asyncio.run(scenario())


def test_external_disposal_cannot_close_resource_beneath_active_owner() -> None:
    class Resource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        root = Container()
        root.bind(Resource, lambda _: Resource(), scope=Scope.INVOCATION)
        invocation = root.application_scope().invocation_scope()
        entered = asyncio.Event()
        release_owner = asyncio.Event()
        resource: Resource | None = None

        async def owner() -> None:
            nonlocal resource
            async with invocation:
                resource = invocation.resolve(Resource)
                entered.set()
                await release_owner.wait()
                assert not resource.closed

        owner_task = asyncio.create_task(owner())
        await entered.wait()
        assert resource is not None
        with pytest.raises(InvalidBinding, match="external shutdown"):
            await invocation.aclose()
        assert not resource.closed
        release_owner.set()
        await owner_task
        assert resource.closed

    asyncio.run(scenario())


def test_cleanup_continues_after_failure_and_reports_protected_evidence() -> None:
    closed: list[str] = []

    class GoodResource:
        def close(self) -> None:
            closed.append("good")

    class BadResource:
        def close(self) -> None:
            closed.append("bad")
            raise RuntimeError("private cleanup evidence")

    root = Container()
    root.bind(GoodResource, lambda _: GoodResource())
    root.bind(BadResource, lambda _: BadResource())
    root.resolve(GoodResource)
    root.resolve(BadResource)

    with pytest.raises(ScopeDisposalError) as captured:
        root.close()

    assert closed == ["bad", "good"]
    assert len(captured.value.failures) == 1
    assert "private cleanup evidence" not in str(captured.value)


def test_sync_cleanup_finalizes_after_disposer_base_exception() -> None:
    class FatalCleanup(BaseException):
        pass

    closed: list[str] = []

    class GoodResource:
        def close(self) -> None:
            closed.append("good")

    class FatalResource:
        def close(self) -> None:
            closed.append("fatal")
            raise FatalCleanup("protected fatal evidence")

    root = Container()
    root.bind(GoodResource, lambda _: GoodResource())
    root.bind(FatalResource, lambda _: FatalResource())
    root.resolve(GoodResource)
    root.resolve(FatalResource)

    with pytest.raises(ScopeDisposalError) as captured:
        root.close()

    assert closed == ["fatal", "good"]
    assert isinstance(captured.value.failures[0], FatalCleanup)
    with pytest.raises(ScopeDisposalError):
        root.close()


def test_async_cleanup_finalizes_after_disposer_base_exception() -> None:
    class FatalCleanup(BaseException):
        pass

    async def scenario() -> None:
        closed: list[str] = []

        class GoodResource:
            async def aclose(self) -> None:
                closed.append("good")

        class FatalResource:
            async def aclose(self) -> None:
                closed.append("fatal")
                raise FatalCleanup("protected fatal evidence")

        root = Container()
        root.bind(GoodResource, lambda _: GoodResource())
        root.bind(FatalResource, lambda _: FatalResource())
        root.resolve(GoodResource)
        root.resolve(FatalResource)

        await root._aclose_impl()  # type: ignore[attr-defined]

        assert closed == ["fatal", "good"]
        with pytest.raises(ScopeDisposalError) as captured:
            await root.aclose()
        assert isinstance(captured.value.failures[0], FatalCleanup)

    asyncio.run(scenario())


def test_disposer_cancellation_is_reraised_after_complete_unwinding() -> None:
    async def scenario() -> None:
        closed: list[str] = []

        class GoodResource:
            async def aclose(self) -> None:
                closed.append("good")

        class CancellingResource:
            async def aclose(self) -> None:
                closed.append("cancelling")
                raise asyncio.CancelledError()

        root = Container()
        root.bind(GoodResource, lambda _: GoodResource())
        root.bind(CancellingResource, lambda _: CancellingResource())
        root.resolve(GoodResource)
        root.resolve(CancellingResource)

        with pytest.raises(asyncio.CancelledError):
            await root.aclose()
        assert closed == ["cancelling", "good"]

    asyncio.run(scenario())


def test_external_instances_are_unowned_unless_ownership_is_explicit() -> None:
    closed: list[str] = []

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)

    external = Resource("external")
    owned = Resource("owned")
    root = Container()
    root.instance("external", external)
    root.instance("owned", owned, owned=True)
    root.resolve_token("external")
    root.resolve_token("owned")

    root.close()
    assert closed == ["owned"]


def test_parent_cleanup_aggregates_child_failures_and_still_closes_parent_resources() -> None:
    closed: list[str] = []

    class ParentResource:
        def close(self) -> None:
            closed.append("parent")

    class ChildResource:
        def close(self) -> None:
            closed.append("child")
            raise RuntimeError("private child failure")

    root = Container()
    root.bind(ParentResource, lambda _: ParentResource(), scope=Scope.SINGLETON)
    root.bind(ChildResource, lambda _: ChildResource(), scope=Scope.INVOCATION)
    invocation = root.application_scope().invocation_scope()
    root.resolve(ParentResource)
    invocation.__enter__()
    invocation.resolve(ChildResource)

    with pytest.raises(ScopeDisposalError) as captured:
        root.close()
    with pytest.raises(ScopeDisposalError):
        invocation.__exit__(None, None, None)

    assert closed == ["child", "parent"]
    assert len(captured.value.failures) == 1


def test_async_context_manager_restores_invocation_context_and_is_idempotent() -> None:
    execution = ExecutionContext(tenant_id="async-tenant")
    invocation = Container().application_scope().invocation_scope(context=execution)

    async def scenario() -> None:
        async with invocation:
            assert current_context() is execution
        await invocation.aclose()

    asyncio.run(scenario())
    with pytest.raises(PlatformError):
        current_context()


def test_broader_lifetime_factories_cannot_capture_invocation_identity_or_create_scopes() -> None:
    class CapturedTenant:
        def __init__(self, tenant_id: str | None) -> None:
            self.tenant_id = tenant_id

    root = Container()

    def create_singleton(_: Container) -> CapturedTenant:
        try:
            tenant_id = current_context().tenant_id
        except PlatformError:
            tenant_id = None
        return CapturedTenant(tenant_id)

    root.bind(CapturedTenant, create_singleton, scope=Scope.SINGLETON)
    root.bind("scope-escalation", lambda current: current.application_scope())
    invocation = root.application_scope().invocation_scope(
        context=ExecutionContext(tenant_id="private-tenant")
    )

    with invocation:
        assert invocation.resolve(CapturedTenant).tenant_id is None
        with pytest.raises(InvalidBinding, match="factory"):
            invocation.resolve_token("scope-escalation")


def test_broader_lifetime_disposer_discovery_cannot_capture_invocation_context() -> None:
    observed: list[ExecutionContext | None] = []

    class Resource:
        @property
        def close(self) -> object:
            observed.append(maybe_current_context())
            return lambda: None

    root = Container()
    root.bind(Resource, lambda _: Resource(), scope=Scope.SINGLETON)
    invocation = root.application_scope().invocation_scope(
        context=ExecutionContext(tenant_id="private")
    )

    with invocation:
        invocation.resolve(Resource)

    assert observed == [None]


def test_owned_instance_disposer_discovery_masks_ambient_context() -> None:
    observed: list[ExecutionContext | None] = []

    class Resource:
        @property
        def close(self) -> object:
            observed.append(maybe_current_context())
            return lambda: None

    root = Container()
    with use_context(ExecutionContext(tenant_id="private")):
        root.instance(Resource, Resource(), owned=True)

    assert observed == [None]
    root.close()


def test_resource_disposal_masks_unrelated_ambient_context() -> None:
    observed: list[ExecutionContext | None] = []

    class Resource:
        def close(self) -> None:
            observed.append(maybe_current_context())

    root = Container()
    root.bind(Resource, lambda _: Resource())
    root.resolve(Resource)

    with use_context(ExecutionContext(tenant_id="unrelated")):
        root.close()

    assert observed == [None]


def test_async_resource_disposal_masks_unrelated_ambient_context() -> None:
    observed: list[ExecutionContext | None] = []

    class Resource:
        async def aclose(self) -> None:
            await asyncio.sleep(0)
            observed.append(maybe_current_context())

    async def scenario() -> None:
        root = Container()
        root.bind(Resource, lambda _: Resource())
        root.resolve(Resource)

        with use_context(ExecutionContext(tenant_id="unrelated")):
            await root.aclose()

    asyncio.run(scenario())
    assert observed == [None]


def test_async_cleanup_task_starts_without_caller_context() -> None:
    observed: list[ExecutionContext | None] = []

    class InspectingContainer(Container):
        async def _aclose_impl(self) -> None:
            observed.append(maybe_current_context())
            await super()._aclose_impl()

    async def scenario() -> None:
        root = InspectingContainer()
        with use_context(ExecutionContext(tenant_id="unrelated")):
            await root.aclose()

    asyncio.run(scenario())
    assert observed == [None]


def test_async_cleanup_does_not_block_loop_with_unaffined_sync_disposer() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        disposed = asyncio.Event()

        class Resource:
            def close(self) -> None:
                async def mark_disposed() -> None:
                    disposed.set()

                asyncio.run_coroutine_threadsafe(mark_disposed(), loop).result(timeout=0.5)

        root = Container()
        root.bind(Resource, lambda _: Resource())
        root.resolve(Resource)

        await root.aclose()
        assert disposed.is_set()

    asyncio.run(scenario())


def test_ambient_context_is_bound_to_the_requesting_invocation_only() -> None:
    class CapturedTenant:
        def __init__(self, tenant_id: str | None) -> None:
            self.tenant_id = tenant_id

    def factory(_: Container) -> CapturedTenant:
        try:
            tenant_id = current_context().tenant_id
        except PlatformError:
            tenant_id = None
        return CapturedTenant(tenant_id)

    root = Container()
    root.bind(CapturedTenant, factory, scope=Scope.TRANSIENT)
    first_application = root.application_scope()
    second_application = root.application_scope()
    first = first_application.invocation_scope(context=ExecutionContext(tenant_id="tenant-one"))
    second = second_application.invocation_scope(context=ExecutionContext(tenant_id="tenant-two"))

    with first:
        assert first.resolve(CapturedTenant).tenant_id == "tenant-one"
        assert root.resolve(CapturedTenant).tenant_id is None
        assert first_application.resolve(CapturedTenant).tenant_id is None
        with pytest.raises(ScopeUnavailable, match="entered"):
            second.resolve(CapturedTenant)

    second.close()


def test_cached_async_invocation_factory_receives_invocation_authority() -> None:
    class Service:
        def __init__(self, tenant_id: str | None) -> None:
            self.tenant_id = tenant_id

    async def factory(_: Container) -> Service:
        context = maybe_current_context()
        return Service(None if context is None else context.tenant_id)

    async def scenario() -> None:
        root = Container()
        root.bind_async(Service, factory, scope=Scope.INVOCATION)
        invocation = root.application_scope().invocation_scope(
            context=ExecutionContext(tenant_id="owner")
        )

        async with invocation:
            assert (await invocation.resolve_async(Service)).tenant_id == "owner"

    asyncio.run(scenario())


def test_disposer_contract_is_snapshotted_when_resource_ownership_is_acquired() -> None:
    closed: list[str] = []

    class Resource:
        def __init__(self) -> None:
            self.lookups = 0

        @property
        def close(self) -> object:
            self.lookups += 1
            if self.lookups > 1:
                raise RuntimeError("private property evidence")

            def dispose() -> None:
                closed.append("resource")

            return dispose

    root = Container()
    root.bind(Resource, lambda _: Resource())
    resource = root.resolve(Resource)

    root.close()
    assert resource.lookups == 1
    assert closed == ["resource"]


def test_hostile_disposer_descriptor_is_sanitized_and_releases_construction() -> None:
    class FatalDescriptor(BaseException):
        pass

    class Resource:
        @property
        def close(self) -> object:
            raise FatalDescriptor("private descriptor evidence")

    root = Container()
    root.bind(Resource, lambda _: Resource())

    with pytest.raises(InvalidBinding, match="disposal contract") as captured:
        root.resolve(Resource)

    assert "private descriptor evidence" not in str(captured.value)
    assert captured.value.__context__ is None
    root.close()


def test_async_close_method_must_be_declared_as_aclose() -> None:
    class Resource:
        async def close(self) -> None:
            pass

    root = Container()
    root.bind(Resource, lambda _: Resource())

    with pytest.raises(InvalidBinding, match="aclose"):
        root.resolve(Resource)
    root.close()


def test_sync_disposer_returning_awaitable_fails_without_coroutine_leak() -> None:
    class Resource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> object:
            async def cleanup() -> None:
                self.closed = True

            return cleanup()

    root = Container()
    resource = Resource()
    root.instance(Resource, resource, owned=True)

    with pytest.raises(ScopeDisposalError) as captured:
        root.close()

    assert isinstance(captured.value.failures[0], InvalidBinding)
    assert not resource.closed


def test_async_disposer_must_return_an_awaitable() -> None:
    class Resource:
        def aclose(self) -> None:
            return None

    root = Container()
    root.instance(Resource, Resource(), owned=True)

    with pytest.raises(ScopeDisposalError) as captured:
        asyncio.run(root.aclose())

    assert isinstance(captured.value.failures[0], InvalidBinding)


def test_async_cleanup_finishes_after_caller_cancellation_then_reraises_it() -> None:
    closed: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    class FirstResource:
        async def aclose(self) -> None:
            closed.append("first")

    class BlockingResource:
        async def aclose(self) -> None:
            entered.set()
            await release.wait()
            closed.append("blocking")

    async def scenario() -> None:
        root = Container()
        root.bind(FirstResource, lambda _: FirstResource())
        root.bind(BlockingResource, lambda _: BlockingResource())
        root.resolve(FirstResource)
        root.resolve(BlockingResource)
        closing = asyncio.create_task(root.aclose())
        await entered.wait()
        closing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await closing
        assert cancelled.value.__context__ is None
        assert closed == ["blocking", "first"]
        await root.aclose()

    asyncio.run(scenario())


def test_parent_retains_child_failures_when_child_cleanup_propagates_cancellation() -> None:
    class FailedResource:
        async def aclose(self) -> None:
            raise RuntimeError("protected child failure")

    class CancelledResource:
        async def aclose(self) -> None:
            raise asyncio.CancelledError

    async def scenario() -> None:
        root = Container()
        root.bind(FailedResource, lambda _: FailedResource(), scope=Scope.INVOCATION)
        root.bind(CancelledResource, lambda _: CancelledResource(), scope=Scope.INVOCATION)
        invocation = root.application_scope().invocation_scope()
        await invocation.__aenter__()
        invocation.resolve(FailedResource)
        invocation.resolve(CancelledResource)

        with pytest.raises(asyncio.CancelledError):
            await root.aclose()
        with pytest.raises(asyncio.CancelledError):
            await invocation.__aexit__(None, None, None)

        assert any(
            isinstance(failure, RuntimeError)
            for failure in root._disposal_failures  # type: ignore[attr-defined]
        )

    asyncio.run(scenario())


def test_sync_cleanup_unwinds_then_propagates_cancellation() -> None:
    closed: list[str] = []

    class GoodResource:
        def close(self) -> None:
            closed.append("good")

    class CancelledResource:
        def close(self) -> None:
            closed.append("cancelled")
            raise asyncio.CancelledError

    root = Container()
    root.bind(GoodResource, lambda _: GoodResource())
    root.bind(CancelledResource, lambda _: CancelledResource())
    root.resolve(GoodResource)
    root.resolve(CancelledResource)

    with pytest.raises(asyncio.CancelledError):
        root.close()

    assert closed == ["cancelled", "good"]


def test_disposer_timeout_error_is_not_misclassified_as_shutdown_budget_expiry() -> None:
    class Resource:
        def close(self) -> None:
            raise TimeoutError("protected disposer failure")

    root = Container()
    root.bind(Resource, lambda _: Resource())
    root.resolve(Resource)

    with pytest.raises(ScopeDisposalError) as captured:
        root.close()

    assert isinstance(captured.value.failures[0], TimeoutError)
    assert not isinstance(captured.value.failures[0], ShutdownTimeout)


def test_caller_cancellation_precedes_cleanup_failure_without_losing_evidence() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class FailingResource:
        async def aclose(self) -> None:
            entered.set()
            await release.wait()
            raise RuntimeError("protected cleanup evidence")

    async def scenario() -> None:
        root = Container()
        root.bind(FailingResource, lambda _: FailingResource())
        root.resolve(FailingResource)
        closing = asyncio.create_task(root.aclose())
        await entered.wait()
        closing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await closing
        assert cancelled.value.__context__ is None
        with pytest.raises(ScopeDisposalError) as captured:
            await root.aclose()
        assert isinstance(captured.value.failures[0], RuntimeError)

    asyncio.run(scenario())


def test_async_context_body_cancellation_precedes_cleanup_failure() -> None:
    class FailingResource:
        async def aclose(self) -> None:
            raise RuntimeError("protected cleanup evidence")

    async def scenario() -> None:
        root = Container()
        root.bind(FailingResource, lambda _: FailingResource(), scope=Scope.INVOCATION)
        invocation = root.application_scope().invocation_scope()

        with pytest.raises(asyncio.CancelledError) as captured:
            async with invocation:
                invocation.resolve(FailingResource)
                raise asyncio.CancelledError("body cancellation")

        assert captured.value.__context__ is None
        assert isinstance(invocation._disposal_failures[0], RuntimeError)  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_async_context_cleanup_failure_suppresses_private_body_exception() -> None:
    class FailingResource:
        async def aclose(self) -> None:
            raise RuntimeError("CLEANUP_SECRET")

    async def scenario() -> None:
        root = Container()
        root.bind(FailingResource, lambda _: FailingResource(), scope=Scope.INVOCATION)
        invocation = root.application_scope().invocation_scope()

        with pytest.raises(ScopeDisposalError) as captured:
            async with invocation:
                invocation.resolve(FailingResource)
                raise RuntimeError("BODY_SECRET")

        rendered = "".join(traceback.format_exception(captured.value))
        assert "BODY_SECRET" not in rendered
        assert "CLEANUP_SECRET" not in rendered
        assert captured.value.__suppress_context__

    asyncio.run(scenario())


def test_disposed_children_detach_and_do_not_force_parent_async_cleanup() -> None:
    class AsyncResource:
        async def aclose(self) -> None:
            await asyncio.sleep(0)

    root = Container()
    root.bind(AsyncResource, lambda _: AsyncResource(), scope=Scope.INVOCATION)
    application = root.application_scope()
    invocation = application.invocation_scope()

    async def use_invocation() -> None:
        async with invocation:
            invocation.resolve(AsyncResource)

    asyncio.run(use_invocation())
    assert invocation not in application._children  # type: ignore[attr-defined]
    application.close()


def test_disposed_child_handle_releases_parent_and_registry_graph() -> None:
    root = Container()
    root.bind("service", lambda _: object())
    application = root.application_scope()
    invocation = application.invocation_scope()
    root_reference = weakref.ref(root)
    application_reference = weakref.ref(application)

    invocation.close()
    del application
    del root
    gc.collect()

    assert root_reference() is None
    assert application_reference() is None


def test_failed_disposal_does_not_retain_resource_through_cached_tracebacks() -> None:
    class Resource:
        def close(self) -> None:
            raise RuntimeError("protected cleanup evidence")

    root = Container()
    resource = Resource()
    resource_reference = weakref.ref(resource)
    root.instance(Resource, resource, owned=True)

    with pytest.raises(ScopeDisposalError) as captured:
        root.close()
    del captured
    del resource
    gc.collect()

    assert resource_reference() is None


def test_failure_sanitization_does_not_reinvoke_untrusted_exception_constructor() -> None:
    constructions = 0

    class UntrustedFailure(BaseException):
        def __init__(self) -> None:
            nonlocal constructions
            constructions += 1
            super().__init__("private")

    class Resource:
        def close(self) -> None:
            raise UntrustedFailure

    root = Container()
    root.bind(Resource, lambda _: Resource())
    root.resolve(Resource)

    with pytest.raises(ScopeDisposalError):
        root.close()

    assert constructions == 1


def test_failure_sanitization_does_not_invoke_exception_metaclass_attributes() -> None:
    class SlowMeta(type):
        def __getattribute__(cls, name: str) -> object:
            if name == "__name__":
                sleep(0.2)
            return super().__getattribute__(name)

    class UntrustedFailure(BaseException, metaclass=SlowMeta):
        pass

    class Resource:
        def close(self) -> None:
            raise UntrustedFailure

    root = Container(shutdown_timeout=0.05)
    root.bind(Resource, lambda _: Resource())
    root.resolve(Resource)
    started = monotonic()

    with pytest.raises(ScopeDisposalError):
        root.close()

    assert monotonic() - started < 0.15


def test_failure_sanitization_does_not_hash_untrusted_exception_class() -> None:
    hashes = 0

    class HostileMeta(type):
        def __hash__(cls) -> int:
            nonlocal hashes
            hashes += 1
            raise RuntimeError("untrusted metaclass hash")

    class UntrustedFailure(BaseException, metaclass=HostileMeta):
        pass

    class Resource:
        def close(self) -> None:
            raise UntrustedFailure

    root = Container()
    root.bind(Resource, lambda _: Resource())
    root.resolve(Resource)

    with pytest.raises(ScopeDisposalError):
        root.close()

    assert hashes == 0


def test_invocation_scope_cancels_and_joins_owned_tasks_before_cleanup() -> None:
    cancelled = asyncio.Event()

    async def worker() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def scenario() -> None:
        invocation = Container().application_scope().invocation_scope()
        async with invocation:
            task = invocation.create_task(worker())
            await asyncio.sleep(0)
        assert task.cancelled()
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_async_cleanup_bounds_task_that_suppresses_cancellation() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        invocation = Container(shutdown_timeout=0.05).application_scope().invocation_scope()
        invocation.__enter__()

        async def stubborn() -> None:
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        task = invocation.create_task(stubborn())
        await asyncio.sleep(0)
        started = asyncio.get_running_loop().time()
        with pytest.raises(ScopeDisposalError) as captured:
            await invocation.aclose()
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 0.5
        assert any(isinstance(error, ShutdownTimeout) for error in captured.value.failures)
        release.set()
        await task
        with pytest.raises(ScopeDisposalError):
            invocation.__exit__(None, None, None)

    asyncio.run(scenario())


def test_timed_out_owned_task_late_failure_is_consumed() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        release = asyncio.Event()
        finished = asyncio.Event()
        reports: list[dict[str, object]] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: reports.append(context))
        invocation = Container(shutdown_timeout=0.05).application_scope().invocation_scope()
        invocation.__enter__()

        async def stubborn() -> None:
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                finished.set()
            raise RuntimeError("late private failure")

        task = invocation.create_task(stubborn())
        await asyncio.sleep(0)
        try:
            with pytest.raises(ScopeDisposalError):
                await invocation.aclose()
            release.set()
            await finished.wait()
            del task
            gc.collect()
            await asyncio.sleep(0)
            assert not reports
        finally:
            release.set()
            loop.set_exception_handler(previous_handler)
            with pytest.raises(ScopeDisposalError):
                invocation.__exit__(None, None, None)

    asyncio.run(scenario())


def test_async_cleanup_bounds_disposer_that_suppresses_cancellation() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        finished = asyncio.Event()

        class Resource:
            async def aclose(self) -> None:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                finally:
                    finished.set()

        root = Container(shutdown_timeout=0.05)
        root.bind(Resource, lambda _: Resource())
        root.resolve(Resource)
        closing = asyncio.create_task(root.aclose())
        await asyncio.sleep(0.1)
        try:
            assert closing.done()
        finally:
            release.set()
        with pytest.raises(ScopeDisposalError) as captured:
            await closing
        assert any(isinstance(error, ShutdownTimeout) for error in captured.value.failures)
        await finished.wait()

    asyncio.run(scenario())


def test_async_cleanup_bounds_blocked_synchronous_disposer() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        release = asyncio.Event()
        started = asyncio.Event()
        finished = asyncio.Event()

        class Resource:
            def close(self) -> None:
                loop.call_soon_threadsafe(started.set)
                asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
                loop.call_soon_threadsafe(finished.set)

        root = Container(shutdown_timeout=0.05)
        root.bind(Resource, lambda _: Resource())
        root.resolve(Resource)
        closing = asyncio.create_task(root.aclose())
        await started.wait()
        await asyncio.sleep(0.1)
        try:
            assert closing.done()
        finally:
            release.set()
        with pytest.raises(ScopeDisposalError) as captured:
            await closing
        assert any(isinstance(error, ShutdownTimeout) for error in captured.value.failures)
        await finished.wait()

    asyncio.run(scenario())


def test_async_cleanup_bounds_sync_disposer_from_async_resolution() -> None:
    class Resource:
        def close(self) -> None:
            sleep(0.2)

    async def scenario() -> None:
        root = Container(shutdown_timeout=0.05)
        root.bind(Resource, lambda _: Resource())
        await root.resolve_async(Resource)
        started = asyncio.get_running_loop().time()

        with pytest.raises(ScopeDisposalError) as captured:
            await root.aclose()

        assert asyncio.get_running_loop().time() - started < 0.15
        assert any(isinstance(error, ShutdownTimeout) for error in captured.value.failures)

    asyncio.run(scenario())


def test_contextless_invocation_owned_task_does_not_inherit_foreign_context() -> None:
    async def scenario() -> None:
        invocation = Container().application_scope().invocation_scope()

        async def worker() -> ExecutionContext | None:
            return maybe_current_context()

        with use_context(ExecutionContext(tenant_id="foreign")):
            async with invocation:
                assert await invocation.create_task(worker()) is None

    asyncio.run(scenario())


def test_sync_close_cannot_discard_a_completed_owned_task_failure() -> None:
    invocation = Container().application_scope().invocation_scope()

    async def prepare() -> None:
        invocation.__enter__()

        async def fail() -> None:
            raise RuntimeError("protected task evidence")

        task = invocation.create_task(fail())
        with pytest.raises(RuntimeError):
            await task

    asyncio.run(prepare())

    with pytest.raises(AsyncDisposalRequired):
        invocation.close()
    with pytest.raises(ScopeDisposalError) as captured:
        asyncio.run(invocation.aclose())
    assert isinstance(captured.value.failures[0], RuntimeError)
    with pytest.raises(ScopeDisposalError):
        invocation.__exit__(None, None, None)


def test_async_close_cancels_owned_task_before_waiting_for_its_factory() -> None:
    class Service:
        pass

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        cancelled = asyncio.Event()

        async def factory(_: Container) -> Service:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return Service()

        root = Container()
        root.bind_async(Service, factory, scope=Scope.TRANSIENT)
        invocation = root.application_scope().invocation_scope()
        invocation.__enter__()
        resolving = invocation.create_task(invocation.resolve_async(Service))
        await entered.wait()
        try:
            await invocation.aclose()
        finally:
            release.set()
            invocation.__exit__(None, None, None)
        assert cancelled.is_set()
        assert resolving.cancelled()

    asyncio.run(scenario())


def test_raw_task_cannot_start_unowned_transient_factory() -> None:
    class Service:
        pass

    async def scenario() -> None:
        entered = asyncio.Event()

        async def factory(_: Container) -> Service:
            entered.set()
            return Service()

        root = Container()
        root.bind_async(Service, factory, scope=Scope.TRANSIENT)
        invocation = root.application_scope().invocation_scope()
        async with invocation:
            resolving = asyncio.create_task(invocation.resolve_async(Service))
            with pytest.raises(ScopeUnavailable, match="active invocation"):
                await resolving
        assert not entered.is_set()

    asyncio.run(scenario())
