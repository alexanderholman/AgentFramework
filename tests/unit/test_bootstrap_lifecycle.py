from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

import af.bootstrap as bootstrap
from af.application.ideons import IdeonService
from af.bootstrap import create_application, create_application_async
from af.config import ResourceLeaseBackend, Settings
from af.domain.resources import LeaseState, ResourceBudget, ResourceIdentity, ResourceKind
from af.framework.application import Application, ApplicationState
from af.framework.container import (
    Container,
    ProviderRegistrar,
    ProviderResourceRegistry,
    Scope,
    ServiceProvider,
)
from af.framework.errors import ApplicationBootError, AsyncLifecycleRequired, ScopeUnavailable
from af.framework.pipeline import PipelineAgent
from af.infrastructure.database import Database
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.sqlite import SQLiteDriver
from af.infrastructure.tracing import InMemoryTraceRepository


def test_default_sqlite_is_boot_owned_application_state_and_disposed_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "bootstrap.sqlite3"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    close_calls: list[Database] = []
    original_close = Database.close

    def record_close(database: Database) -> None:
        close_calls.append(database)
        original_close(database)

    monkeypatch.setattr(Database, "close", record_close)

    root = Container()
    root.instance(Settings, settings)
    lifecycle = Application(
        root,
        (bootstrap._PersistenceProvider(settings, None, None, None),),
    )
    lifecycle.register()

    try:
        assert lifecycle.state is ApplicationState.REGISTERED
        assert lifecycle.container is not root
        assert not database_path.exists()

        with pytest.raises(ScopeUnavailable):
            root.resolve(Database)
        assert not database_path.exists()

        lifecycle.boot()

        assert lifecycle.state is ApplicationState.BOOTED
        assert database_path.is_file()
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        assert {"af_tasks", "af_run_traces"} <= tables

        database = lifecycle.container.resolve(Database)
        assert lifecycle.container.resolve(Database) is database

        lifecycle.shutdown()
        lifecycle.shutdown()

        assert lifecycle.state is ApplicationState.SHUTDOWN
        assert close_calls == [database]
    finally:
        if lifecycle.state is ApplicationState.BOOTING:
            root.close()
        else:
            lifecycle.shutdown()


