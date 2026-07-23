"""Transport-neutral application dispatch, policy, and provenance contracts."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import StrEnum
from inspect import isawaitable
from typing import Protocol, cast

from af.framework.context import AcceptanceScope, ExecutionContext, use_context
from af.framework.errors import AFError, PlatformError, PlatformReason

__all__ = (
    "ApplicationBus",
    "ApplicationResult",
    "AuthorizationRequest",
    "Command",
    "DerivationEvidence",
    "DispatchError",
    "DispatchRequest",
    "Gate",
    "GateDecision",
    "GateEffect",
    "GateMiddleware",
    "HandlerRegistry",
    "InvalidDispatchContract",
    "MessageKind",
    "Middleware",
    "Policy",
    "PolicyEvidence",
    "ProvenanceRecord",
    "Query",
    "SanitizedFailureMiddleware",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_VERSION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.+-]{0,126}[A-Za-z0-9])?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SCOPE_ORDER = {
    AcceptanceScope.CHILD: 0,
    AcceptanceScope.DETERMINISTIC: 1,
    AcceptanceScope.REVIEWED_GENERATIVE: 2,
    AcceptanceScope.OPERATOR_EVALUATION: 3,
}


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


class InvalidDispatchContract(AFError, ValueError):
    """A message, handler, middleware, policy, or result contract is invalid."""


class DispatchError(PlatformError):
    """Sanitized dispatch resolution failure."""

    def __init__(self, *, protected_cause: BaseException | None = None) -> None:
        super().__init__(
            PlatformReason.DEPENDENCY_RESOLUTION_FAILED,
            stage="application.dispatch",
            protected_cause=protected_cause,
        )


class Command:
    """Marker base for immutable application commands."""

    __slots__ = ()


class Query:
    """Marker base for immutable application queries."""

    __slots__ = ()


type Message = Command | Query


class MessageKind(StrEnum):
    COMMAND = "command"
    QUERY = "query"


@dataclass(frozen=True, slots=True)
class PolicyEvidence:
    kind: str
    code: str

    def __post_init__(self) -> None:
        _identifier(self.kind, "policy evidence kind")
        _identifier(self.code, "policy evidence code")

    def __repr__(self) -> str:
        return "PolicyEvidence(kind='[REDACTED]', code='[REDACTED]')"


class GateEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True, repr=False)
class GateDecision:
    effect: GateEffect
    policy_version: str
    reason_code: str
    evidence: tuple[PolicyEvidence, ...] = ()
    effective_scope: AcceptanceScope | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.effect, GateEffect):
            raise ValueError("gate effect is invalid")
        _version(self.policy_version, "policy version")
        _identifier(self.reason_code, "gate reason code")
        normalized = tuple(self.evidence)
        if len(normalized) > 256 or any(
            not isinstance(item, PolicyEvidence) for item in normalized
        ):
            raise ValueError("gate evidence is invalid")
        if len(set(normalized)) != len(normalized):
            raise ValueError("gate evidence must be unique")
        object.__setattr__(self, "evidence", normalized)
        if self.effective_scope is not None and not isinstance(
            self.effective_scope,
            AcceptanceScope,
        ):
            raise ValueError("gate effective scope is invalid")
        if self.effect is GateEffect.DENY and self.effective_scope is not None:
            raise ValueError("denied gate decisions cannot grant an effective scope")
        if self.effect is GateEffect.ALLOW and self.effective_scope is None:
            raise ValueError("allowed gate decisions require an effective scope")

    def __repr__(self) -> str:
        return (
            f"GateDecision(effect={self.effect.value!r}, "
            f"policy_version={self.policy_version!r}, "
            f"reason_code={self.reason_code!r}, evidence='[REDACTED]', "
            f"effective_scope={self.effective_scope!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AuthorizationRequest:
    action: str
    resource_type: str
    protected_resource_id: str | None
    required_scope: AcceptanceScope

    def __post_init__(self) -> None:
        _identifier(self.action, "authorization action")
        _identifier(self.resource_type, "authorization resource type")
        if self.protected_resource_id is not None and (
            type(self.protected_resource_id) is not str
            or not self.protected_resource_id
            or len(self.protected_resource_id) > 512
            or re.search(
                r"[\x00-\x1f\x7f-\x9f\u2028\u2029]",
                self.protected_resource_id,
            )
        ):
            raise ValueError("protected resource id is invalid")
        if not isinstance(self.required_scope, AcceptanceScope):
            raise ValueError("required authorization scope is invalid")

    def __repr__(self) -> str:
        return (
            f"AuthorizationRequest(action={self.action!r}, "
            f"resource_type={self.resource_type!r}, "
            "protected_resource_id='[REDACTED]', "
            f"required_scope={self.required_scope!r})"
        )


class Policy(Protocol):
    """Deterministic transport-neutral authorization policy."""

    def __call__(
        self,
        request: AuthorizationRequest,
        context: ExecutionContext,
    ) -> GateDecision | None: ...


class Gate:
    """Central default-deny combination of deterministic policy decisions."""

    def __init__(
        self,
        *,
        policy_version: str,
        policies: Iterable[Policy],
    ) -> None:
        try:
            self._policy_version = _version(policy_version, "policy version")
            normalized = tuple(policies)
            if len(normalized) > 256 or any(not callable(policy) for policy in normalized):
                raise ValueError("gate policies are invalid")
        except (TypeError, ValueError) as error:
            raise InvalidDispatchContract("gate configuration is invalid") from error
        self._policies = normalized

    @property
    def policy_version(self) -> str:
        return self._policy_version

    def decide(
        self,
        request: AuthorizationRequest,
        context: ExecutionContext,
    ) -> GateDecision:
        if not isinstance(request, AuthorizationRequest) or not isinstance(
            context,
            ExecutionContext,
        ):
            raise InvalidDispatchContract("gate request and context are invalid")
        decisions: list[GateDecision] = []
        for policy in self._policies:
            try:
                decision = policy(request, context)
            except PlatformError:
                raise
            except Exception as error:
                raise PlatformError(
                    PlatformReason.AUTHORIZATION_DENIED,
                    stage="application.gate-policy",
                    protected_cause=error,
                ) from error
            if decision is None:
                continue
            self._validate_decision(decision, request, context)
            decisions.append(decision)
        denied = next(
            (decision for decision in decisions if decision.effect is GateEffect.DENY),
            None,
        )
        if denied is not None:
            return denied
        allowed = next(
            (decision for decision in decisions if decision.effect is GateEffect.ALLOW),
            None,
        )
        if allowed is not None:
            return allowed
        return GateDecision(
            effect=GateEffect.DENY,
            policy_version=self._policy_version,
            reason_code="default_deny",
        )

    def _validate_decision(
        self,
        decision: GateDecision,
        request: AuthorizationRequest,
        context: ExecutionContext,
    ) -> None:
        if not isinstance(decision, GateDecision):
            raise InvalidDispatchContract("policy returned an invalid gate decision")
        if decision.policy_version != self._policy_version:
            raise InvalidDispatchContract("gate policy version does not match")
        if decision.effect is GateEffect.DENY:
            return
        effective = decision.effective_scope
        assert effective is not None
        if _SCOPE_ORDER[effective] > _SCOPE_ORDER[request.required_scope]:
            raise InvalidDispatchContract("gate decision widens the required scope")
        requested = context.requested_scope
        if requested is not None and _SCOPE_ORDER[effective] > _SCOPE_ORDER[requested]:
            raise InvalidDispatchContract("gate decision widens the requested scope")
        current = context.effective_scope
        if current is not None and _SCOPE_ORDER[effective] > _SCOPE_ORDER[current]:
            raise InvalidDispatchContract("gate decision widens the effective scope")


@dataclass(frozen=True, slots=True)
class DerivationEvidence:
    source_id: str
    source_digest: str
    relationship: str
    model_derived: bool

    def __post_init__(self) -> None:
        _identifier(self.source_id, "derivation source id")
        _digest(self.source_digest, "derivation source digest")
        _identifier(self.relationship, "derivation relationship")
        if type(self.model_derived) is not bool:
            raise ValueError("model-derived flag must be boolean")


@dataclass(frozen=True, slots=True)
class ProvenanceRecord:
    operation_id: str
    operation_version: str
    input_digest: str
    output_digest: str
    derivations: tuple[DerivationEvidence, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "provenance operation id")
        _version(self.operation_version, "provenance operation version")
        _digest(self.input_digest, "provenance input digest")
        _digest(self.output_digest, "provenance output digest")
        normalized = tuple(self.derivations)
        if len(normalized) > 256 or any(
            not isinstance(item, DerivationEvidence) for item in normalized
        ):
            raise ValueError("provenance derivations are invalid")
        if len(set(normalized)) != len(normalized):
            raise ValueError("provenance derivations must be unique")
        object.__setattr__(self, "derivations", normalized)


@dataclass(frozen=True, slots=True)
class ApplicationResult[T]:
    value: T
    provenance: tuple[ProvenanceRecord, ...] = ()
    gate_decision: GateDecision | None = None

    def __post_init__(self) -> None:
        normalized = tuple(self.provenance)
        if len(normalized) > 256 or any(
            not isinstance(item, ProvenanceRecord) for item in normalized
        ):
            raise ValueError("result provenance is invalid")
        if len(set(normalized)) != len(normalized):
            raise ValueError("result provenance must be unique")
        object.__setattr__(self, "provenance", normalized)
        if self.gate_decision is not None and not isinstance(
            self.gate_decision,
            GateDecision,
        ):
            raise ValueError("result gate decision is invalid")


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    message: Message
    kind: MessageKind
    context: ExecutionContext

    def __post_init__(self) -> None:
        if not isinstance(self.message, (Command, Query)):
            raise InvalidDispatchContract("dispatch message must be a Command or Query")
        if not isinstance(self.kind, MessageKind):
            raise InvalidDispatchContract("dispatch kind is invalid")
        if isinstance(self.message, Command) != (self.kind is MessageKind.COMMAND):
            raise InvalidDispatchContract("dispatch kind does not match its message")
        if not isinstance(self.context, ExecutionContext):
            raise InvalidDispatchContract("dispatch context is invalid")


type CallNext = Callable[[DispatchRequest], Awaitable[ApplicationResult[object]]]


class Middleware(Protocol):
    """Ordered application middleware around one dispatch."""

    def __call__(
        self,
        request: DispatchRequest,
        call_next: CallNext,
    ) -> ApplicationResult[object] | Awaitable[ApplicationResult[object]]: ...


type Handler = Callable[[Message, ExecutionContext], object]


def _message_kind(message_type: type[object]) -> MessageKind:
    is_command = issubclass(message_type, Command)
    is_query = issubclass(message_type, Query)
    if is_command == is_query:
        raise InvalidDispatchContract("message type must inherit exactly one of Command or Query")
    return MessageKind.COMMAND if is_command else MessageKind.QUERY


def _validate_message_type(message_type: object) -> tuple[type[Message], MessageKind]:
    if not isinstance(message_type, type):
        raise InvalidDispatchContract("message type must be a class")
    kind = _message_kind(message_type)
    if not is_dataclass(message_type):
        raise InvalidDispatchContract("message type must be an immutable dataclass")
    parameters = getattr(message_type, "__dataclass_params__", None)
    if parameters is None or not bool(getattr(parameters, "frozen", False)):
        raise InvalidDispatchContract("message type must be an immutable frozen dataclass")
    if any(field.init is False for field in fields(message_type)):
        raise InvalidDispatchContract("message fields must be constructor-defined")
    return cast(type[Message], message_type), kind


class HandlerRegistry:
    """Exact-type handler declarations frozen before dispatch begins."""

    def __init__(self) -> None:
        self._handlers: dict[type[Message], tuple[MessageKind, Handler]] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def message_types(self) -> tuple[type[Message], ...]:
        return tuple(self._handlers)

    def kind_for(self, message_type: type[object]) -> MessageKind:
        registered = self._handlers.get(cast(type[Message], message_type))
        if registered is None:
            raise DispatchError(protected_cause=LookupError("message handler is not registered"))
        return registered[0]

    def register(self, message_type: object, handler: object) -> None:
        if self._frozen:
            raise InvalidDispatchContract("handler registry is frozen")
        normalized, kind = _validate_message_type(message_type)
        if not callable(handler):
            raise InvalidDispatchContract("message handler must be callable")
        if normalized in self._handlers:
            raise InvalidDispatchContract("message handler is already registered")
        self._handlers[normalized] = (kind, cast(Handler, handler))

    def freeze(self) -> None:
        self._frozen = True

    def resolve(self, message: object) -> tuple[MessageKind, Handler]:
        normalized, expected_kind = _validate_message_type(type(message))
        registered = self._handlers.get(normalized)
        if registered is None:
            raise DispatchError(protected_cause=LookupError("message handler is not registered"))
        kind, handler = registered
        if kind is not expected_kind:
            raise InvalidDispatchContract("registered message kind changed")
        return kind, handler


async def _result(value: object) -> ApplicationResult[object]:
    if isawaitable(value):
        value = await cast(Awaitable[object], value)
    if not isinstance(value, ApplicationResult):
        raise InvalidDispatchContract("handler or middleware returned an invalid result")
    return cast(ApplicationResult[object], value)


class ApplicationBus:
    """One async, transport-neutral execution boundary for commands and queries."""

    def __init__(
        self,
        registry: HandlerRegistry,
        *,
        middleware: Iterable[Middleware] = (),
    ) -> None:
        if not isinstance(registry, HandlerRegistry):
            raise InvalidDispatchContract("application bus registry is invalid")
        normalized = tuple(middleware)
        if len(normalized) > 256 or any(not callable(item) for item in normalized):
            raise InvalidDispatchContract("application middleware is invalid")
        registry.freeze()
        self._registry = registry
        self._middleware = normalized

    @property
    def registry(self) -> HandlerRegistry:
        return self._registry

    async def dispatch(
        self,
        message: object,
        *,
        context: ExecutionContext,
    ) -> ApplicationResult[object]:
        if not isinstance(context, ExecutionContext):
            raise InvalidDispatchContract("dispatch context is invalid")
        kind, handler = self._registry.resolve(message)
        if context.cancellation.cancelled:
            raise asyncio.CancelledError
        request = DispatchRequest(cast(Message, message), kind, context)

        async def invoke_handler(current: DispatchRequest) -> ApplicationResult[object]:
            self._same_dispatch(request, current)
            if current.context.cancellation.cancelled:
                raise asyncio.CancelledError
            return await _result(handler(current.message, current.context))

        call_next = invoke_handler
        for middleware in reversed(self._middleware):
            downstream = call_next

            async def invoke_middleware(
                current: DispatchRequest,
                *,
                active: Middleware = middleware,
                following: CallNext = downstream,
            ) -> ApplicationResult[object]:
                self._same_dispatch(request, current)
                called = False

                async def call_once(next_request: DispatchRequest) -> ApplicationResult[object]:
                    nonlocal called
                    if called:
                        raise InvalidDispatchContract(
                            "application middleware called next more than once"
                        )
                    called = True
                    self._same_dispatch(current, next_request)
                    return await following(next_request)

                return await _result(active(current, call_once))

            call_next = invoke_middleware

        with use_context(context):
            return await call_next(request)

    @staticmethod
    def _same_dispatch(original: DispatchRequest, current: DispatchRequest) -> None:
        if current is not original:
            raise InvalidDispatchContract("middleware cannot change dispatch identity")


class SanitizedFailureMiddleware:
    """Project unexpected application failures without catching cancellation."""

    async def __call__(
        self,
        request: DispatchRequest,
        call_next: CallNext,
    ) -> ApplicationResult[object]:
        try:
            return await call_next(request)
        except PlatformError:
            raise
        except Exception as error:
            raise PlatformError(
                PlatformReason.PERMANENT_FAILURE,
                stage="application.handler",
                protected_cause=error,
            ) from error


AuthorizationResolver = Callable[
    [Message, ExecutionContext],
    AuthorizationRequest | None,
]


class GateMiddleware:
    """Authorize a message before its handler can observe protected resources."""

    def __init__(
        self,
        gate: Gate,
        *,
        authorize: AuthorizationResolver,
    ) -> None:
        if not isinstance(gate, Gate) or not callable(authorize):
            raise InvalidDispatchContract("gate middleware configuration is invalid")
        self._gate = gate
        self._authorize = authorize

    async def __call__(
        self,
        request: DispatchRequest,
        call_next: CallNext,
    ) -> ApplicationResult[object]:
        try:
            authorization = self._authorize(request.message, request.context)
        except PlatformError:
            raise
        except Exception as error:
            raise PlatformError(
                PlatformReason.AUTHORIZATION_DENIED,
                stage="application.authorization-request",
                protected_cause=error,
            ) from error
        if authorization is None:
            return await call_next(request)
        if not isinstance(authorization, AuthorizationRequest):
            raise InvalidDispatchContract("authorization resolver returned an invalid request")
        decision = self._gate.decide(authorization, request.context)
        if decision.effect is GateEffect.DENY:
            raise PlatformError(
                PlatformReason.AUTHORIZATION_DENIED,
                stage="application.gate",
                protected_cause=PermissionError("gate denied dispatch"),
            )
        result = await call_next(request)
        if result.gate_decision is not None and result.gate_decision != decision:
            raise InvalidDispatchContract("handler result conflicts with gate decision")
        return replace(result, gate_decision=decision)
