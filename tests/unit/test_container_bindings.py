from __future__ import annotations

import asyncio
import gc
import weakref
from queue import Queue
from threading import Thread

import pytest

from af.framework.container import Container, Qualifier, Resolver, Scope, ServiceKey
from af.framework.errors import CircularDependency, InvalidBinding, ScopeDisposed, ServiceNotBound


class Store:
    pass


def test_qualified_bindings_and_aliases_share_only_the_selected_identity() -> None:
    primary = Qualifier("primary")
    replica = Qualifier("replica")
    container = Container()
    container.bind(Store, lambda _: Store(), qualifier=primary, scope=Scope.SINGLETON)
    container.bind(Store, lambda _: Store(), qualifier=replica, scope=Scope.SINGLETON)
    container.alias("default-store", Store, target_qualifier=primary)

    assert container.resolve(Store, qualifier=primary) is container.resolve_token("default-store")
    assert container.resolve(Store, qualifier=primary) is not container.resolve(
        Store, qualifier=replica
    )


def test_contextual_bindings_are_selected_for_the_immediate_consumer() -> None:
    class ReportService:
        def __init__(self, store: Store) -> None:
            self.store = store

    class AuditService:
        def __init__(self, store: Store) -> None:
            self.store = store

    default = Store()
    reporting = Store()
    container = Container()
    container.instance(Store, default)
    container.bind(ReportService, lambda current: ReportService(current.resolve(Store)))
    container.bind(AuditService, lambda current: AuditService(current.resolve(Store)))
    container.bind_contextual(ReportService, Store, lambda _: reporting)

    assert container.resolve(ReportService).store is reporting
    assert container.resolve(AuditService).store is default


def test_contextual_consumer_alias_has_identical_semantics_before_and_after_freeze() -> None:
    class Consumer:
        def __init__(self, store: Store) -> None:
            self.store = store

    default = Store()
    contextual = Store()
    container = Container()
    container.instance(Store, default)
    container.bind(Consumer, lambda current: Consumer(current.resolve(Store)))
    container.alias("consumer", Consumer)
    container.bind_contextual("consumer", Store, lambda _: contextual)

    assert container.resolve(Consumer).store is contextual
    container.freeze()
    assert container.resolve(Consumer).store is contextual


def test_tags_resolve_services_in_declaration_order() -> None:
    first = Store()
    second = Store()
    container = Container()
    container.instance(Store, first, qualifier="first")
    container.instance(Store, second, qualifier="second")
    container.tag(
        "stores",
        container.key(Store, qualifier="first"),
        container.key(Store, qualifier="second"),
    )

    assert container.resolve_tagged("stores") == (first, second)


def test_tags_accumulate_across_multiple_registration_calls() -> None:
    first = Store()
    second = Store()
    container = Container()
    container.instance(Store, first, qualifier="first")
    container.instance(Store, second, qualifier="second")

    container.tag("stores", container.key(Store, qualifier="first"))
    container.tag("stores", container.key(Store, qualifier="second"))

    assert container.resolve_tagged("stores") == (first, second)


def test_test_overrides_fork_declarations_without_sharing_caches() -> None:
    class OtherStore:
        pass

    production = Store()
    fake = Store()
    container = Container()
    container.bind(Store, lambda _: production, scope=Scope.SINGLETON)
    container.bind(OtherStore, lambda _: OtherStore(), scope=Scope.SINGLETON)
    container.alias("store", Store)
    production_other = container.resolve(OtherStore)

    test_container = container.with_test_overrides({Store: fake})

    assert test_container.resolve(Store) is fake
    assert test_container.resolve_token("store") is fake
    assert container.resolve(Store) is production
    assert test_container.resolve(OtherStore) is not production_other


def test_test_override_takes_precedence_over_contextual_production_binding() -> None:
    class Consumer:
        def __init__(self, store: Store) -> None:
            self.store = store

    default = Store()
    production = Store()
    fake = Store()
    container = Container()
    container.instance(Store, default)
    container.bind(Consumer, lambda current: Consumer(current.resolve(Store)))
    container.bind_contextual(Consumer, Store, lambda _: production)

    test_container = container.with_test_overrides({Store: fake})

    assert test_container.resolve(Consumer).store is fake
    assert container.resolve(Consumer).store is production


def test_test_overrides_require_root_and_existing_canonical_target() -> None:
    root = Container()
    root.bind(Store, lambda _: Store())
    application = root.application_scope()

    with pytest.raises(InvalidBinding, match="root"):
        application.with_test_overrides({Store: Store()})
    with pytest.raises(InvalidBinding, match="not bound"):
        root.with_test_overrides({"missing": Store()})


