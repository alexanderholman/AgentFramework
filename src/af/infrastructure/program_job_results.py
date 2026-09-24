"""SQLAlchemy persistence for host program job outputs."""

from __future__ import annotations

from datetime import UTC

from sqlalchemy import select

from af.application.program_jobs import ProgramJobResult
from af.framework.programs import ProgramResult, ProgramState
from af.infrastructure.database import Database, ProgramJobResultRow

__all__ = ("SqlAlchemyProgramJobResultRepository",)


class SqlAlchemyProgramJobResultRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def save(self, value: ProgramJobResult) -> None:
        result = value.result
        with self._database.sessions.begin() as session:
            row = session.get(ProgramJobResultRow, value.job_id)
            if row is None:
                row = ProgramJobResultRow(job_id=value.job_id)
                session.add(row)
            row.state = result.state.value
            row.exit_code = result.exit_code
            row.stdout = result.stdout
            row.stderr = result.stderr
            row.elapsed_seconds = result.elapsed.total_seconds()
            row.finished_at = value.finished_at

    def get(self, job_id: str) -> ProgramJobResult | None:
        with self._database.sessions() as session:
            row = session.scalar(
                select(ProgramJobResultRow).where(ProgramJobResultRow.job_id == job_id)
            )
            if row is None:
                return None
            finished_at = (
                row.finished_at.replace(tzinfo=UTC)
                if row.finished_at.tzinfo is None
                else row.finished_at
            )
            from datetime import timedelta

            return ProgramJobResult(
                job_id=row.job_id,
                result=ProgramResult(
                    state=ProgramState(row.state),
                    exit_code=row.exit_code,
                    stdout=row.stdout,
                    stderr=row.stderr,
                    elapsed=timedelta(seconds=row.elapsed_seconds),
                ),
                finished_at=finished_at,
            )
