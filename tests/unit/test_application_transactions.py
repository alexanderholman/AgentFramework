from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from af.application.bus import (
    ApplicationBus,
    ApplicationResult,
    AuthorizationRequest,
    Command,
    Gate,
    GateMiddleware,
    HandlerRegistry,
    InvalidDispatchContract,
    Query,
)
from af.application.transactions import TransactionMiddleware
from af.framework.context import AcceptanceScope, ExecutionContext
from af.framework.errors import PlatformError, PlatformReason


@dataclass(frozen=True, slots=True)
class _Increment(Command):
    value: int


@dataclass(frozen=True, slots=True)
class _Read(Query):
    key: str


def _result(value: object) -> ApplicationResult[object]:
    return ApplicationResult(value=value)


def test_transaction_commits_after_success() -> None:
    events: list[object] = []
    registry = HandlerRegistry()

    async def handler(message, _context):
        events.append(("handler", message.value))
        return _result(message.value + 1)

    class Transaction:
        async def __aenter__(self):
            events.append("transaction:begin")
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            events.append(("transaction:end", exc_type, exc, traceback))
            return None

    registry.register(_Increment, handler)
    bus = ApplicationBus(
        registry,
        middleware=(TransactionMiddleware(lambda _request: Transaction()),),
    )

    async def scenario() -> None:
        assert await bus.dispatch(_Increment(2), context=ExecutionContext()) == _result(3)

    asyncio.run(scenario())
    assert events[:2] == ["transaction:begin", ("handler", 2)]
    assert events[2] == ("transaction:end", None, None, None)


def test_transaction_rolls_back_on_failure_and_preserves_cancellation() -> None:
    events: list[tuple[type[BaseException] | None, BaseException | None]] = []
    registry = HandlerRegistry()

    async def handler(message, _context):
        if message.value == 1:
            raise RuntimeError("private handler failure")
        raise asyncio.CancelledError

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, _traceback):
            events.append((exc_type, exc))
            return None

    registry.register(_Increment, handler)
    bus = ApplicationBus(
        registry,
        middleware=(TransactionMiddleware(lambda _request: Transaction()),),
    )

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="private handler failure"):
            await bus.dispatch(_Increment(1), context=ExecutionContext())
        with pytest.raises(asyncio.CancelledError):
            await bus.dispatch(_Increment(2), context=ExecutionContext())

    asyncio.run(scenario())
    assert events[0][0] is RuntimeError
    assert isinstance(events[0][1], RuntimeError)
    assert events[1][0] is asyncio.CancelledError
    assert isinstance(events[1][1], asyncio.CancelledError)


def test_transaction_selection_and_invalid_boundaries() -> None:
    calls = 0
    registry = HandlerRegistry()
    registry.register(_Read, lambda message, _context: _result(message.key))

    def no_transaction(_request):
        nonlocal calls
        calls += 1
        return None

    bus = ApplicationBus(
        registry,
        middleware=(TransactionMiddleware(no_transaction),),
    )

    async def scenario() -> None:
        assert await bus.dispatch(_Read("ok"), context=ExecutionContext()) == _result("ok")

        invalid_registry = HandlerRegistry()
        invalid_registry.register(_Read, lambda message, _context: _result(message.key))
        invalid = ApplicationBus(
            invalid_registry,
            middleware=(TransactionMiddleware(lambda _request: object()),),
        )
        with pytest.raises(InvalidDispatchContract, match="async boundary"):
            await invalid.dispatch(_Read("bad"), context=ExecutionContext())

    asyncio.run(scenario())
    assert calls == 1
    with pytest.raises(InvalidDispatchContract, match="resolver"):
        TransactionMiddleware(object())


def test_transaction_resolution_failure_is_sanitized() -> None:
    registry = HandlerRegistry()
    registry.register(_Read, lambda message, _context: _result(message.key))

    def fail_resolution(_request):
        raise RuntimeError("private database name")

    bus = ApplicationBus(
        registry,
        middleware=(TransactionMiddleware(fail_resolution),),
    )

    async def scenario() -> None:
        with pytest.raises(PlatformError) as captured:
            await bus.dispatch(_Read("item"), context=ExecutionContext())
        assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
        assert captured.value.stage == "application.transaction-resolution"
        assert "private database name" not in str(captured.value)

    asyncio.run(scenario())


def test_authorization_can_prevent_transaction_begin() -> None:
    transaction_calls = 0
    registry = HandlerRegistry()
    registry.register(_Read, lambda message, _context: _result(message.key))
    gate = Gate(policy_version="policy-v1", policies=())

    def select_transaction(_request):
        nonlocal transaction_calls
        transaction_calls += 1
        return None

    bus = ApplicationBus(
        registry,
        middleware=(
            GateMiddleware(
                gate,
                authorize=lambda _message, _context: AuthorizationRequest(
                    "read",
                    "task",
                    "protected",
                    AcceptanceScope.DETERMINISTIC,
                ),
            ),
            TransactionMiddleware(select_transaction),
        ),
    )

    async def scenario() -> None:
        with pytest.raises(PlatformError) as captured:
            await bus.dispatch(_Read("item"), context=ExecutionContext())
        assert captured.value.reason is PlatformReason.AUTHORIZATION_DENIED

    asyncio.run(scenario())
    assert transaction_calls == 0