def test_factory_resolver_fails_closed_during_foreign_registry_reentrancy() -> None:
    class Consumer:
        pass

    class ForeignCallback:
        pass

    privileged = Store()
    restricted = Store()
    foreign = Container()
    root = Container()
    root.instance(Store, privileged)

    def create_consumer(current: Resolver) -> Consumer:
        foreign.bind(ForeignCallback, lambda _: current.resolve(Store))
        foreign.resolve(ForeignCallback)
        return Consumer()

    root.bind(Consumer, create_consumer)
    root.bind_contextual(Consumer, Store, lambda _: restricted)

    with pytest.raises(InvalidBinding, match="resolution frame"):
        root.resolve(Consumer)


def test_dependency_cycles_report_the_complete_resolution_chain() -> None:
    class ServiceA:
        pass

    class ServiceB:
        pass

    container = Container()
    container.bind(ServiceA, lambda current: current.resolve(ServiceB))
    container.bind(ServiceB, lambda current: current.resolve(ServiceA))

    with pytest.raises(CircularDependency) as captured:
        container.resolve(ServiceA)

    assert captured.value.chain == (
        ServiceKey(ServiceA),
        ServiceKey(ServiceB),
        ServiceKey(ServiceA),
    )
    assert str(captured.value).endswith("ServiceA -> ServiceB -> ServiceA")


def test_alias_cycles_report_the_complete_chain() -> None:
    container = Container()
    container.alias("one", "two")
    container.alias("two", "one")

    with pytest.raises(CircularDependency) as captured:
        container.resolve_token("one")

    assert captured.value.chain == (
        ServiceKey("one"),
        ServiceKey("two"),
        ServiceKey("one"),
    )


def test_invalid_binding_declarations_fail_closed() -> None:
    container = Container()
    with pytest.raises(InvalidBinding):
        container.bind(Store, object())  # type: ignore[arg-type]
    with pytest.raises(InvalidBinding):
        container.bind(Store, lambda _: Store(), scope="singleton")  # type: ignore[arg-type]
    identity_token: list[object] = []
    container.bind(identity_token, lambda _: Store())
    assert isinstance(container.resolve_token(identity_token), Store)


def test_scope_creation_freezes_declarations_and_children_cannot_mutate_registry() -> None:
    container = Container()
    container.bind(Store, lambda _: Store())
    application = container.application_scope()

    with pytest.raises(InvalidBinding, match="frozen"):
        container.bind("late", lambda _: object())
    with pytest.raises(InvalidBinding, match="root"):
        application.bind("request-global", lambda _: object())


def test_factory_cannot_mutate_registry_through_a_captured_root() -> None:
    class Service:
        pass

    root = Container()

    def factory(_: Container) -> Service:
        root.bind("injected", lambda _: object())
        return Service()

    root.bind(Service, factory)

    with pytest.raises(InvalidBinding, match="factory"):
        root.resolve(Service)
    with pytest.raises(ServiceNotBound):
        root.resolve_token("injected")


def test_factory_thread_cannot_mutate_captured_root_registry() -> None:
    class Service:
        pass

    root = Container()
    outcomes: Queue[BaseException | None] = Queue()

    def factory(_: Container) -> Service:
        def mutate() -> None:
            try:
                root.bind("thread-injected", lambda _: object())
            except BaseException as error:
                outcomes.put(error)
            else:
                outcomes.put(None)

        thread = Thread(target=mutate)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        return Service()

    root.bind(Service, factory)
    assert isinstance(root.resolve(Service), Service)
    assert isinstance(outcomes.get_nowait(), InvalidBinding)
    with pytest.raises(ServiceNotBound):
        root.resolve_token("thread-injected")


def test_expired_factory_resolver_releases_the_container_graph() -> None:
    class Service:
        pass

    escaped: list[object] = []
    root = Container()

    def factory(resolver: object) -> Service:
        escaped.append(resolver)
        return Service()

    root.bind(Service, factory)  # type: ignore[arg-type]
    root.resolve(Service)
    root_reference = weakref.ref(root)
    root.close()
    del root
    gc.collect()

    assert root_reference() is None
    with pytest.raises(ScopeDisposed, match="expired"):
        escaped[0].resolve(Service)  # type: ignore[attr-defined]


def test_expired_construction_flight_releases_root_from_escaped_task_context() -> None:
    class Service:
        pass

    async def scenario() -> bool:
        release = asyncio.Event()
        escaped_tasks: list[asyncio.Task[None]] = []
        root = Container()

        async def factory(_: object) -> Service:
            escaped_tasks.append(asyncio.create_task(release.wait()))
            return Service()

        root.bind_async(Service, factory, scope=Scope.SINGLETON)  # type: ignore[arg-type]
        await root.resolve_async(Service)
        root_reference = weakref.ref(root)
        root.close()
        del root
        gc.collect()
        released = root_reference() is None
        release.set()
        await escaped_tasks[0]
        return released

    assert asyncio.run(scenario())


