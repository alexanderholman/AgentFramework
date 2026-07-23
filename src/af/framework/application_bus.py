"""Transport-neutral application dispatch, authorization, and provenance contracts."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast

from af.framework.context import AcceptanceScope, ExecutionContext, Principal, use_context
from af.framework.errors import AFError, PlatformError, PlatformReason

__all__ = (
    "ApplicationBus",
    "ApplicationMessage",
    "ApplicationMiddleware",
    "ApplicationResult",
    "ApplicationTraceEvent",
    "AuthorizationMiddleware",
    "AuthorizationRegistry",
    "Command",
    "ContextMiddleware",
    "FailureSanitizationMiddleware",
    "Gate",
    "GateDecision",
    "GateDenied",
    "GateEvidence",
    "GateReason",
    "GateRequirement",
    "HandlerRegistry",
    "InProcessApplicationAdapter",
    "PolicyOutcome",
    "Provenance",
    "ProvenanceRef",
    "Query",
    "ResourcePolicy",
    "TraceSink",
    "TracingMiddleware",
    "TransactionMiddleware",
    "ValidationMiddleware",
    "ValidationRegistry",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_VERSION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.+-]{0,126}[A-Za-z0-9])?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_COLLECTION = 256
_MAX_TEXT = 200


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical identifier")
    return value


def _version(value: object, field: str) -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded version identifier")
    return value


def _digest(value: object, field: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


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


@dataclass(frozen=True, slots=True)
class Command:
    """Immutable marker base for application commands."""


@dataclass(frozen=True, slots=True)
class Query:
    """Immutable marker base for application queries."""


type ApplicationMessage = Command | Query


class GateReason(StrEnum):
    ALLOWED = "allowed"
    DEFAULT_DENY = "default_deny"
    PRINCIPAL_REQUIRED = "principal_required"
    TENANT_DENIED = "tenant_denied"
    SCOPE_DENIED = "scope_denied"
    ACCEPTANCE_SCOPE_DENIED = "acceptance_scope_denied"
    POLICY_DENIED = "policy_denied"


@dataclass(frozen=True, slots=True)
class GateEvidence:
    """A bounded non-secret evidence code retained with one gate decision."""

    code: str

    def __post_init__(self) -> None:
        _identifier(self.code, "gate evidence code")


@dataclass(frozen=True, slots=True)
class GateRequirement:
    """Authorization facts known before resource or search-candidate access."""

    action: str
    resource_type: str
    tenant_id: str | None = None
    required_scopes: tuple[str, ...] = ()
    required_acceptance_scope: AcceptanceScope | None = None
    resource_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.action, "gate action")
        _identifier(self.resource_type, "gate resource type")
        if self.tenant_id is not None:
            _bounded_text(self.tenant_id, "gate tenant id")
        scopes = tuple(self.required_scopes)
        if len(scopes) > _MAX_COLLECTION:
            raise ValueError("gate requirement contains too many scopes")
        for scope in scopes:
            _bounded_text(scope, "required principal scope")
        object.__setattr__(self, "required_scopes", scopes)
        if self.required_acceptance_scope is not None and not isinstance(
            self.required_acceptance_scope,
            AcceptanceScope,
        ):
            raise ValueError("required acceptance scope is invalid")
        if self.resource_fingerprint is not None:
            _digest(self.resource_fingerprint, "resource fingerprint")


@dataclass(frozen=True, slots=True)
class PolicyOutcome:
    """A policy's bounded answer; Gate supplies authoritative reason and version."""

    allowed: bool
    evidence: tuple[GateEvidence, ...] = ()

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise ValueError("policy outcome allowed must be boolean")
        evidence = tuple(self.evidence)
        if len(evidence) > _MAX_COLLECTION or any(
            not isinstance(item, GateEvidence) for item in evidence
        ):
            raise ValueError("policy outcome evidence is invalid")
        object.__setattr__(self, "evidence", evidence)


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reason: GateReason
    policy_version: str
    evidence: tuple[GateEvidence, ...]
    requested_scope: AcceptanceScope | None
    effective_scope: AcceptanceScope | None

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool or not isinstance(self.reason, GateReason):
            raise ValueError("gate decision status is invalid")
        if self.allowed != (self.reason is GateReason.ALLOWED):
            raise ValueError("gate decision reason does not match its status")
        _version(self.policy_version, "gate policy version")
        evidence = tuple(self.evidence)
        if len(evidence) > _MAX_COLLECTION or any(
            not isinstance(item, GateEvidence) for item in evidence
        ):
            raise ValueError("gate decision evidence is invalid")
        object.__setattr__(self, "evidence", evidence)
        for scope in (self.requested_scope, self.effective_scope):
            if scope is not None and not isinstance(scope, AcceptanceScope):
                raise ValueError("gate decision acceptance scope is invalid")