def test_opt_in_sqlite_resource_leases_are_boot_owned_and_survive_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_database = tmp_path / "resource-leases.sqlite3"
    settings = Settings(
        resource_lease_backend=ResourceLeaseBackend.SQLITE,
        resource_lease_sqlite_path=str(lease_database),
    )
    close_calls: list[SQLiteDriver] = []
    original_close = SQLiteDriver.close

    def record_close(driver: SQLiteDriver) -> None:
        close_calls.append(driver)
        original_close(driver)

    monkeypatch.setattr(SQLiteDriver, "close", record_close)
    budget = ResourceBudget(
        identity=ResourceIdentity(
            kind=ResourceKind.CONCURRENCY,
            host_id="host.local",
            instance_id="worker.slots",
        ),
        max_quantity=1,
        max_duration=None,
        max_concurrent=1,
    )
    first = create_application(
        settings=settings,
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    created = first.leases.create_lease(
        "lease-bootstrap-persistent",
        budget,
        1,
        {"queue": "default"},
        "worker-policy",
        "authorization-v1",
    )
    first.shutdown()

    second = create_application(
        settings=settings,
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    try:
        assert second.leases.get_lease(created.id) == created
        assert second.leases.get_lease(created.id).state is LeaseState.RESERVED
    finally:
        second.shutdown()

    assert len(close_calls) == 2


def test_default_ideon_runtime_is_constructed_during_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    original_create = bootstrap.create_reference_ideon_runtime

    def create_runtime():
        events.append("create")
        return original_create()

    monkeypatch.setattr(bootstrap, "create_reference_ideon_runtime", create_runtime)
    lifecycle = Application(Container(), (bootstrap._IdeonProvider(None),))

    lifecycle.register()

    assert events == []
    lifecycle.boot()
    assert events == ["create"]
    assert isinstance(lifecycle.container.resolve(IdeonService), IdeonService)
    lifecycle.shutdown()


def test_application_assembly_failure_rolls_back_started_providers() -> None:
    events: list[str] = []

    class Resource:
        def close(self) -> None:
            events.append("resource")

    class BreakingProvider(ServiceProvider):
        provider_id = "test.breaking"

        def register(self, registrar: ProviderRegistrar) -> None:
            def fail(_: object) -> PipelineAgent:
                raise RuntimeError("private assembly failure")

            registrar.bind(
                PipelineAgent,
                fail,
                scope=Scope.APPLICATION,
                replace=True,
            )

        def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container
            events.append("boot")
            resources.own(Resource())

        def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    with pytest.raises(RuntimeError, match="private assembly failure"):
        create_application(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            providers=(BreakingProvider(),),
        )

    assert events == ["boot", "shutdown", "resource"]


def test_async_application_factory_owns_async_provider_lifecycle() -> None:
    events: list[str] = []

    class Resource:
        async def aclose(self) -> None:
            events.append("resource")

    class AsyncProvider(ServiceProvider):
        provider_id = "test.async"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar
            events.append("register")

        async def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container
            events.append("boot")
            resources.own(Resource())

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    async def scenario() -> None:
        application = await create_application_async(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            providers=(AsyncProvider(),),
        )
        assert application.lifecycle.state is ApplicationState.SERVING
        await application.ashutdown()

    asyncio.run(scenario())

    assert events == ["register", "boot", "shutdown", "resource"]


def test_sync_application_factory_bridges_required_async_failure_cleanup() -> None:
    events: list[str] = []

    class AsyncResource:
        async def aclose(self) -> None:
            events.append("resource")

    class Foundation(ServiceProvider):
        provider_id = "test.async-resource"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container
            resources.own(AsyncResource())
            events.append("boot")

        def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    class Failing(ServiceProvider):
        provider_id = "test.failing"
        dependencies = ("test.async-resource",)

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container, resources
            raise RuntimeError("private startup failure")

    with pytest.raises(ApplicationBootError):
        create_application(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            providers=(Foundation(), Failing()),
        )

    assert events == ["boot", "shutdown", "resource"]


def test_sync_application_handle_bridges_async_shutdown() -> None:
    events: list[str] = []

    class Provider(ServiceProvider):
        provider_id = "test.async-shutdown"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        providers=(Provider(),),
    )

    application.shutdown()

    assert application.lifecycle.state is ApplicationState.SHUTDOWN
    assert events == ["shutdown"]


def test_sync_application_factory_rejects_an_active_event_loop() -> None:
    async def scenario() -> None:
        with pytest.raises(AsyncLifecycleRequired, match="create_application_async"):
            create_application(settings=Settings(database_url="sqlite+pysqlite:///:memory:"))

    asyncio.run(scenario())


def test_async_application_assembly_failure_rolls_back_started_providers() -> None:
    events: list[str] = []

    class Resource:
        async def aclose(self) -> None:
            events.append("resource")

    class BreakingProvider(ServiceProvider):
        provider_id = "test.async-breaking"

        def register(self, registrar: ProviderRegistrar) -> None:
            def fail(_: object) -> PipelineAgent:
                raise RuntimeError("private async assembly failure")

            registrar.bind(
                PipelineAgent,
                fail,
                scope=Scope.APPLICATION,
                replace=True,
            )

        async def boot(
            self,
            container: Container,
            resources: ProviderResourceRegistry,
        ) -> None:
            del container
            events.append("boot")
            resources.own(Resource())

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="private async assembly failure"):
            await create_application_async(
                settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
                providers=(BreakingProvider(),),
            )

    asyncio.run(scenario())
    assert events == ["boot", "shutdown", "resource"]