def test_disposed_root_clears_factory_closures() -> None:
    class Payload:
        pass

    payload = Payload()
    payload_reference = weakref.ref(payload)
    root = Container()

    def factory(_: object, captured: Payload = payload) -> object:
        return captured

    root.bind("payload", factory)  # type: ignore[arg-type]
    root.close()
    del factory
    del payload
    gc.collect()

    assert payload_reference() is None


def test_factory_resolver_contract_cannot_be_constructed_by_callers() -> None:
    with pytest.raises(TypeError):
        Resolver(Container())  # type: ignore[abstract]


def test_key_tag_and_freeze_validation_rejects_malformed_declarations() -> None:
    container = Container()
    with pytest.raises(ValueError, match="qualifier"):
        container.key(Store, qualifier="bad\u2028name")
    with pytest.raises(InvalidBinding, match="repeated"):
        container.resolve_token(ServiceKey(Store), qualifier="duplicate")
    with pytest.raises(InvalidBinding, match="qualifier"):
        container.key(Store, qualifier=object())  # type: ignore[arg-type]
    with pytest.raises(InvalidBinding, match="qualifier"):
        ServiceKey(Store, object())  # type: ignore[arg-type]
    key_representation = repr(ServiceKey("private-token", Qualifier("private-qualifier")))
    assert "private-token" not in key_representation
    assert "private-qualifier" not in key_representation
    with pytest.raises(ServiceNotBound) as missing:
        container.resolve_token("private-token")
    assert "private-token" not in str(missing.value)
    assert "string token" in str(missing.value)
    assert missing.value.__cause__ is None
    assert missing.value.__context__ is None
    with pytest.raises(ServiceNotBound) as tag_miss:
        container.resolve_tagged("missing")
    assert "tag" in str(tag_miss.value)
    assert "missing" not in str(tag_miss.value)
    with pytest.raises(ServiceNotBound) as async_tag_miss:
        asyncio.run(container.resolve_tagged_async("missing"))
    assert "tag" in str(async_tag_miss.value)
    assert "missing" not in str(async_tag_miss.value)
    with pytest.raises(InvalidBinding, match="tag name"):
        container.resolve_tagged([])  # type: ignore[arg-type]
    with pytest.raises(InvalidBinding, match="tag name"):
        asyncio.run(container.resolve_tagged_async(object()))  # type: ignore[arg-type]

    container.alias("missing-alias", Store)
    with pytest.raises(InvalidBinding, match="alias target"):
        container.freeze()


def test_missing_binding_error_does_not_execute_hostile_token_name() -> None:
    class HostileMeta(type):
        @property
        def __name__(cls) -> str:
            raise RuntimeError("private metaclass evidence")

    class SecretService(metaclass=HostileMeta):
        pass

    with pytest.raises(ServiceNotBound) as captured:
        Container().resolve(SecretService)

    assert "private metaclass evidence" not in str(captured.value)


def test_service_not_bound_message_is_informative_and_non_secret_bearing() -> None:
    """Error messages distinguish categories without leaking token contents."""
    container = Container()

    # Tag miss: message says "tag", not the actual tag name
    with pytest.raises(ServiceNotBound) as tag_err:
        container.resolve_tagged("secret-tag-name")
    tag_message = str(tag_err.value)
    assert "tag" in tag_message
    assert "secret-tag-name" not in tag_message

    # Async tag miss: same guarantee
    with pytest.raises(ServiceNotBound) as async_tag_err:
        asyncio.run(container.resolve_tagged_async("another-secret-tag"))
    async_tag_message = str(async_tag_err.value)
    assert "tag" in async_tag_message
    assert "another-secret-tag" not in async_tag_message

    # Generic string token miss: message says "string token", not the value
    with pytest.raises(ServiceNotBound) as str_err:
        container.resolve_token("secret-service-name")
    str_message = str(str_err.value)
    assert "string token" in str_message
    assert "secret-service-name" not in str_message

    # The tag namespace does not reserve prefixes from ordinary string tokens.
    with pytest.raises(ServiceNotBound) as prefixed_str_err:
        container.resolve_token("tag:secret-service-name")
    prefixed_str_message = str(prefixed_str_err.value)
    assert "string token" in prefixed_str_message
    assert "tag:secret-service-name" not in prefixed_str_message

    # Type token miss: still uses the type name (existing behaviour)
    with pytest.raises(ServiceNotBound) as type_err:
        container.resolve(Store)
    assert "Store" in str(type_err.value)


