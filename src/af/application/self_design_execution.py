"""Application controller for one explicit governed self-design execution cycle."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from af.application.self_design_publication import SelfDesignPublicationController
from af.application.self_design_recording import SelfDesignRecordingController
from af.framework.errors import AFError
from af.framework.self_design import DesignPlan, DesignRoute
from af.framework.self_design_operation import PreparedDesignPlan
from af.framework.self_design_publication import (
    DesignPublicationAuthority,
    DesignPublicationDraft,
    DesignPublicationReceipt,
)
from af.framework.self_design_recording import (
    DesignRecordAuthority,
    DesignRecordingEffect,
    DesignRecordResult,
)
from af.framework.version_control import VersionControlInitialization


class SelfDesignExecutionError(AFError):
    """A sanitized fail-closed self-design execution error."""


@dataclass(frozen=True, slots=True)
class SelfDesignExecutionResult:
    """One exact self-design execution outcome across plan, record, and publication."""

    plan: DesignPlan
    record: DesignRecordResult
    publication: DesignPublicationReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.plan, DesignPlan):
            raise TypeError("self-design execution plan is invalid")
        if not isinstance(self.record, DesignRecordResult):
            raise TypeError("self-design execution record is invalid")
        if not isinstance(self.publication, DesignPublicationReceipt):
            raise TypeError("self-design execution publication is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "plan": json.loads(self.plan.canonical_json()),
            "publication": self.publication.to_dict(),
            "record": self.record.to_dict(),
        }


class SelfDesignExecutor(Protocol):
    """Execute one explicitly configured governed self-design cycle."""

    def preview(self) -> DesignPlan:
        """Return the exact dry-run plan for one configured self-design cycle."""
        ...

    def execute(self) -> SelfDesignExecutionResult:
        """Return the exact plan, local record, and publication receipt."""
        ...


class SelfDesignCoordinationLoader(Protocol):
    """Load existing repository coordination state without broadening authority."""

    def load(self) -> VersionControlInitialization:
        """Return the exact coordination state for one execution cycle."""
        ...


class SelfDesignDraftBuilder(Protocol):
    """Build one exact publication draft for a proposed design plan."""

    def build(self, prepared: PreparedDesignPlan) -> DesignPublicationDraft:
        """Return the exact admitted publication draft for one prepared plan."""
        ...


class GovernedSelfDesignExecutor:
    """Prepare, record, and publish one explicit self-design cycle."""

    __slots__ = (
        "_authority_ttl_seconds",
        "_clock",
        "_coordination",
        "_draft_builder",
        "_publication",
        "_recording",
    )

    def __init__(
        self,
        recording: SelfDesignRecordingController,
        coordination: SelfDesignCoordinationLoader,
        draft_builder: SelfDesignDraftBuilder,
        publication: SelfDesignPublicationController,
        clock: Callable[[], datetime],
        *,
        authority_ttl_seconds: int = 300,
    ) -> None:
        if not isinstance(recording, SelfDesignRecordingController):
            raise TypeError("self-design recording controller is invalid")
        if not callable(getattr(coordination, "load", None)):
            raise TypeError("self-design coordination loader is invalid")
        if not callable(getattr(draft_builder, "build", None)):
            raise TypeError("self-design draft builder is invalid")
        if not isinstance(publication, SelfDesignPublicationController):
            raise TypeError("self-design publication controller is invalid")
        if not callable(clock):
            raise TypeError("self-design execution clock is invalid")
        if type(authority_ttl_seconds) is not int or not 1 <= authority_ttl_seconds <= 3600:
            raise ValueError("self-design execution authority lifetime is invalid")
        self._recording = recording
        self._coordination = coordination
        self._draft_builder = draft_builder
        self._publication = publication
        self._clock = clock
        self._authority_ttl_seconds = authority_ttl_seconds

    def execute(self) -> SelfDesignExecutionResult:
        """Run one explicit self-design cycle through exact local and external authority."""
        prepared = self._recording.prepare()
        if prepared.plan.route is not DesignRoute.PROPOSE:
            raise SelfDesignExecutionError("self-design execution requires a proposed design unit")
        coordination = self._coordination.load()
        if not isinstance(coordination, VersionControlInitialization):
            raise TypeError("self-design coordination loader returned an invalid state")
        draft = self._draft_builder.build(prepared)
        if not isinstance(draft, DesignPublicationDraft):
            raise TypeError("self-design draft builder returned an invalid draft")
        issued_at = self._clock()
        if not isinstance(issued_at, datetime):
            raise TypeError("self-design execution clock returned an invalid time")
        expires_at = issued_at + timedelta(seconds=self._authority_ttl_seconds)
        record_authority = DesignRecordAuthority(
            authority_id=self._authority_id("record", prepared.plan.digest),
            repository=prepared.snapshot.repository,
            base_ref=prepared.snapshot.base_ref,
            head_sha=prepared.snapshot.head_sha,
            snapshot_digest=prepared.snapshot.digest,
            plan_digest=prepared.plan.digest,
            version_control_manifest_digest=coordination.manifest.digest(),
            issued_at=issued_at,
            expires_at=expires_at,
            allowed_effects=(DesignRecordingEffect.LOCAL_RECORD,),
        )
        recorded = self._recording.record(prepared, record_authority, coordination)
        publication_authority = DesignPublicationAuthority(
            authority_id=self._authority_id("publish", draft.digest),
            repository=prepared.snapshot.repository,
            base_ref=prepared.snapshot.base_ref,
            head_sha=prepared.snapshot.head_sha,
            snapshot_digest=prepared.snapshot.digest,
            plan_digest=prepared.plan.digest,
            record_digest=recorded.record.digest,
            record_path=recorded.record_path,
            draft_digest=draft.digest,
            issued_at=issued_at,
            expires_at=expires_at,
            allowed_effects=(draft.effect,),
        )
        publication = self._publication.publish(
            prepared,
            recorded,
            draft,
            publication_authority,
        )
        return SelfDesignExecutionResult(
            plan=prepared.plan,
            record=recorded,
            publication=publication,
        )

    def preview(self) -> DesignPlan:
        """Return the exact dry-run plan without issuing any write authority."""
        return self._recording.prepare().plan

    @staticmethod
    def _authority_id(kind: str, digest: str) -> str:
        return f"self-design.{kind}:{digest.removeprefix('sha256:')[:16]}"


class SelfDesignExecutionController:
    """Invoke one admitted self-design executor through the application boundary."""

    __slots__ = ("_executor",)

    def __init__(self, executor: SelfDesignExecutor) -> None:
        if not callable(getattr(executor, "preview", None)) or not callable(
            getattr(executor, "execute", None)
        ):
            raise TypeError("self-design executor is invalid")
        self._executor = executor

    def execute(self) -> SelfDesignExecutionResult:
        """Run one explicitly admitted self-design cycle."""
        result = self._executor.execute()
        if not isinstance(result, SelfDesignExecutionResult):
            raise TypeError("self-design executor returned an invalid result")
        return result

    def preview(self) -> DesignPlan:
        """Return the exact dry-run plan for the configured execution boundary."""
        plan = self._executor.preview()
        if not isinstance(plan, DesignPlan):
            raise TypeError("self-design executor returned an invalid plan")
        return plan
