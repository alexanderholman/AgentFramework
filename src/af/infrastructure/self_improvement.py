"""Authenticated append-only self-improvement ledger and fenced stage claims."""

from __future__ import annotations

import hashlib
import hmac
from typing import cast

from sqlalchemy import Integer, String, Text, UniqueConstraint, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from af.framework.self_improvement import ImprovementRecord, StageDispatch, StageEvaluation
from af.framework.workflows import WorkflowConflict, WorkflowError
from af.infrastructure.database import Base, Database


class ImprovementOperationRow(Base):
    __tablename__ = "af_improvement_operations"
    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    active_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ImprovementLedgerRow(Base):
    __tablename__ = "af_improvement_ledger"
    __table_args__ = (UniqueConstraint("effect_key", "kind"),)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String(128), index=True)
    effect_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    payload: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(String(64))


class SqlAlchemySelfImprovementLedger:
    def __init__(self, database: Database, *, authentication_key: bytes) -> None:
        if len(authentication_key) < 32:
            raise ValueError("ledger authentication key must be at least 32 bytes")
        self._database = database
        self._key = authentication_key
        Base.metadata.create_all(database.engine)

    def _signature(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def _row(self, record: ImprovementRecord) -> ImprovementLedgerRow:
        payload = record.model_dump_json()
        return ImprovementLedgerRow(
            operation_id=record.dispatch.operation_id,
            effect_key=record.dispatch.effect_key,
            kind=record.kind,
            payload=payload,
            signature=self._signature(payload),
        )

    def _decode(self, row: ImprovementLedgerRow) -> ImprovementRecord:
        if not hmac.compare_digest(row.signature, self._signature(row.payload)):
            raise WorkflowError("self-improvement ledger authentication failed")
        record = ImprovementRecord.model_validate_json(row.payload)
        if (
            record.dispatch.operation_id != row.operation_id
            or record.dispatch.effect_key != row.effect_key
            or record.kind != row.kind
        ):
            raise WorkflowError("self-improvement ledger identity mismatch")
        return record

    def records(self, operation_id: str) -> tuple[ImprovementRecord, ...]:
        with self._database.sessions() as session:
            rows = session.scalars(
                select(ImprovementLedgerRow)
                .where(ImprovementLedgerRow.operation_id == operation_id)
                .order_by(ImprovementLedgerRow.sequence)
                .limit(201)
            )
            records = tuple(self._decode(row) for row in rows)
        if len(records) > 200:
            raise WorkflowError("self-improvement ledger stage bound exceeded")
        return records

    def reserve(self, dispatch: StageDispatch) -> bool:
        try:
            with self._database.sessions.begin() as session:
                if session.get(ImprovementOperationRow, dispatch.operation_id) is None:
                    session.add(
                        ImprovementOperationRow(operation_id=dispatch.operation_id, active_key=None)
                    )
        except IntegrityError:
            pass
        try:
            with self._database.sessions.begin() as session:
                changed = session.execute(
                    update(ImprovementOperationRow)
                    .where(
                        ImprovementOperationRow.operation_id == dispatch.operation_id,
                        ImprovementOperationRow.active_key.is_(None),
                    )
                    .values(active_key=dispatch.effect_key)
                )
                if cast(CursorResult[tuple[object, ...]], changed).rowcount != 1:
                    return False
                session.add(self._row(ImprovementRecord(dispatch=dispatch, kind="reserved")))
        except IntegrityError:
            return False
        return True

    def complete(self, dispatch: StageDispatch, evaluation: StageEvaluation) -> None:
        record = ImprovementRecord(dispatch=dispatch, kind="completed", evaluation=evaluation)
        with self._database.sessions.begin() as session:
            reserved = session.scalar(
                select(ImprovementLedgerRow).where(
                    ImprovementLedgerRow.effect_key == dispatch.effect_key,
                    ImprovementLedgerRow.kind == "reserved",
                )
            )
            if reserved is None or self._decode(reserved).dispatch != dispatch:
                raise WorkflowConflict("completion does not match reserved stage")
            changed = session.execute(
                update(ImprovementOperationRow)
                .where(
                    ImprovementOperationRow.operation_id == dispatch.operation_id,
                    ImprovementOperationRow.active_key == dispatch.effect_key,
                )
                .values(active_key=None)
            )
            if cast(CursorResult[tuple[object, ...]], changed).rowcount != 1:
                raise WorkflowConflict("stage completion is stale")
            session.add(self._row(record))