class GateDenied(PlatformError):
    """Sanitized default-deny authorization failure with protected gate evidence."""

    def __init__(self, decision: GateDecision) -> None:
        if not isinstance(decision, GateDecision) or decision.allowed:
            raise ValueError("gate denial requires a denied GateDecision")
        self.decision = decision
        super().__init__(
            PlatformReason.AUTHORIZATION_DENIED,
            stage="application.authorization",
        )


class ResourcePolicy(Protocol):
    def decide(
        self,
        principal: Principal,
        requirement: GateRequirement,
        context: ExecutionContext,
    ) -> PolicyOutcome: ...


@dataclass(frozen=True, slots=True)
class _PolicyRegistration:
    version: str
    policy: ResourcePolicy


class Gate:
    """Central default-deny policy gate with non-enumerating reason codes."""

    def __init__(self, *, policy_version: str) -> None:
        self._policy_version = _version(policy_version, "gate policy version")
        self._policies: dict[tuple[str, str], _PolicyRegistration] = {}

    @property
    def policy_version(self) -> str:
        return self._policy_version

    def register(
        self,
        resource_type: str,
        action: str,
        policy: ResourcePolicy,
        *,
        policy_version: str | None = None,
    ) -> None:
        key = (
            _identifier(resource_type, "policy resource type"),
            _identifier(action, "policy action"),
        )
        if not callable(getattr(policy, "decide", None)):
            raise ValueError("resource policy must define decide")
        if key in self._policies:
            raise ValueError("resource policy is already registered")
        version = self._policy_version if policy_version is None else _version(
            policy_version,
            "resource policy version",
        )
        self._policies[key] = _PolicyRegistration(version, policy)

    def evaluate(
        self,
        context: ExecutionContext,
        requirement: GateRequirement | None,
    ) -> GateDecision:
        if not isinstance(context, ExecutionContext):
            raise TypeError("gate context must be an ExecutionContext")
        if requirement is None:
            return self._decision(context, False, GateReason.DEFAULT_DENY)
        if not isinstance(requirement, GateRequirement):
            raise TypeError("gate requirement must be a GateRequirement")
        principal = context.principal
        if principal is None:
            return self._decision(context, False, GateReason.PRINCIPAL_REQUIRED)
        if requirement.tenant_id is not None and context.tenant_id != requirement.tenant_id:
            return self._decision(context, False, GateReason.TENANT_DENIED)
        if not set(requirement.required_scopes).issubset(principal.scopes):
            return self._decision(context, False, GateReason.SCOPE_DENIED)
        required_acceptance = requirement.required_acceptance_scope
        if required_acceptance is not None and context.effective_scope is not required_acceptance:
            return self._decision(context, False, GateReason.ACCEPTANCE_SCOPE_DENIED)
        registration = self._policies.get((requirement.resource_type, requirement.action))
        if registration is None:
            return self._decision(context, False, GateReason.DEFAULT_DENY)
        try:
            outcome = registration.policy.decide(principal, requirement, context)
        except Exception:
            return self._decision(
                context,
                False,
                GateReason.POLICY_DENIED,
                policy_version=registration.version,
                evidence=(GateEvidence("policy.failure"),),
            )
        if not isinstance(outcome, PolicyOutcome):
            return self._decision(
                context,
                False,
                GateReason.POLICY_DENIED,
                policy_version=registration.version,
                evidence=(GateEvidence("policy.invalid_outcome"),),
            )
        return self._decision(
            context,
            outcome.allowed,
            GateReason.ALLOWED if outcome.allowed else GateReason.POLICY_DENIED,
            policy_version=registration.version,
            evidence=outcome.evidence,
        )

    def authorize(
        self,
        context: ExecutionContext,
        requirement: GateRequirement | None,
    ) -> GateDecision:
        decision = self.evaluate(context, requirement)
        if not decision.allowed:
            raise GateDenied(decision)
        return decision

    def _decision(
        self,
        context: ExecutionContext,
        allowed: bool,
        reason: GateReason,
        *,
        policy_version: str | None = None,
        evidence: tuple[GateEvidence, ...] = (),
    ) -> GateDecision:
        return GateDecision(
            allowed=allowed,
            reason=reason,
            policy_version=policy_version or self._policy_version,
            evidence=evidence,
            requested_scope=context.requested_scope,
            effective_scope=context.effective_scope,
        )


