"""SQLAlchemy 2 database setup for the reference SQLite adapter."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    String,
    Text,
    create_engine,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    __tablename__ = "af_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    prompt: Mapped[str] = mapped_column(Text)
    output: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(String(128))
    route: Mapped[str] = mapped_column(String(128))
    tier: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    verified: Mapped[bool] = mapped_column(Boolean)
    reused: Mapped[bool] = mapped_column(Boolean)
    trace_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TraceRow(Base):
    __tablename__ = "af_run_traces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    prompt: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(128), nullable=True)
    arguments_json: Mapped[str] = mapped_column(Text)
    route: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean)
    reused: Mapped[bool] = mapped_column(Boolean)
    regression_passed: Mapped[bool] = mapped_column(Boolean)
    promoted: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(32))
    events_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class JobEventRow(Base):
    __tablename__ = "af_job_events"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    progress: Mapped[float | None] = mapped_column(nullable=True)
    detail: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class WorkerMeasurementReplayRow(Base):
    __tablename__ = "af_worker_measurement_replays"

    issuer: Mapped[str] = mapped_column(String(127), primary_key=True)
    evidence_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProgramJobExecutionRow(Base):
    __tablename__ = "af_program_job_executions"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProgramJobResultRow(Base):
    __tablename__ = "af_program_job_results"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state: Mapped[str] = mapped_column(String(32))
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout: Mapped[bytes] = mapped_column(LargeBinary)
    stderr: Mapped[bytes] = mapped_column(LargeBinary)
    elapsed_seconds: Mapped[float] = mapped_column(Float)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Database:
    def __init__(self, url: str) -> None:
        parsed = make_url(url)
        engine_options: dict[str, object] = {}
        if parsed.get_backend_name() == "sqlite":
            engine_options["connect_args"] = {"check_same_thread": False}
            database_name = parsed.database
            if database_name in (None, "", ":memory:"):
                engine_options["poolclass"] = StaticPool
            elif database_name:
                Path(database_name).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

        self.engine: Engine = create_engine(url, **engine_options)
        self.sessions: sessionmaker[Session] = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
        )
        if parsed.get_backend_name() == "sqlite":
            self._migrate_reference_schema()
        else:
            Base.metadata.create_all(self.engine)

    def _migrate_reference_schema(self) -> None:
        from af.framework.migrations import MigrationRunner
        from af.infrastructure.reference_schema import reference_migrations
        from af.infrastructure.sqlite import SQLiteDriver

        raw = self.engine.raw_connection()
        try:
            connection = raw.driver_connection
            if not isinstance(connection, sqlite3.Connection):
                raise TypeError("reference SQLite schema requires a SQLite connection")
            with SQLiteDriver(":memory:", connection=connection) as driver:
                runner = MigrationRunner(
                    driver,
                    reference_migrations(),
                    owner_id=f"reference-{uuid4().hex}",
                    clock=lambda: datetime.now(UTC),
                )
                runner.bootstrap()
                runner.apply_up()
                runner.verify()
        except BaseException:
            self.engine.dispose()
            raise
        finally:
            raw.close()

    def verify_schema(self) -> None:
        if self.engine.url.get_backend_name() != "sqlite":
            raise ValueError("reference schema verification requires SQLite")
        self._migrate_reference_schema()

    def close(self) -> None:
        self.engine.dispose()
