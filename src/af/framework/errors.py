"""Domain and framework errors; adapters translate these at their boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class AFError(Exception):
    """Base class for expected AF failures."""


class FailureCategory(StrEnum):
    CONFIGURATION = "configuration"
    RESOLUTION = "resolution"
    CAPABILITY = "capability"
    VALIDATION = "validation"
    AUTHORIZATION = "authorization"
    TRANSPORT = "transport"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    INTEGRITY = "integrity"
    CANCELLATION = "cancellation"
    SHUTDOWN = "shutdown"


class PlatformReason(StrEnum):
    CONFIGURATION_INVALID = "configuration_invalid"
    CONFIGURATION_PROFILE_UNKNOWN = "configuration_profile_unknown"
    CONFIGURATION_SECRET_UNRESOLVED = "configuration_secret_unresolved"
    DEPENDENCY_RESOLUTION_FAILED = "dependency_resolution_failed"
    CONTEXT_UNAVAILABLE = "context_unavailable"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    VALIDATION_FAILED = "validation_failed"
    AUTHORIZATION_DENIED = "authorization_denied"
    TRANSPORT_FAILED = "transport_failed"
    TRANSIENT_FAILURE = "transient_failure"
    PERMANENT_FAILURE = "permanent_failure"
    INTEGRITY_FAILURE = "integrity_failure"
    CANCELLED = "cancelled"
    SHUTDOWN_FAILED = "shutdown_failed"


_REASON_CATEGORIES = {
    PlatformReason.CONFIGURATION_INVALID: FailureCategory.CONFIGURATION,
    PlatformReason.CONFIGURATION_PROFILE_UNKNOWN: FailureCategory.CONFIGURATION,
    PlatformReason.CONFIGURATION_SECRET_UNRESOLVED: FailureCategory.CONFIGURATION,
    PlatformReason.DEPENDENCY_RESOLUTION_FAILED: FailureCategory.RESOLUTION,
    PlatformReason.CONTEXT_UNAVAILABLE: FailureCategory.RESOLUTION,
    PlatformReason.CAPABILITY_UNAVAILABLE: FailureCategory.CAPABILITY,
    PlatformReason.VALIDATION_FAILED: FailureCategory.VALIDATION,
    PlatformReason.AUTHORIZATION_DENIED: FailureCategory.AUTHORIZATION,
    PlatformReason.TRANSPORT_FAILED: FailureCategory.TRANSPORT,
    PlatformReason.TRANSIENT_FAILURE: FailureCategory.TRANSIENT,
    PlatformReason.PERMANENT_FAILURE: FailureCategory.PERMANENT,
    PlatformReason.INTEGRITY_FAILURE: FailureCategory.INTEGRITY,
    PlatformReason.CANCELLED: FailureCategory.CANCELLATION,
    PlatformReason.SHUTDOWN_FAILED: FailureCategory.SHUTDOWN,
}

_PUBLIC_MESSAGES = {
    FailureCategory.CONFIGURATION: "configuration is invalid",
    FailureCategory.RESOLUTION: "required application context is unavailable",
    FailureCategory.CAPABILITY: "required capability is unavailable",
    FailureCategory.VALIDATION: "input validation failed",
    FailureCategory.AUTHORIZATION: "operation is not authorized",
    FailureCategory.TRANSPORT: "transport operation failed",
    FailureCategory.TRANSIENT: "operation temporarily failed",
    FailureCategory.PERMANENT: "operation failed",
    FailureCategory.INTEGRITY: "integrity validation failed",
    FailureCategory.CANCELLATION: "operation was cancelled",
    FailureCategory.SHUTDOWN: "application shutdown failed",
}


def category_for_reason(reason: PlatformReason) -> FailureCategory:
    return _REASON_CATEGORIES[reason]


@dataclass(frozen=True, slots=True)
class PublicError:
    code: str
    message: str


class PlatformError(AFError):
    """Sanitized platform failure with protected evidence for trusted telemetry."""

    def __init__(
        self,
        reason: PlatformReason,
        *,
        stage: str,
        protected_cause: BaseException | None = None,
    ) -> None:
        if not stage or len(stage) > 200 or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", stage):
            raise ValueError("stage must be a bounded non-empty single-line value")
        self.reason = reason
        self.category = category_for_reason(reason)
        self.stage = stage
        self.public_message = _PUBLIC_MESSAGES[self.category]
        self.protected_cause = protected_cause
        super().__init__(self.public_message)

    @property
    def public_error(self) -> PublicError:
        return PublicError(self.reason.value, self.public_message)

    def __repr__(self) -> str:
        return (
            f"PlatformError(reason={self.reason.value!r}, stage={self.stage!r}, "
            f"public_message={self.public_message!r})"
        )


class InvalidCharacter(AFError, ValueError):
    def __init__(self, character: str) -> None:
        super().__init__(f"character must contain exactly one code point, got {character!r}")


class InvalidPrompt(AFError, ValueError):
    def __init__(self, message: str = "prompt must not be blank") -> None:
        super().__init__(message)


class InvalidWorkRequest(AFError, ValueError):
    def __init__(self, message: str = "work request is invalid") -> None:
        super().__init__(message)


class InvalidPagination(AFError, ValueError):
    """Raised when a paginated list request is outside AF's supported bounds."""