@dataclass(frozen=True, slots=True)
class ProvenanceRef:
    kind: str
    reference_digest: str
    version: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.kind, "provenance reference kind")
        _digest(self.reference_digest, "provenance reference digest")
        if self.version is not None:
            _version(self.version, "provenance reference version")


@dataclass(frozen=True, slots=True)
class Provenance:
    """Transport-independent evidence emitted by one application use case."""

    use_case: str
    handler_id: str
    handler_version: str
    occurred_at: datetime
    sources: tuple[ProvenanceRef, ...] = ()
    derived_from: tuple[ProvenanceRef, ...] = ()
    authorization: GateDecision | None = None

    def __post_init__(self) -> None:
        _identifier(self.use_case, "provenance use case")
        _identifier(self.handler_id, "provenance handler id")
        _version(self.handler_version, "provenance handler version")
        object.__setattr__(self, "occurred_at", _aware_utc(self.occurred_at, "provenance time"))
        for field_name in ("sources", "derived_from"):
            values = tuple(getattr(self, field_name))
            if len(values) > _MAX_COLLECTION or any(
                not isinstance(item, ProvenanceRef) for item in values
            ):
                raise ValueError(f"provenance {field_name} is invalid")
            object.__setattr__(self, field_name, values)
        if self.authorization is not None and not isinstance(self.authorization, GateDecision):
            raise ValueError("provenance authorization is invalid")

    def authorized_by(self, decision: GateDecision) -> Provenance:
        if not isinstance(decision, GateDecision) or not decision.allowed:
            raise ValueError("provenance requires an allowed gate decision")
        if self.authorization is not None and self.authorization != decision:
            raise ValueError("provenance already contains different authorization evidence")
        return replace(self, authorization=decision)


@dataclass(frozen=True, slots=True)
class ApplicationResult[T]:
    value: T
    provenance: Provenance

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, Provenance):
            raise ValueError("application result requires provenance")

    def authorized_by(self, decision: GateDecision) -> ApplicationResult[T]:
        return replace(self, provenance=self.provenance.authorized_by(decision))


type ErasedResult = ApplicationResult[object]
type ErasedHandler = Callable[
    [ApplicationMessage, ExecutionContext],
    Awaitable[ErasedResult],
]
type NextHandler = ErasedHandler


class HandlerRegistry:
    """Exact-type command/query handler registry with no reflective fallback."""

    def __init__(self) -> None:
        self._handlers: dict[type[ApplicationMessage], ErasedHandler] = {}

    def register[M: ApplicationMessage, R](
        self,
        message_type: type[M],
        handler: Callable[[M, ExecutionContext], Awaitable[ApplicationResult[R]]],
    ) -> None:
        if not isinstance(message_type, type) or not issubclass(message_type, (Command, Query)):
            raise ValueError("handler message type must be a Command or Query")
        if not callable(handler):
            raise ValueError("application handler must be callable")
        if message_type in self._handlers:
            raise ValueError("application handler is already registered")
        self._handlers[message_type] = cast(ErasedHandler, handler)

    def resolve(self, message: ApplicationMessage) -> ErasedHandler:
        handler = self._handlers.get(type(message))
        if handler is None:
            raise PlatformError(
                PlatformReason.DEPENDENCY_RESOLUTION_FAILED,
                stage="application.handler-resolution",
            )
        return handler


class ApplicationMiddleware(Protocol):
    async def __call__(
        self,
        message: ApplicationMessage,
        context: ExecutionContext,
        call_next: NextHandler,
    ) -> ErasedResult: ...


