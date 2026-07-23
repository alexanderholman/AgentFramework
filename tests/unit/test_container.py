from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from af.framework.container import Container, InvocationKind, Scope
from af.framework.errors import InvalidBinding, ScopeUnavailable, ServiceNotBound


class Counter:
    pass


def test_singleton_binding_is_created_once() -> None:
    container = Container()
    container.bind(Counter, lambda _: Counter(), scope=Scope.SINGLETON)

    assert container.resolve(Counter) is container.resolve(Counter)


def test_transient_binding_is_created_each_time() -> None:
    container = Container()
    container.bind(Counter, lambda _: Counter(), scope=Scope.TRANSIENT)

    assert container.resolve(Counter) is not container.resolve(Counter)


def test_singleton_none_value_is_cached_without_reinvoking_factory() -> None:
    calls = 0

    def factory(_: Container) -> None:
        nonlocal calls
        calls += 1
        return None

    container = Container()
    container.bind("nullable", factory, scope=Scope.SINGLETON)

    assert container.resolve_token("nullable") is None
    assert container.resolve_token("nullable") is None
    assert calls == 1


def test_missing_binding_fails_explicitly() -> None:
    with pytest.raises(ServiceNotBound):
        Container().resolve(Counter)


def test_duplicate_binding_requires_explicit_replacement() -> None:
    container = Container()
    container.instance(Counter, Counter())

    with pytest.raises(InvalidBinding):
        container.instance(Counter, Counter())

    replacement = Counter()
    container.instance(Counter, replacement, replace=True)
    assert container.resolve(Counter) is replacement


def test_every_lifetime_has_explicit_hierarchical_identity() -> None:
    class SingletonService:
        pass

    class ApplicationService:
        pass

    class InvocationService:
        pass

    class TransientService:
        pass

    root = Container()
    root.bind(SingletonService, lambda _: SingletonService(), scope=Scope.SINGLETON)
    root.bind(ApplicationService, lambda _: ApplicationService(), scope=Scope.APPLICATION)
    root.bind(InvocationService, lambda _: InvocationService(), scope=Scope.INVOCATION)
    root.bind(TransientService, lambda _: TransientService(), scope=Scope.TRANSIENT)

    first_application = root.application_scope()
    second_application = root.application_scope()
    first_invocation = first_application.invocation_scope()
    second_invocation = first_application.invocation_scope()

    with first_invocation:
        singleton = first_invocation.resolve(SingletonService)
        application = first_invocation.resolve(ApplicationService)
        first_scoped = first_invocation.resolve(InvocationService)
        assert first_scoped is first_invocation.resolve(InvocationService)
        assert first_invocation.resolve(TransientService) is not first_invocation.resolve(
            TransientService
        )
    with second_invocation:
        assert singleton is second_invocation.resolve(SingletonService)
        assert application is second_invocation.resolve(ApplicationService)
        assert first_scoped is not second_invocation.resolve(InvocationService)
    assert singleton is second_application.resolve(SingletonService)
    assert application is not second_application.resolve(ApplicationService)


def test_minimal_container_import_does_not_load_bootstrap_or_infrastructure() -> None:
    script = """
import sys
from importlib.abc import MetaPathFinder

class Blocker(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'af.bootstrap' or fullname.startswith('af.infrastructure'):
            raise ImportError(f'blocked eager import: {fullname}')
        return None

sys.meta_path.insert(0, Blocker())
from af.framework.container import Container
container = Container()
assert 'af.bootstrap' not in sys.modules
assert not any(name.startswith('af.infrastructure') for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_package_root_keeps_application_exports_compatible_after_lazy_import() -> None:
    import af
    from af.bootstrap import AFApplication, create_application

    assert af.AFApplication is AFApplication
    assert af.create_application is create_application
    with pytest.raises(AttributeError):
        _ = af.not_a_public_export


def test_container_module_exposes_only_the_declared_public_contract() -> None:
    namespace: dict[str, object] = {}
    exec("from af.framework.container import *", namespace)

    assert {
        "AsyncFactory",
        "Container",
        "Factory",
        "Qualifier",
        "Resolver",
        "Scope",
        "ServiceKey",
        "ServiceProvider",
    } <= namespace.keys()
    assert "asyncio" not in namespace
    assert "RLock" not in namespace


@pytest.mark.parametrize(
    "value",
    [0, -1, float("inf"), float("nan"), True, "30"],
)
def test_shutdown_timeout_must_be_a_positive_finite_number(value: object) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        Container(shutdown_timeout=value)  # type: ignore[arg-type]


def test_async_api_preserves_all_synchronous_lifetime_semantics() -> None:
    class ApplicationService:
        pass

    class InvocationService:
        pass

    class TransientService:
        pass

    async def scenario() -> None:
        root = Container()
        root.bind(ApplicationService, lambda _: ApplicationService(), scope=Scope.APPLICATION)
        root.bind(InvocationService, lambda _: InvocationService(), scope=Scope.INVOCATION)
        root.bind(TransientService, lambda _: TransientService())
        application = root.application_scope()
        invocation = application.invocation_scope()

        async with invocation:
            first_application = await invocation.resolve_async(ApplicationService)
            assert first_application is await application.resolve_async(ApplicationService)
            first_invocation = await invocation.resolve_async(InvocationService)
            assert first_invocation is await invocation.resolve_async(InvocationService)
            assert await invocation.resolve_async(
                TransientService
            ) is not await invocation.resolve_async(TransientService)
        with pytest.raises(ScopeUnavailable):
            await root.resolve_async(ApplicationService)
        await root.aclose()

    asyncio.run(scenario())


def test_failed_sync_singleton_factory_can_retry_through_async_api() -> None:
    class Service:
        pass

    async def scenario() -> None:
        calls = 0

        def factory(_: Container) -> Service:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("private construction failure")
            return Service()

        root = Container()
        root.bind(Service, factory, scope=Scope.SINGLETON)
        with pytest.raises(RuntimeError):
            await root.resolve_async(Service)
        resolved = await root.resolve_async(Service)
        assert resolved is await root.resolve_async(Service)
        root.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", list(InvocationKind))
def test_invocation_scope_exposes_every_typed_unit_of_work(kind: InvocationKind) -> None:
    invocation = Container().application_scope().invocation_scope(kind=kind)

    assert invocation.invocation_kind is kind
    invocation.close()


def test_invocation_scope_rejects_untyped_unit_of_work() -> None:
    application = Container().application_scope()

    with pytest.raises(InvalidBinding, match="invocation kind"):
        application.invocation_scope(kind="request")  # type: ignore[arg-type]
    with pytest.raises(ScopeUnavailable, match="not an invocation"):
        _ = application.invocation_kind