class InvalidRoutingPolicy(AFError, ValueError):
    """Raised when a policy does not make an explicit one-or-two goal trade-off."""


class InvalidProviderConfiguration(AFError, ValueError):
    """Raised when an executable provider lacks its corresponding safety boundary."""


class InvalidSelectionProposal(AFError, ValueError):
    """Raised when a selector proposes an unsupported or malformed route."""


class InvalidBinding(AFError):
    """Raised when a dependency-container declaration or lifecycle contract is invalid."""


class ApplicationGraphError(AFError):
    """Raised before registration when the provider graph is invalid."""


class ApplicationRegistrationError(AFError):
    """Sanitized provider-registration failure."""

    def __init__(self, provider_id: str, *, protected_cause: BaseException) -> None:
        self.provider_id = provider_id
        self.protected_cause = protected_cause
        super().__init__("provider registration failed")


class ApplicationBootError(AFError):
    """Sanitized provider-boot failure with protected cleanup evidence."""

    def __init__(
        self,
        provider_id: str,
        *,
        protected_cause: BaseException,
        cleanup_failures: tuple[BaseException, ...] = (),
    ) -> None:
        self.provider_id = provider_id
        self.protected_cause = protected_cause
        self.cleanup_failures = cleanup_failures
        super().__init__("application boot failed")


class ApplicationShutdownError(AFError):
    """Raised after best-effort lifecycle shutdown has completed."""

    def __init__(self, failures: tuple[BaseException, ...]) -> None:
        self.failures = failures
        super().__init__(f"application shutdown failed for {len(failures)} operation(s)")


class AsyncLifecycleRequired(AFError):
    """Raised when a synchronous entry point reaches an asynchronous lifecycle hook."""


def _safe_type_label(value: object) -> str:
    if type(value) is str:
        return "string token"
    candidate = value if isinstance(value, type) else type(value)
    try:
        label = type.__getattribute__(candidate, "__name__")
    except BaseException:
        return "service"
    if type(label) is not str or not label or len(label) > 100 or not label.isidentifier():
        return "service"
    return label


class ServiceNotBound(AFError):
    def __init__(self, token: object, *, tag: bool = False) -> None:
        label = "tag" if tag else _safe_type_label(token)
        super().__init__(f"service is not bound: {label}")


class CircularDependency(AFError):
    def __init__(self, chain: tuple[object, ...]) -> None:
        self.chain = chain
        labels: list[str] = []
        for item in chain:
            try:
                token = object.__getattribute__(item, "token")
            except BaseException:
                token = item
            labels.append(_safe_type_label(token))
        super().__init__(f"circular dependency: {' -> '.join(labels)}")


class ScopeDisposed(AFError):
    """Raised when resolution is attempted through a closing or disposed scope."""


class ScopeUnavailable(AFError):
    """Raised when a binding is resolved outside its declared lifetime scope."""


class AsyncDisposalRequired(AFError):
    """Raised when synchronous cleanup would leave asynchronous resources behind."""


class AsyncResolutionRequired(AFError):
    """Raised when a synchronous caller reaches an asynchronous factory."""


class ShutdownTimeout(AFError):
    """Protected evidence that shutdown exceeded its configured total budget."""


class ScopeDisposalError(AFError):
    def __init__(self, failures: tuple[BaseException, ...]) -> None:
        self.failures = failures
        super().__init__(f"scope disposal failed for {len(failures)} resource(s)")


class UnsupportedWork(AFError):
    def __init__(self, prompt: str, trace_id: str | None = None) -> None:
        self.prompt = prompt
        self.trace_id = trace_id
        super().__init__(f"no route found for prompt: {prompt!r}")


class VerificationFailed(AFError):
    def __init__(self, trace_id: str, reason: str) -> None:
        self.trace_id = trace_id
        self.reason = reason
        super().__init__(f"verification failed ({trace_id}): {reason}")


class TaskNotFound(AFError):
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"task not found: {task_id}")


class TraceNotFound(AFError):
    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        super().__init__(f"trace not found: {trace_id}")