class ApplicationBus:
    """Dispatch every application message through one deterministic middleware chain."""

    def __init__(
        self,
        registry: HandlerRegistry,
        *,
        middleware: tuple[ApplicationMiddleware, ...] = (),
    ) -> None:
        if not isinstance(registry, HandlerRegistry):
            raise TypeError("application bus registry must be a HandlerRegistry")
        chain = tuple(middleware)
        if any(not callable(item) for item in chain):
            raise ValueError("application middleware must be callable")
        self._registry = registry
        self._middleware = chain

    async def dispatch[M: ApplicationMessage, R](
        self,
        message: M,
        context: ExecutionContext,
    ) -> ApplicationResult[R]:
        if not isinstance(message, (Command, Query)):
            raise TypeError("application message must be a Command or Query")
        if not isinstance(context, ExecutionContext):
            raise TypeError("application context must be an ExecutionContext")
        handler = self._registry.resolve(message)

        async def invoke(
            index: int,
            current_message: ApplicationMessage,
            current_context: ExecutionContext,
        ) -> ErasedResult:
            self._raise_if_cancelled(current_context)
            if index == len(self._middleware):
                result = await handler(current_message, current_context)
            else:
                current = self._middleware[index]

                async def next_handler(
                    nested_message: ApplicationMessage,
                    nested_context: ExecutionContext,
                ) -> ErasedResult:
                    return await invoke(index + 1, nested_message, nested_context)

                result = await current(current_message, current_context, next_handler)
            self._raise_if_cancelled(current_context)
            if not isinstance(result, ApplicationResult):
                raise PlatformError(
                    PlatformReason.VALIDATION_FAILED,
                    stage="application.result-validation",
                )
            return result

        return cast(ApplicationResult[R], await invoke(0, message, context))

    @staticmethod
    def _raise_if_cancelled(context: ExecutionContext) -> None:
        if context.cancellation.cancelled:
            raise asyncio.CancelledError


class InProcessApplicationAdapter:
    """Minimal transport-test adapter proving semantic equivalence with direct dispatch."""

    def __init__(self, bus: ApplicationBus) -> None:
        if not isinstance(bus, ApplicationBus):
            raise TypeError("adapter bus must be an ApplicationBus")
        self._bus = bus

    async def send[M: ApplicationMessage, R](
        self,
        message: M,
        context: ExecutionContext,
    ) -> ApplicationResult[R]:
        return await self._bus.dispatch(message, context)


class ContextMiddleware:
    async def __call__(
        self,
        message: ApplicationMessage,
        context: ExecutionContext,
        call_next: NextHandler,
    ) -> ErasedResult:
        with use_context(context):
            return await call_next(message, context)


class FailureSanitizationMiddleware:
    async def __call__(
        self,
        message: ApplicationMessage,
        context: ExecutionContext,
        call_next: NextHandler,
    ) -> ErasedResult:
        try:
            return await call_next(message, context)
        except (asyncio.CancelledError, AFError):
            raise
        except Exception as error:
            raise PlatformError(
                PlatformReason.PERMANENT_FAILURE,
                stage="application.dispatch",
                protected_cause=error,
            ) from error


type Validator = Callable[[ApplicationMessage, ExecutionContext], Awaitable[None]]


class ValidationRegistry:
    def __init__(self) -> None:
        self._validators: dict[type[ApplicationMessage], Validator] = {}

    def register[M: ApplicationMessage](
        self,
        message_type: type[M],
        validator: Callable[[M, ExecutionContext], Awaitable[None]],
    ) -> None:
        if not isinstance(message_type, type) or not issubclass(message_type, (Command, Query)):
            raise ValueError("validator message type must be a Command or Query")
        if not callable(validator):
            raise ValueError("application validator must be callable")
        if message_type in self._validators:
            raise ValueError("application validator is already registered")
        self._validators[message_type] = cast(Validator, validator)

    def resolve(self, message: ApplicationMessage) -> Validator | None:
        return self._validators.get(type(message))


class ValidationMiddleware:
    def __init__(self, validators: ValidationRegistry) -> None:
        if not isinstance(validators, ValidationRegistry):
            raise TypeError("validation registry is invalid")
        self._validators = validators

    async def __call__(
        self,
        message: ApplicationMessage,
        context: ExecutionContext,
        call_next: NextHandler,
    ) -> ErasedResult:
        validator = self._validators.resolve(message)
        if validator is not None:
            try:
                await validator(message, context)
            except (asyncio.CancelledError, AFError):
                raise
            except Exception as error:
                raise PlatformError(
                    PlatformReason.VALIDATION_FAILED,
                    stage="application.validation",
                    protected_cause=error,
                ) from error
        return await call_next(message, context)


