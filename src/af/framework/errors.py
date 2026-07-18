"""Domain and framework errors; adapters translate these at their boundaries."""

from __future__ import annotations


class AFError(Exception):
    """Base class for expected AF failures."""


class InvalidCharacter(AFError, ValueError):
    def __init__(self, character: str) -> None:
        super().__init__(f"character must contain exactly one code point, got {character!r}")


class InvalidPrompt(AFError, ValueError):
    def __init__(self, message: str = "prompt must not be blank") -> None:
        super().__init__(message)


class InvalidRoutingPolicy(AFError, ValueError):
    """Raised when a policy does not make an explicit one-or-two goal trade-off."""


class InvalidProviderConfiguration(AFError, ValueError):
    """Raised when an executable provider lacks its corresponding safety boundary."""


class InvalidSelectionProposal(AFError, ValueError):
    """Raised when a selector proposes an unsupported or malformed route."""


class InvalidBinding(AFError):
    """Raised when a dependency binding would be replaced implicitly."""


class ServiceNotBound(AFError):
    def __init__(self, token: object) -> None:
        super().__init__(f"service is not bound: {token!r}")


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