def test_registry_rejects_hostile_qualifier_and_service_key_subclasses() -> None:
    class HostileQualifier(Qualifier):
        def __hash__(self) -> int:
            raise RuntimeError("private qualifier evidence")

    class HostileServiceKey(ServiceKey):
        def __hash__(self) -> int:
            raise RuntimeError("private key evidence")

    container = Container()
    with pytest.raises(InvalidBinding, match="qualifier"):
        container.bind(Store, lambda _: Store(), qualifier=HostileQualifier("hostile"))
    with pytest.raises(InvalidBinding, match="service key"):
        container.resolve_token(HostileServiceKey(Store))


def test_rejected_duplicate_instance_does_not_probe_candidate_disposer() -> None:
    class Candidate:
        def __init__(self) -> None:
            self.lookups = 0

        @property
        def close(self) -> object:
            self.lookups += 1
            return lambda: None

    container = Container()
    container.instance("service", object())
    candidate = Candidate()

    with pytest.raises(InvalidBinding, match="exists"):
        container.instance("service", candidate, owned=True)

    assert candidate.lookups == 0


def test_contextual_resolution_frames_do_not_cross_container_boundaries() -> None:
    class Consumer:
        def __init__(self, store: Store) -> None:
            self.store = store

    default = Store()
    leaked = Store()
    dependency_container = Container()
    dependency_container.instance(Store, default)
    dependency_container.bind(Consumer, lambda _: Consumer(default))
    dependency_container.bind_contextual(Consumer, Store, lambda _: leaked)

    consumer_container = Container()
    consumer_container.bind(
        Consumer,
        lambda _: Consumer(dependency_container.resolve(Store)),
    )

    assert consumer_container.resolve(Consumer).store is default


def test_sync_bind_rejects_coroutine_function_at_registration_time() -> None:
    async def async_factory(_: Container) -> Store:
        return Store()

    container = Container()
    with pytest.raises(InvalidBinding, match="async"):
        container.bind(Store, async_factory)  # type: ignore[arg-type]


def test_async_callable_object_is_a_valid_async_factory() -> None:
    class AsyncStoreFactory:
        async def __call__(self, _: object) -> Store:
            return Store()

    container = Container()
    container.bind_async(Store, AsyncStoreFactory())

    assert isinstance(asyncio.run(container.resolve_async(Store)), Store)


def test_bind_async_accepts_wrapper_returning_coroutine() -> None:
    """A normal callable that wraps and returns a coroutine is a valid AsyncFactory."""

    async def _inner(_: object) -> Store:
        return Store()

    def wrapper(resolver: object) -> object:
        return _inner(resolver)

    container = Container()
    container.bind_async(Store, wrapper)

    assert isinstance(asyncio.run(container.resolve_async(Store)), Store)


def test_bind_async_accepts_callable_returning_asyncio_future() -> None:
    """A normal callable returning an asyncio.Future satisfies the AsyncFactory contract."""

    def future_factory(_: object) -> asyncio.Future[Store]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Store] = loop.create_future()
        future.set_result(Store())
        return future

    async def scenario() -> Store:
        container = Container()
        container.bind_async(Store, future_factory)
        return await container.resolve_async(Store)

    assert isinstance(asyncio.run(scenario()), Store)


def test_bind_async_non_awaitable_return_fails_at_invocation_not_registration() -> None:
    """bind_async accepts any callable; a non-awaitable result fails at invocation."""

    def non_awaitable_factory(_: object) -> Store:
        return Store()

    container = Container()
    # Must not raise at registration time
    container.bind_async(Store, non_awaitable_factory)

    with pytest.raises(InvalidBinding, match="awaitable"):
        asyncio.run(container.resolve_async(Store))


def test_disposed_scopes_reject_empty_tags_and_override_forks_do_not_duplicate_ownership() -> None:
    closed: list[str] = []

    class Resource:
        def close(self) -> None:
            closed.append("closed")

    resource = Resource()
    container = Container()
    container.instance("shared-resource", resource, owned=True)
    container.instance(Resource, Resource())
    container.tag("empty")
    fork = container.with_test_overrides({Resource: Resource()})
    container.resolve_token("shared-resource")
    fork.resolve_token("shared-resource")
    fork.close()
    container.close()
    assert closed == ["closed"]

    with pytest.raises(ScopeDisposed):
        container.resolve_tagged("empty")
    with pytest.raises(ScopeDisposed):
        asyncio.run(container.resolve_tagged_async("empty"))
    with pytest.raises(ScopeDisposed):
        container.with_test_overrides({Resource: Resource()})