type RequirementResolver = Callable[
    [ApplicationMessage, ExecutionContext],
    GateRequirement,
]


class AuthorizationRegistry:
    def __init__(self) -> None:
        self._resolvers: dict[type[ApplicationMessage], RequirementResolver] = {}

    def register[M: ApplicationMessage](
        self,
        message_type: type[M],
        resolver: Callable[[M, ExecutionContext], GateRequirement],
    ) -> None:
        if not isinstance(message_type, type) or not issubclass(message_type, (Command, Query)):
            raise ValueError("authorization message type must be a Command or Query")
        if not callable(resolver):
            raise ValueError("authorization resolver must be callable")
        if message_type in self._resolvers:
            raise ValueError("authorization resolver is already registered")
        self._resolvers[message_type] = cast(RequirementResolver, resolver)

    def resolve(
        self,
        message: ApplicationMessage,
        context: ExecutionContext,
    ) -> GateRequirement | None:
        resolver = self._resolvers.get(type(message))
        if resolver is None:
            return None
        requirement = resolver(message, context)
        if not isinstance(requirement, GateRequirement):
            raise PlatformError(
                PlatformReason.VALIDATION_FAILED,
                stage="application.authorization-requirement",
            )
        return requirement


class AuthorizationMiddleware:
    def __init__(self, gate: Gate, registry: AuthorizationRegistry) -> None:
        if not isinstance(gate, Gate) or not isinstance(registry, AuthorizationRegistry):
            raise TypeError("authorization middleware dependencies are invalid")
        self._gate = gate
        self._registry = registry

    async def __call__(
        self,
        message: ApplicationMessage,
        context: ExecutionContext,
        call_next: NextHandler,
    ) -> ErasedResult:
        requirement = self._registry.resolve(message, context)
        decision = self._gate.authorize(context, requirement)
        result = await call_next(message, context)
        return result.authorized_by(decision)


@dataclass(frozen=True, slots=True)
class ApplicationTraceEvent:
    stage: str
    message_type: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.stage, "application trace stage")
        _bounded_text(self.message_type, "application trace message type")
        object.__setattr__(self, "occurred_at", _aware_utc(self.occurred_at, "trace time"))


class TraceSink(Protocol):
    def record(self, event: ApplicationTraceEvent) -> None: ...


class TracingMiddleware:
    def __init__(
        self,
        sink: TraceSink,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(sink, "record", None)):
            raise TypeError("trace sink must define record")
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(UTC))

    async def __call__(
        self,
        message: ApplicationMessage,
        context: ExecutionContext,
        call_next: NextHandler,
    ) -> ErasedResult:
        message_type = type(message).__name__
        self._record("started", message_type)
        try:
            result = await call_next(message, context)
        except asyncio.CancelledError:
            self._record_preserving("cancelled", message_type)
            raise
        except Exception as error:
            try:
                self._record("failed", message_type)
            except Exception as trace_error:
                raise ExceptionGroup(
                    "application dispatch and tracing failed",
                    (error, trace_error),
                ) from error
            raise
        self._record("completed", message_type)
        return result

    def _record(self, stage: str, message_type: str) -> None:
        self._sink.record(ApplicationTraceEvent(stage, message_type, self._clock()))

    def _record_preserving(self, stage: str, message_type: str) -> None:
        try:
            self._record(stage, message_type)
        except Exception:
            # Cancellation remains authoritative; telemetry failure must not suppress it.
            return


type TransactionFactory = Callable[
    [ApplicationMessage, ExecutionContext],
    AbstractAsyncContextManager[None] | None,
]


class TransactionMiddleware:
    def __init__(self, factory: TransactionFactory) -> None:
        if not callable(factory):
            raise TypeError("transaction factory must be callable")
        self._factory = factory

    async def __call__(
        self,
        message: ApplicationMessage,
        context: ExecutionContext,
        call_next: NextHandler,
    ) -> ErasedResult:
        transaction = self._factory(message, context)
        if transaction is None:
            return await call_next(message, context)
        async with transaction:
            return await call_next(message, context)
