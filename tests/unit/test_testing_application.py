from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from af.application.bus import (
    ApplicationBus,
    ApplicationResult,
    DispatchError,
    InvalidDispatchContract,
    Query,
)
from af.config import Settings
from af.framework.application import ApplicationState
from af.framework.container import ProviderRegistrar, Scope, ServiceProvider
from af.framework.context import AcceptanceScope, ExecutionContext, Principal
from af.testing import TestApplicationBuilder


@dataclass(frozen=True, slots=True)
class _Value:
    name: str


class _ValueProvider(ServiceProvider):
    provider_id = "tests.value"

    def __init__(self, value: _Value) -> None:
        self._value = value

    def register(self, container: ProviderRegistrar) -> None:
        value = self._value
        container.bind(
            _Value,
            lambda _: value,
            scope=Scope.APPLICATION,
            owned=False,
        )


@dataclass(frozen=True, slots=True)
class _Echo(Query):
    value: str


def _settings() -> Settings:
    return Settings(database_url="sqlite+pysqlite:///:memory:")


def _context(name: str = "test") -> ExecutionContext:
    return ExecutionContext(
        principal=Principal(
            subject_id=f"subject-{name}",
            principal_type="user",
            roles=("tester",),
        ),
        tenant_id=f"tenant-{name}",
        correlation_id=f"correlation-{name}",
        requested_scope=AcceptanceScope.DETERMINISTIC,
    )


def test_multiple_test_applications_are_isolated_and_overrides_are_local() -> None:
    first_value = _Value("first")
    first_override = _Value("first-override")
    second_value = _Value("second")
    first = (
        TestApplicationBuilder(settings=_settings())
        .provider(_ValueProvider(first_value))
        .override(_Value, first_override)
        .build()
    )
    second = (
        TestApplicationBuilder(settings=_settings())
        .provider(_ValueProvider(second_value))
        .build()
    )

    try:
        assert first.resolve(_Value) is first_override
        assert second.resolve(_Value) is second_value

        first.shutdown()
        first.assert_clean()
        assert first.state is ApplicationState.SHUTDOWN
        assert second.resolve(_Value) is second_value
    finally:
        second.shutdown()
        second.assert_clean()


def test_test_adapter_and_direct_bus_have_identical_semantics() -> None:
    calls: list[tuple[str, ExecutionContext]] = []

    async def handler(message: _Echo, context: ExecutionContext) -> ApplicationResult[object]:
        calls.append((message.value, context))
        return ApplicationResult(value=message.value.upper())

    application = TestApplicationBuilder(settings=_settings()).handler(_Echo, handler).build()
    context = _context()

    async def scenario() -> None:
        direct = await application.resolve(ApplicationBus).dispatch(
            _Echo("direct"),
            context=context,
        )
        adapted = await application.dispatch(_Echo("adapted"), context=context)
        assert direct == ApplicationResult(value="DIRECT")
        assert adapted == ApplicationResult(value="ADAPTED")

    try:
        asyncio.run(scenario())
        assert calls == [("direct", context), ("adapted", context)]
    finally:
        application.shutdown()
        application.assert_clean()


def test_handler_registries_do_not_leak_between_test_applications() -> None:
    async def handler(message: _Echo, _context: ExecutionContext) -> ApplicationResult[object]:
        return ApplicationResult(value=message.value)

    with_handler = TestApplicationBuilder(settings=_settings()).handler(_Echo, handler).build()
    without_handler = TestApplicationBuilder(settings=_settings()).build()

    async def scenario() -> None:
        assert await with_handler.dispatch(_Echo("yes"), context=_context("one")) == (
            ApplicationResult(value="yes")
        )
        with pytest.raises(DispatchError):
            await without_handler.dispatch(_Echo("no"), context=_context("two"))

    try:
        asyncio.run(scenario())
    finally:
        with_handler.shutdown()
        without_handler.shutdown()
        with_handler.assert_clean()
        without_handler.assert_clean()


def test_context_manager_owns_shutdown_and_duplicate_handlers_fail_early() -> None:
    async def handler(message: _Echo, _context: ExecutionContext) -> ApplicationResult[object]:
        return ApplicationResult(value=message.value)

    builder = TestApplicationBuilder(settings=_settings()).handler(_Echo, handler)
    with pytest.raises(InvalidDispatchContract, match="already registered"):
        builder.handler(_Echo, handler)

    with builder.build() as application:
        assert application.state is ApplicationState.SERVING

    application.assert_clean()
