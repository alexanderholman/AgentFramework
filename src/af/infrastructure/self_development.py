"""Host-configured finite development source and authenticated local cycle ledger."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sqlalchemy import Integer, String, Table, Text, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from af.application.engineering import LocalEngineeringService
from af.application.self_development import (
    LocalEngineeringDevelopmentExecutor,
    SelfDevelopmentController,
)
from af.framework.context import ExecutionContext
from af.framework.self_development import (
    DevelopmentSnapshot,
    SelectionPolicy,
    WorkCandidate,
    WorkKind,
)
from af.framework.self_development_execution import (
    DevelopmentCompletion,
    DevelopmentPlanningInput,
    LocalDevelopmentUnit,
    PreparedDevelopment,
)
from af.framework.workflows import WorkflowError
from af.infrastructure.database import Base, Database
from af.infrastructure.local_engineering import GitTemplateEngineer
from af.infrastructure.programs import AsyncSubprocessExecutor


class DevelopmentEventRow(Base):
    __tablename__ = "af_development_events"
    __table_args__ = (UniqueConstraint("plan_digest", "kind"),)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_digest: Mapped[str] = mapped_column(String(71), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    payload: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(String(64))


class SqlAlchemyDevelopmentLedger:
    def __init__(self, database: Database, authentication_key: bytes) -> None:
        if len(authentication_key) < 32:
            raise ValueError("development ledger key must be at least 32 bytes")
        self._database, self._key = database, authentication_key
        cast(Table, DevelopmentEventRow.__table__).create(database.engine, checkfirst=True)

    def _signature(self, digest: str, kind: str, payload: str) -> str:
        return hmac.new(
            self._key, f"{digest}:{kind}:{payload}".encode(), hashlib.sha256
        ).hexdigest()

    def _append(self, digest: str, kind: str, payload: str) -> bool:
        try:
            with self._database.sessions.begin() as session:
                session.add(
                    DevelopmentEventRow(
                        plan_digest=digest,
                        kind=kind,
                        payload=payload,
                        signature=self._signature(digest, kind, payload),
                    )
                )
            return True
        except IntegrityError:
            return False

    def _events(self, digest: str) -> dict[str, str]:
        with self._database.sessions() as session:
            rows = tuple(
                session.scalars(
                    select(DevelopmentEventRow)
                    .where(DevelopmentEventRow.plan_digest == digest)
                    .order_by(DevelopmentEventRow.sequence)
                    .limit(4)
                )
            )
            if tuple(row.kind for row in rows) not in {
                (),
                ("prepared",),
                ("prepared", "claimed"),
                ("prepared", "claimed", "completed"),
            }:
                raise WorkflowError("development ledger event order is invalid")
            for row in rows:
                if not hmac.compare_digest(
                    row.signature, self._signature(digest, row.kind, row.payload)
                ):
                    raise WorkflowError("development ledger authentication failed")
            return {row.kind: row.payload for row in rows}

    def save(self, prepared: PreparedDevelopment) -> None:
        if (
            not self._append(prepared.plan_digest, "prepared", prepared.model_dump_json())
            and self.load(prepared.plan_digest) != prepared
        ):
            raise WorkflowError("development plan identity conflict")

    def load(self, digest: str) -> PreparedDevelopment:
        payload = self._events(digest).get("prepared")
        if payload is None:
            raise ValueError("development plan is unknown")
        prepared = PreparedDevelopment.model_validate_json(payload)
        if prepared.plan_digest != digest:
            raise WorkflowError("development ledger identity mismatch")
        return prepared

    def completed(self, digest: str) -> DevelopmentCompletion | None:
        payload = self._events(digest).get("completed")
        if payload is None:
            return None
        result = DevelopmentCompletion.model_validate_json(payload)
        if result.plan_digest != digest:
            raise WorkflowError("development result identity mismatch")
        return result

    def claim(self, digest: str) -> bool:
        self.load(digest)
        return self._append(digest, "claimed", "{}")

    def finish(self, completion: DevelopmentCompletion) -> None:
        if "claimed" not in self._events(completion.plan_digest):
            raise WorkflowError("development completion has no claim")
        if not self._append(completion.plan_digest, "completed", completion.model_dump_json()):
            raise WorkflowError("development completion already exists")


class LocalDevelopmentSource:
    def __init__(self, repository: Path, unit_path: Path) -> None:
        self._observer = GitTemplateEngineer(repository, AsyncSubprocessExecutor())
        self._unit_path = unit_path

    def capture(self, context: ExecutionContext) -> DevelopmentPlanningInput:
        if self._unit_path.is_symlink():
            raise ValueError("development unit must be a regular host configuration file")
        with self._unit_path.open("rb") as stream:
            data = stream.read(262_145)
        if len(data) > 262_144:
            raise ValueError("development unit exceeds its input bound")
        unit = LocalDevelopmentUnit.model_validate_json(data)
        head, branch, dirty = self._observer.observe(context)
        now = datetime.now(UTC)
        return DevelopmentPlanningInput(
            snapshot=DevelopmentSnapshot(
                source_id="configured-local-git",
                repository=unit.repository,
                base_ref=branch,
                head_sha=head,
                captured_at=now,
                candidates=(
                    WorkCandidate(
                        item_id=unit.candidate_id,
                        revision=unit.digest,
                        kind=WorkKind.IMPLEMENTATION,
                        priority=1,
                        blocking_impact=0,
                        risk=1,
                        created_at=unit.issued_at,
                        procedure_id=None,
                        dependencies_ready=True,
                        active_overlap=dirty,
                        scope_clear=True,
                        authority_granted=unit.issued_at <= now < unit.expires_at,
                        policy_allowed=head == unit.expected_head,
                        evidence_passed=True,
                        complete=False,
                    ),
                ),
            ),
            policy=SelectionPolicy(
                300,
                False,
                (
                    "test_first",
                    "tests",
                    "lint",
                    "format",
                    "typing",
                    "coverage",
                    "security",
                    "compatibility",
                    "governance",
                    "independent_review",
                ),
            ),
            unit=unit,
        )


def create_local_development_controller(
    database: Database,
    repository: Path,
    unit_path: Path,
    *,
    authentication_key: bytes,
    engineering: LocalEngineeringService,
) -> SelfDevelopmentController:
    return SelfDevelopmentController(
        source=LocalDevelopmentSource(repository, unit_path),
        executor=LocalEngineeringDevelopmentExecutor(engineering),
        ledger=SqlAlchemyDevelopmentLedger(database, authentication_key),
    )
