"""Authenticated SQLAlchemy workflow checkpoints with CAS and append-only history.

The authentication key is host configuration, never stored with checkpoints. The
current row is a cursor; the history's latest revision prevents row-only rollback.
Whole-database rollback requires an external trusted monotonic backup manifest.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import Integer, String, Text, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from af.framework.workflows import WorkflowConflict, WorkflowError, WorkflowRun
from af.infrastructure.database import Base, Database


class WorkflowRunRow(Base):
    __tablename__ = "af_workflow_runs"
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(String(64))


class WorkflowHistoryRow(Base):
    __tablename__ = "af_workflow_history"
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(String(64))


class SqlAlchemyWorkflowRepository:
    def __init__(self, database: Database, *, authentication_key: bytes) -> None:
        if not isinstance(authentication_key, bytes) or len(authentication_key) < 32:
            raise ValueError("workflow authentication key must be at least 32 bytes")
        self._database = database
        self._key = authentication_key
        Base.metadata.create_all(database.engine)

    def _signature(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def _decode(self, payload: str, signature: str, run_id: str, revision: int) -> WorkflowRun:
        if not hmac.compare_digest(signature, self._signature(payload)):
            raise WorkflowError("checkpoint authentication failed; restore trusted backup")
        try:
            run = WorkflowRun.model_validate_json(payload)
        except ValueError as exc:
            raise WorkflowError("checkpoint schema is incompatible") from exc
        if run.run_id != run_id or run.revision != revision:
            raise WorkflowError("checkpoint identity mismatch")
        return run

    def create(self, run: WorkflowRun) -> WorkflowRun:
        if run.revision != 0:
            raise WorkflowError("new workflow must start at revision zero")
        payload = run.model_dump_json()
        signature = self._signature(payload)
        try:
            with self._database.sessions.begin() as session:
                session.add(
                    WorkflowRunRow(
                        run_id=run.run_id, revision=0, payload=payload, signature=signature
                    )
                )
                session.add(
                    WorkflowHistoryRow(
                        run_id=run.run_id, revision=0, payload=payload, signature=signature
                    )
                )
        except IntegrityError as exc:
            raise WorkflowConflict("workflow already exists") from exc
        return run

    def get(self, run_id: str) -> WorkflowRun:
        with self._database.sessions() as session:
            row = session.get(WorkflowRunRow, run_id)
            if row is None:
                raise WorkflowError("workflow not found")
            latest = session.scalar(
                select(func.max(WorkflowHistoryRow.revision)).where(
                    WorkflowHistoryRow.run_id == run_id
                )
            )
            if latest != row.revision:
                raise WorkflowError("checkpoint revision rollback detected")
            return self._decode(row.payload, row.signature, row.run_id, row.revision)

    def save(self, run: WorkflowRun, *, expected_revision: int) -> WorkflowRun:
        if run.revision != expected_revision:
            raise WorkflowConflict("checkpoint revision mismatch")
        saved = run.model_copy(
            update={"revision": expected_revision + 1, "updated_at": datetime.now(UTC)}
        )
        payload = saved.model_dump_json()
        signature = self._signature(payload)
        with self._database.sessions.begin() as session:
            result = session.execute(
                update(WorkflowRunRow)
                .where(
                    WorkflowRunRow.run_id == run.run_id,
                    WorkflowRunRow.revision == expected_revision,
                )
                .values(revision=saved.revision, payload=payload, signature=signature)
            )
            if cast(CursorResult[tuple[object, ...]], result).rowcount != 1:
                raise WorkflowConflict("workflow advanced; stale claim rejected")
            session.add(
                WorkflowHistoryRow(
                    run_id=run.run_id, revision=saved.revision, payload=payload, signature=signature
                )
            )
        return saved

    def history(self, run_id: str) -> tuple[WorkflowRun, ...]:
        with self._database.sessions() as session:
            rows = session.scalars(
                select(WorkflowHistoryRow)
                .where(WorkflowHistoryRow.run_id == run_id)
                .order_by(WorkflowHistoryRow.revision)
            )
            return tuple(
                self._decode(row.payload, row.signature, row.run_id, row.revision) for row in rows
            )
