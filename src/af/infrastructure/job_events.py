"""SQLAlchemy persistence for reconnectable job progress events."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from af.framework.jobs import JobEvent, JobState
from af.infrastructure.database import Database, JobEventRow

__all__ = ("SqlAlchemyJobEventRepository",)


class SqlAlchemyJobEventRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def append(
        self,
        job_id: str,
        state: JobState,
        *,
        stage: str | None = None,
        progress: float | None = None,
        detail: str | None = None,
    ) -> JobEvent:
        now = datetime.now(UTC)
        with self._database.sessions.begin() as session:
            sequence = session.scalar(
                select(func.max(JobEventRow.sequence)).where(JobEventRow.job_id == job_id)
            )
            event = JobEvent(
                job_id=job_id,
                sequence=(sequence or 0) + 1,
                state=state,
                stage=stage,
                progress=progress,
                detail=detail,
                created_at=now,
            )
            session.add(
                JobEventRow(
                    job_id=event.job_id,
                    sequence=event.sequence,
                    state=event.state.value,
                    stage=event.stage,
                    progress=event.progress,
                    detail=event.detail,
                    created_at=event.created_at,
                )
            )
        return event

    def list_after(self, job_id: str, *, sequence: int = 0, limit: int = 100) -> list[JobEvent]:
        if type(sequence) is not int or sequence < 0:
            raise ValueError("sequence must be non-negative")
        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise ValueError("limit must be between one and one thousand")
        with self._database.sessions() as session:
            rows = session.scalars(
                select(JobEventRow)
                .where(JobEventRow.job_id == job_id, JobEventRow.sequence > sequence)
                .order_by(JobEventRow.sequence)
                .limit(limit)
            ).all()
            return [
                JobEvent(
                    job_id=row.job_id,
                    sequence=row.sequence,
                    state=JobState(row.state),
                    stage=row.stage,
                    progress=row.progress,
                    detail=row.detail,
                    created_at=(
                        row.created_at.replace(tzinfo=UTC)
                        if row.created_at.tzinfo is None
                        else row.created_at
                    ),
                )
                for row in rows
            ]
