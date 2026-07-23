"""Immutable invocation context backed by context-local storage."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import cast

from af.framework.errors import PlatformError, PlatformReason

_IDENTIFIER_MAX_LENGTH = 200


class _UnsetType:
    pass


_UNSET = _UnsetType()


class AcceptanceScope(StrEnum):
    CHILD = "child"
    DETERMINISTIC = "deterministic"
    REVIEWED_GENERATIVE = "reviewed-generative"
    OPERATOR_EVALUATION = "operator-evaluation"


def _validate_scalar(value: str | None, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if (
        not value
        or len(value) > _IDENTIFIER_MAX_LENGTH
        or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", value)
    ):
        raise ValueError(f"{label} must be a bounded non-empty single-line value")


@dataclass(frozen=True, slots=True, repr=False)
class Principal:
    subject_id: str
    principal_type: str
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    claims: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if len(self.roles) > 256 or len(self.scopes) > 256 or len(self.claims) > 256:
            raise ValueError("principal collections contain too many values")
        object.__setattr__(self, "roles", tuple(self.roles))
        object.__setattr__(self, "scopes", tuple(self.scopes))
        object.__setattr__(self, "claims", tuple(tuple(item) for item in self.claims))
        _validate_scalar(self.subject_id, "subject id")
        _validate_scalar(self.principal_type, "principal type")
        for value in (*self.roles, *self.scopes):
            _validate_scalar(value, "principal role or scope")
        for key, value in self.claims:
            _validate_scalar(key, "claim key")
            _validate_scalar(value, "claim value")

    def __repr__(self) -> str:
        return "Principal(identity='[REDACTED]', authorization='[REDACTED]')"


@dataclass(frozen=True, slots=True, repr=False)
class TraceContext:
    trace_id: str
    span_id: str | None = None
    trace_flags: int = 0

    def __post_init__(self) -> None:
        _validate_scalar(self.trace_id, "trace id")
        _validate_scalar(self.span_id, "span id")
        if type(self.trace_flags) is not int or self.trace_flags < 0:
            raise ValueError("trace flags must be a non-negative integer")

    def __repr__(self) -> str:
        return (
            "TraceContext(trace_id='[REDACTED]', span_id='[REDACTED]', "
            f"trace_flags={self.trace_flags})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class CancellationToken:
    _event: asyncio.Event = field(default_factory=asyncio.Event, compare=False)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()

    def __repr__(self) -> str:
        return f"CancellationToken(cancelled={self.cancelled})"


@dataclass(frozen=True, slots=True, repr=False)
class ExecutionContext:
    principal: Principal | None = None
    tenant_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    deadline: datetime | None = None
    trace: TraceContext | None = None
    locale: str | None = None
    requested_scope: AcceptanceScope | None = None
    effective_scope: AcceptanceScope | None = None
    cancellation: CancellationToken = field(default_factory=CancellationToken)

    def __post_init__(self) -> None:
        for value, label in (
            (self.tenant_id, "tenant id"),
            (self.correlation_id, "correlation id"),
            (self.causation_id, "causation id"),
            (self.locale, "locale"),
        ):
            _validate_scalar(value, label)
        if self.principal is not None and not isinstance(self.principal, Principal):
            raise TypeError("principal must be a Principal")
        if self.trace is not None and not isinstance(self.trace, TraceContext):
            raise TypeError("trace must be a TraceContext")
        if not isinstance(self.cancellation, CancellationToken):
            raise TypeError("cancellation must be a CancellationToken")
        if self.requested_scope is not None and not isinstance(
            self.requested_scope, AcceptanceScope
        ):
            raise TypeError("requested scope must be an AcceptanceScope")
        if self.effective_scope is not None and not isinstance(
            self.effective_scope, AcceptanceScope
        ):
            raise TypeError("effective scope must be an AcceptanceScope")
        if self.deadline is not None and self.deadline.utcoffset() is None:
            raise ValueError("deadline must include a timezone")

    def __repr__(self) -> str:
        return (
            "ExecutionContext(principal='[REDACTED]', tenant_id='[REDACTED]', "
            "correlation_id='[REDACTED]', trace='[REDACTED]')"
        )


class ContextLease:
    """Revocable authority to expose one execution context ambiently."""

    __slots__ = ("_active", "_context", "_validator")
    _active: bool
    _context: ExecutionContext | None
    _validator: Callable[[], bool] | None

    def __init__(
        self,
        context: ExecutionContext,
        *,
        validator: Callable[[], bool] | None = None,
    ) -> None:
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_active", True)
        object.__setattr__(self, "_validator", validator)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("context lease authority is read-only")

    @property
    def context(self) -> ExecutionContext | None:
        return self._context

    @property
    def active(self) -> bool:
        return self._active

    def permits_current_caller(self) -> bool:
        return (
            self._active
            and self._context is not None
            and (self._validator is None or self._validator())
        )

    def revoke(self) -> None:
        object.__setattr__(self, "_active", False)
        object.__setattr__(self, "_context", None)
        object.__setattr__(self, "_validator", None)


@dataclass(slots=True)
class _ContextBinding:
    lease: ContextLease
    parent: _ContextBinding | None = None
    active: bool = True

    def is_active(self) -> bool:
        return (
            self.active
            and self.lease.permits_current_caller()
            and (self.parent is None or self.parent.is_active())
        )


_CURRENT_CONTEXT: ContextVar[_ContextBinding | None] = ContextVar(
    "af_execution_context", default=None
)


def maybe_current_context() -> ExecutionContext | None:
    binding = _CURRENT_CONTEXT.get()
    return binding.lease.context if binding is not None and binding.is_active() else None


def current_context() -> ExecutionContext:
    context = maybe_current_context()
    if context is None:
        raise PlatformError(
            PlatformReason.CONTEXT_UNAVAILABLE,
            stage="context.resolve",
        )
    return context


@contextmanager
def use_context(
    context: ExecutionContext, *, lease: ContextLease | None = None
) -> Iterator[ExecutionContext]:
    if not isinstance(context, ExecutionContext):
        raise TypeError("context must be an ExecutionContext")
    resolved_lease = ContextLease(context) if lease is None else lease
    if resolved_lease.context is not context or not resolved_lease.active:
        raise ValueError("context lease must be active and bound to the same context")
    binding = _ContextBinding(resolved_lease, parent=_CURRENT_CONTEXT.get())
    token = _CURRENT_CONTEXT.set(binding)
    try:
        yield context
    finally:
        binding.active = False
        resolved_lease.revoke()
        _CURRENT_CONTEXT.reset(token)


@contextmanager
def without_context() -> Iterator[None]:
    """Temporarily mask invocation identity for broader-lifetime construction."""
    token = _CURRENT_CONTEXT.set(None)
    try:
        yield
    finally:
        _CURRENT_CONTEXT.reset(token)


def derive_context(
    parent: ExecutionContext,
    *,
    correlation_id: str | None | _UnsetType = _UNSET,
    causation_id: str | None | _UnsetType = _UNSET,
    deadline: datetime | None | _UnsetType = _UNSET,
    trace: TraceContext | None | _UnsetType = _UNSET,
    locale: str | None | _UnsetType = _UNSET,
    requested_scope: AcceptanceScope | None | _UnsetType = _UNSET,
) -> ExecutionContext:
    child_deadline = parent.deadline if deadline is _UNSET else cast(datetime | None, deadline)
    if (child_deadline is None and parent.deadline is not None) or (
        child_deadline is not None
        and parent.deadline is not None
        and child_deadline > parent.deadline
    ):
        raise ValueError("derived context cannot widen its deadline")
    return ExecutionContext(
        principal=parent.principal,
        tenant_id=parent.tenant_id,
        correlation_id=(
            parent.correlation_id if correlation_id is _UNSET else cast(str | None, correlation_id)
        ),
        causation_id=(
            parent.causation_id if causation_id is _UNSET else cast(str | None, causation_id)
        ),
        deadline=child_deadline,
        trace=parent.trace if trace is _UNSET else cast(TraceContext | None, trace),
        locale=parent.locale if locale is _UNSET else cast(str | None, locale),
        requested_scope=(
            parent.requested_scope
            if requested_scope is _UNSET
            else cast(AcceptanceScope | None, requested_scope)
        ),
        effective_scope=parent.effective_scope,
        cancellation=parent.cancellation,
    )
