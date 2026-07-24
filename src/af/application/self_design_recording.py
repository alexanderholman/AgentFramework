"""Application controller for explicit, locally governed self-design recording."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from af.framework.self_design_operation import PreparedDesignPlan, SelfDesignOperation
from af.framework.self_design_recording import (
    DesignPlanRecorder,
    DesignRecordAuthority,
    DesignRecordResult,
    DesignRecordingRequest,
)
from af.framework.version_control import VersionControlInitialization


class SelfDesignRecordingController:
    """Prepare read-only plans and record only an explicitly authorized exact plan."""

    __slots__ = ("_clock", "_operation", "_recorder")

    def __init__(
        self,
        operation: SelfDesignOperation,
        recorder: DesignPlanRecorder,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(operation, SelfDesignOperation):
            raise TypeError("self-design operation is invalid")
        if not callable(getattr(recorder, "record", None)):
            raise TypeError("self-design recorder is invalid")
        if not callable(clock):
            raise TypeError("self-design recording clock is invalid")
        self._operation = operation
        self._recorder = recorder
        self._clock = clock

    def prepare(self) -> PreparedDesignPlan:
        """Capture and plan once without performing any write."""
        return self._operation.prepare()

    def record(
        self,
        prepared: PreparedDesignPlan,
        authority: DesignRecordAuthority,
        coordination: VersionControlInitialization,
    ) -> DesignRecordResult:
        """Record one previously prepared plan after exact authority validation."""
        observed_at = self._clock()
        if not isinstance(observed_at, datetime):
            raise TypeError("self-design recording clock returned an invalid time")
        return self._recorder.record(
            DesignRecordingRequest(
                prepared=prepared,
                authority=authority,
                coordination=coordination,
                observed_at=observed_at,
            )
        )
