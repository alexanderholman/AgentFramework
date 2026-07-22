from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from af.framework.context import (
    AcceptanceScope,
    CancellationToken,
    ExecutionContext,
    Principal,
    TraceContext,
    current_context,
    derive_context,
    maybe_current_context,
    use_context,
)
from af.framework.errors import PlatformError, PlatformReason


def context(name: str) -> ExecutionContext:
    return ExecutionContext(
        principal=Principal(subject_id=name, principal_type="user", roles=("operator",)),
        tenant_id=f"tenant-{name}",
        correlation_id=f"correlation-{name}",
        causation_id=f"causation-{name}",
        deadline=datetime(2026, 7, 23, tzinfo=UTC),
        trace=TraceContext(trace_id=f"trace-{name}", span_id=f"span-{name}"),
        locale="en-GB",
        requested_scope=AcceptanceScope.REVIEWED_GENERATIVE,
        effective_scope=AcceptanceScope.DETERMINISTIC,
        cancellation=CancellationToken(),
    )


def test_context_values_are_frozen_and_validate_deadlines() -> None:
    value = context("one")
    with pytest.raises(FrozenInstanceError):
        value.tenant_id = "other"  # type: ignore[misc]
    with pytest.raises(ValueError, match="timezone"):
        ExecutionContext(deadline=datetime(2026, 7, 23))
    with pytest.raises(ValueError, match="trace flags"):
        TraceContext(trace_id="trace", trace_flags=-1)
    with pytest.raises(ValueError, match="claim"):
        Principal("subject", "user", claims=(("", "value"),))
    with pytest.raises(ValueError, match="tenant id"):
        ExecutionContext(tenant_id="bad\nvalue")

    roles = ["operator"]
    principal = Principal("subject", "user", roles=roles)  # type: ignore[arg-type]
    roles.append("admin")
    assert principal.roles == ("operator",)
    assert "subject" not in repr(principal)
    assert "tenant-one" not in repr(value)


def test_cancellation_token_propagates_state_and_waiters() -> None:
    async def scenario() -> None:
        token = CancellationToken()
        waiter = asyncio.create_task(token.wait())
        token.cancel()
        await waiter
        assert token.cancelled is True

    asyncio.run(scenario())


def test_context_binding_nests_and_restores_exact_parent() -> None:
    parent = context("parent")
    child = derive_context(parent, causation_id="child-cause")

    assert maybe_current_context() is None
    with use_context(parent):
        assert current_context() is parent
        with use_context(child):
            assert current_context() is child
        assert current_context() is parent
    assert maybe_current_context() is None


def test_context_copied_to_unowned_task_expires_after_parent_scope() -> None:
    async def scenario() -> PlatformReason:
        release = asyncio.Event()

        async def escaped() -> PlatformReason:
            await release.wait()
            with pytest.raises(PlatformError) as captured:
                current_context()
            return captured.value.reason

        with use_context(context("parent")):
            task = asyncio.create_task(escaped())
        release.set()
        return await task

    assert asyncio.run(scenario()) is PlatformReason.CONTEXT_UNAVAILABLE


def test_nested_context_in_escaped_task_expires_with_ancestor_scope() -> None:
    async def scenario() -> PlatformReason:
        rebound = asyncio.Event()
        release = asyncio.Event()

        async def escaped() -> PlatformReason:
            with use_context(derive_context(current_context(), locale="fr")):
                rebound.set()
                await release.wait()
                with pytest.raises(PlatformError) as captured:
                    current_context()
                return captured.value.reason

        with use_context(context("parent")):
            task = asyncio.create_task(escaped())
            await rebound.wait()
        release.set()
        return await task

    assert asyncio.run(scenario()) is PlatformReason.CONTEXT_UNAVAILABLE


def test_derive_context_validates_runtime_types() -> None:
    with pytest.raises((TypeError, ValueError)):
        derive_context(context("parent"), locale=123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        derive_context(  # type: ignore[call-arg]
            context("parent"), effective_scope=AcceptanceScope.OPERATOR_EVALUATION
        )
    with pytest.raises(PlatformError) as missing:
        current_context()
    assert missing.value.reason is PlatformReason.CONTEXT_UNAVAILABLE


def test_context_restores_after_exception() -> None:
    with pytest.raises(RuntimeError, match="stop"), use_context(context("failure")):
        raise RuntimeError("stop")
    assert maybe_current_context() is None


def test_context_propagates_to_child_tasks_without_sibling_leakage() -> None:
    async def scenario() -> tuple[ExecutionContext, ExecutionContext, ExecutionContext]:
        parent = context("parent")

        async def child(name: str) -> ExecutionContext:
            derived = derive_context(current_context(), locale=f"locale-{name}")
            with use_context(derived):
                await asyncio.sleep(0)
                return current_context()

        with use_context(parent):
            first, second = await asyncio.gather(child("first"), child("second"))
            return first, second, current_context()

    first, second, parent = asyncio.run(scenario())
    assert first.locale == "locale-first"
    assert second.locale == "locale-second"
    assert parent.tenant_id == "tenant-parent"
    assert maybe_current_context() is None


def test_context_is_restored_after_async_cancellation() -> None:
    async def scenario() -> None:
        async def wait_forever() -> None:
            with use_context(context("cancelled")):
                await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert maybe_current_context() is None

    asyncio.run(scenario())
