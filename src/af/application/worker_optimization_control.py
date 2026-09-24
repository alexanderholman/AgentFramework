"""Authorized promotion of measured worker-optimization selections."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from af.application.bus import AuthorizationRequest, Gate, GateDecision, GateEffect
from af.application.worker_qualification import (
    WorkerOptimizationCandidate,
    WorkerOptimizationPriorities,
    WorkerOptimizationSelection,
    WorkerOptimizationSelector,
)
from af.framework.context import AcceptanceScope, ExecutionContext
from af.framework.errors import InvalidProviderConfiguration, PlatformError, PlatformReason

__all__ = (
    "WorkerOptimizationControlService",
    "WorkerOptimizationPromotion",
    "WorkerOptimizationRecorder",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@runtime_checkable
class WorkerOptimizationRecorder(Protocol):
    """Persist one exact authorized selection and its objective policy."""

    def record(
        self,
        selection: WorkerOptimizationSelection,
        *,
        priorities: WorkerOptimizationPriorities,
        now: datetime,
    ) -> object: ...


@dataclass(frozen=True, slots=True, repr=False)
class WorkerOptimizationPromotion:
    """Authorized evidence that one exact selection reached its recorder."""

    selection: WorkerOptimizationSelection
    authorization: GateDecision

    def __post_init__(self) -> None:
        if not isinstance(self.selection, WorkerOptimizationSelection):
            raise TypeError("worker optimization promotion selection is invalid")
        if not isinstance(self.authorization, GateDecision):
            raise TypeError("worker optimization promotion authorization is invalid")
        if self.authorization.effect is not GateEffect.ALLOW:
            raise ValueError("worker optimization promotion requires allowing authorization")

    def __repr__(self) -> str:
        return (
            "WorkerOptimizationPromotion(selection='[REDACTED]', "
            f"candidate_count={self.selection.candidate_count!r}, "
            f"authorization={self.authorization!r})"
        )


def _current_utc(clock: Callable[[], datetime]) -> datetime:
    current = clock()
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise InvalidProviderConfiguration("worker optimization control time is invalid")
    return current.astimezone(UTC)


def _validate_target(driver_name: object, environment_digest: object) -> tuple[str, str]:
    if type(driver_name) is not str or _IDENTIFIER.fullmatch(driver_name) is None:
        raise InvalidProviderConfiguration("worker optimization control driver is invalid")
    if type(environment_digest) is not str or _DIGEST.fullmatch(environment_digest) is None:
        raise InvalidProviderConfiguration("worker optimization control environment is invalid")
    return driver_name, environment_digest


def _sync_result(result: object) -> None:
    if inspect.iscoroutine(result):
        result.close()
        raise TypeError("worker optimization recorder returned an asynchronous result")
    if inspect.isawaitable(result) or inspect.isasyncgen(result):
        raise TypeError("worker optimization recorder returned an asynchronous result")
    if result is not None:
        raise TypeError("worker optimization recorder must return None")


class WorkerOptimizationControlService:
    """Authorize an exact target before selecting and recording its worker profile."""

    def __init__(
        self,
        recorder: WorkerOptimizationRecorder,
        *,
        gate: Gate,
        selector: WorkerOptimizationSelector | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(recorder, WorkerOptimizationRecorder):
            raise TypeError("worker optimization recorder is invalid")
        if not isinstance(gate, Gate):
            raise TypeError("worker optimization authorization gate is invalid")
        resolved_selector = WorkerOptimizationSelector() if selector is None else selector
        if not isinstance(resolved_selector, WorkerOptimizationSelector):
            raise TypeError("worker optimization selector is invalid")
        if not callable(clock):
            raise TypeError("worker optimization control clock must be callable")
        self._recorder = recorder
        self._gate = gate
        self._selector = resolved_selector
        self._clock = clock

    def select_and_record(
        self,
        candidates: Iterable[WorkerOptimizationCandidate],
        *,
        priorities: WorkerOptimizationPriorities,
        context: ExecutionContext,
        driver_name: str,
        environment_digest: str,
    ) -> WorkerOptimizationPromotion:
        """Promote one measured selection only after exact-target operator authorization."""

        target_driver, target_environment = _validate_target(driver_name, environment_digest)
        authorization = self._gate.decide(
            AuthorizationRequest(
                action="promote",
                resource_type="worker-optimization",
                protected_resource_id=f"{target_driver}:{target_environment}",
                required_scope=AcceptanceScope.OPERATOR_EVALUATION,
            ),
            context,
        )
        if authorization.effect is GateEffect.DENY:
            raise PlatformError(
                PlatformReason.AUTHORIZATION_DENIED,
                stage="worker.optimization.authorization",
                protected_cause=PermissionError("worker optimization promotion denied"),
            )

        current = _current_utc(self._clock)
        selection = self._selector.select(candidates, priorities=priorities, now=current)
        qualification = selection.qualification
        if (
            qualification.driver_name != target_driver
            or qualification.environment_digest != target_environment
        ):
            raise InvalidProviderConfiguration(
                "worker optimization selection does not match the authorized target"
            )
        result = self._recorder.record(selection, priorities=priorities, now=current)
        _sync_result(result)
        return WorkerOptimizationPromotion(
            selection=selection,
            authorization=authorization,
        )
