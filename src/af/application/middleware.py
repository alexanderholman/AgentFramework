"""Reusable middleware and policy integrations for the application bus."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isawaitable
from typing import Protocol, cast

from af.application.bus import (
    ApplicationBus,
    ApplicationResult,
    AuthorizationRequest,
    CallNext,
    DispatchRequest,
    GateDecision,
    GateEffect,
    InvalidDispatchContract,
    Message,
    PolicyEvidence,
)
from af.framework.context import ExecutionContext
from af.framework.errors import AFError, PlatformError, PlatformReason
from af.framework.managers import Manager

__all__ = (
    "ApplicationTraceEvent",
    "InProcessTestAdapter",
    "ScopedAuthorizationRequest",
    "TenantScopePolicy",
    "TraceSink",
    "TracingMiddleware",
    "TransactionDriver",
    "TransactionMiddleware",
    "ValidationMiddleware",
)

_MAX_TEXT = 200
_MAX_SCOPES = 256


def _bounded_text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_TEXT
        or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", value)
    ):
        raise ValueError(f"{field} must be a bounded non-empty single-line value")
    return value


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


async def _await_if_needed(value: object) -> object:
    if isawaitable(value):
        return await cast(Awaitable[object], value)
    return value


type Validator = Callable[[Message, ExecutionContext], object]


class ValidationMiddleware:
    """Run one transport-neutral validator before authorization or resource access."""

    def __init__(self, validate: Validator) -> None:
        if not callable(validate):
            raise InvalidDispatchContract("application validator must be callable")
        self._validate = validate

    async def __call__(
        self,
        request: DispatchRequest,
        call_next: CallNext,
    ) -> ApplicationResult[object]:
        try:
            await _await_if_needed(self._validate(request.message, request.context))
        except (asyncio.CancelledError, AFError):
            raise
        except Exception as error:
            raise PlatformError(
                PlatformReason.VALIDATION_FAILED,
                stage="application.validation",
                protected_cause=error,
            ) from error
        return await call_next(request)


@dataclass(frozen=True, slots=True, repr=False)
class ScopedAuthorizationRequest(AuthorizationRequest):
    """Authorization request with pre-access tenant and principal-scope requirements."""

    tenant_id: str | None = None
    required_principal_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super(ScopedAuthorizationRequest, self).__post_init__()
        if self.tenant_id is not None:
            _bounded_text(self.tenant_id, "authorization tenant id")
        scopes = tuple(self.required_principal_scopes)
        if len(scopes) > _MAX_SCOPES:
            raise ValueError("authorization request contains too many principal scopes")
        for scope in scopes:
            _bounded_text(scope, "required principal scope")
        if len(set(scopes)) != len(scopes):
            raise ValueError("required principal scopes must be unique")
        object.__setattr__(self, "required_principal_scopes", scopes)

    def __repr__(self) -> str:
        return (
            f"ScopedAuthorizationRequest(action={self.action!r}, "
            f"resource_type={self.resource_type!r}, "
            "protected_resource_id='[REDACTED]', tenant_id='[REDACTED]', "
            "required_principal_scopes='[REDACTED]', "
            f"required_scope={self.required_scope!r})"
        )


class TenantScopePolicy:
    """Deny tenant, principal, or scope mismatches before any handler access."""

    def __init__(self, *, policy_version: str) -> None:
        self._policy_version = _bounded_text(policy_version, "tenant policy version")

    def __call__(
        self,
        request: AuthorizationRequest,
        context: ExecutionContext,
    ) -> GateDecision | None:
        if not isinstance(request, ScopedAuthorizationRequest):
            return None
        principal = context.principal
        if principal is None:
            return self._deny("principal_required", PolicyEvidence("principal", "missing"))
        if request.tenant_id is not None and context.tenant_id != request.tenant_id:
            return self._deny("tenant_denied", PolicyEvidence("tenant", "mismatch"))
        if not set(request.required_principal_scopes).issubset(principal.scopes):
            return self._deny("principal_scope_denied", PolicyEvidence("scope", "missing"))
        return None

    def _deny(self, reason_code: str, evidence: PolicyEvidence) -> GateDecision:
        return GateDecision(
            effect=GateEffect.DENY,
            policy_version=self._policy_version,
            reason_code=reason_code,
            evidence=(evidence,),
        )


class TransactionDriver(Protocol):
    """Manager-resolved driver that opens one application transaction boundary."""

    def transaction(
        self,
        message: Message,
        context: ExecutionContext,
    ) -> AbstractAsyncContextManager[None]: ...


class TransactionMiddleware:
    """Resolve a capability-verified driver before entering its async transaction."""

    def __init__(
        self,
        manager: Manager[TransactionDriver],
        *,
        connection: str | None = None,
        capability: str = "transactions",
    ) -> None:
        if not isinstance(manager, Manager):
            raise InvalidDispatchContract("transaction manager is invalid")
        self._manager = manager
        self._connection = connection
        self._capability = _bounded_text(capability, "transaction capability")

    async def __call__(
        self,
        request: DispatchRequest,
        call_next: CallNext,
    ) -> ApplicationResult[object]:
        driver = self._manager.connection(
            self._connection,
            requires=(self._capability,),
        )
        try:
            transaction = driver.transaction(request.message, request.context)
        except (asyncio.CancelledError, AFError):
            raise
        except Exception as error:
            raise PlatformError(
                PlatformReason.TRANSPORT_FAILED,
                stage="application.transaction-open",
                protected_cause=error,
            ) from error
        if not callable(getattr(transaction, "__aenter__", None)) or not callable(
            getattr(transaction, "__aexit__", None)
        ):
            raise InvalidDispatchContract("transaction driver returned an invalid boundary")
        async with transaction:
            return await call_next(request)


@dataclass(frozen=True, slots=True)
class ApplicationTraceEvent:
    stage: str
    message_kind: str
    message_type: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        _bounded_text(self.stage, "application trace stage")
        _bounded_text(self.message_kind, "application trace message kind")
        _bounded_text(self.message_type, "application trace message type")
        object.__setattr__(self, "occurred_at", _aware_utc(self.occurred_at, "trace time"))


class TraceSink(Protocol):
    def record(self, event: ApplicationTraceEvent) -> object: ...


class TracingMiddleware:
    """Record deterministic dispatch lifecycle events without suppressing cancellation."""

    def __init__(
        self,
        sink: TraceSink,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(sink, "record", None)):
            raise InvalidDispatchContract("application trace sink must define record")
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(UTC))

    async def __call__(
        self,
        request: DispatchRequest,
        call_next: CallNext,
    ) -> ApplicationResult[object]:
        await self._record("started", request)
        try:
            result = await call_next(request)
        except asyncio.CancelledError:
            await self._record_preserving("cancelled", request)
            raise
        except Exception as error:
            try:
                await self._record("failed", request)
            except Exception as trace_error:
                raise ExceptionGroup(
                    "application dispatch and tracing failed",
                    (error, trace_error),
                ) from error
            raise
        await self._record("completed", request)
        return result

    async def _record(self, stage: str, request: DispatchRequest) -> None:
        event = ApplicationTraceEvent(
            stage=stage,
            message_kind=request.kind.value,
            message_type=type(request.message).__name__,
            occurred_at=self._clock(),
        )
        await _await_if_needed(self._sink.record(event))

    async def _record_preserving(self, stage: str, request: DispatchRequest) -> None:
        try:
            await self._record(stage, request)
        except Exception:
            # Cancellation remains authoritative; telemetry failure cannot replace it.
            return


class InProcessTestAdapter:
    """Test transport that delegates exactly once to the authoritative application bus."""

    def __init__(self, bus: ApplicationBus) -> None:
        if not isinstance(bus, ApplicationBus):
            raise InvalidDispatchContract("test adapter bus is invalid")
        self._bus = bus

    async def dispatch(
        self,
        message: object,
        *,
        context: ExecutionContext,
    ) -> ApplicationResult[object]:
        return await self._bus.dispatch(message, context=context)
