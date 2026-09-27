"""Atomic authenticated component+classifier catalogue with fenced gap coalescing."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import cast

from sqlalchemy import Integer, String, Text, literal, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from af.framework.engineering import ComponentAdmission
from af.framework.workflows import WorkflowConflict, WorkflowError
from af.infrastructure.database import Base, Database


class EngineeringCatalogueRow(Base):
    __tablename__ = "af_engineering_catalogue"
    family: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32))
    payload: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(String(64))


class EngineeringHistoryRow(Base):
    __tablename__ = "af_engineering_history"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    family: Mapped[str] = mapped_column(String(128))
    owner: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32))
    payload: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(String(64))


@dataclass(frozen=True, slots=True)
class _AuthenticatedState:
    owner: str
    state: str
    payload: str
    signature: str


class SqlAlchemyEngineeringCatalogue:
    def __init__(self, database: Database, *, authentication_key: bytes) -> None:
        if len(authentication_key) < 32:
            raise ValueError("catalogue authentication key must be at least 32 bytes")
        self._database = database
        self._key = authentication_key
        Base.metadata.create_all(database.engine)

    def _signature(self, family: str, owner: str, state: str, payload: str) -> str:
        data = json.dumps([family, owner, state, payload], ensure_ascii=True).encode()
        return hmac.new(self._key, data, hashlib.sha256).hexdigest()

    def _row(self, session: Session, family: str) -> _AuthenticatedState | None:
        latest = (
            select(
                EngineeringHistoryRow.owner,
                EngineeringHistoryRow.state,
                EngineeringHistoryRow.payload,
                EngineeringHistoryRow.signature,
            )
            .where(EngineeringHistoryRow.family == family)
            .order_by(EngineeringHistoryRow.sequence.desc())
            .limit(1)
            .subquery()
        )
        current = select(
            literal("current"),
            EngineeringCatalogueRow.owner,
            EngineeringCatalogueRow.state,
            EngineeringCatalogueRow.payload,
            EngineeringCatalogueRow.signature,
        ).where(EngineeringCatalogueRow.family == family)
        # One statement gives both records the same database snapshot even with
        # SQLite's legacy SELECT transaction behavior during concurrent admission.
        records = session.execute(
            current.union_all(
                select(
                    literal("history"),
                    latest.c.owner,
                    latest.c.state,
                    latest.c.payload,
                    latest.c.signature,
                )
            )
        ).all()
        if not records:
            return None
        states = {
            record[0]: _AuthenticatedState(*(str(value) for value in record[1:]))
            for record in records
        }
        if set(states) != {"current", "history"}:
            raise WorkflowError("engineering_catalogue_history_missing")
        row = states["current"]
        if row != states["history"]:
            raise WorkflowError("engineering_catalogue_stale_state")
        if not hmac.compare_digest(
            row.signature, self._signature(family, row.owner, row.state, row.payload)
        ):
            raise WorkflowError("engineering_catalogue_authentication_failed")
        return row

    def lookup(self, family: str) -> ComponentAdmission | None:
        with self._database.sessions() as session:
            row = self._row(session, family)
            if row is None or row.state != "admitted":
                return None
            admission = ComponentAdmission.model_validate_json(row.payload)
            if admission.family != family:
                raise WorkflowError("engineering_catalogue_identity_mismatch")
            return admission

    def status(self, family: str) -> str | None:
        with self._database.sessions() as session:
            row = self._row(session, family)
            return row.state if row is not None else None

    def claim(self, family: str, owner: str) -> bool:
        try:
            with self._database.sessions.begin() as session:
                if self._row(session, family) is not None:
                    return False
                signature = self._signature(family, owner, "engineering", "")
                session.add(
                    EngineeringCatalogueRow(
                        family=family,
                        owner=owner,
                        state="engineering",
                        payload="",
                        signature=signature,
                    )
                )
                session.add(
                    EngineeringHistoryRow(
                        family=family,
                        owner=owner,
                        state="engineering",
                        payload="",
                        signature=signature,
                    )
                )
        except IntegrityError:
            return False
        return True

    def _transition(
        self,
        family: str,
        owner: str | None,
        expected: str,
        target: str,
        payload: str,
        *,
        optional: bool = False,
    ) -> None:
        with self._database.sessions.begin() as session:
            row = self._row(session, family)
            if row is None or row.state != expected or (owner is not None and row.owner != owner):
                if optional:
                    return
                raise WorkflowConflict("engineering_admission_fence_rejected")
            signature = self._signature(family, row.owner, target, payload)
            changed = session.execute(
                update(EngineeringCatalogueRow)
                .where(
                    EngineeringCatalogueRow.family == family,
                    EngineeringCatalogueRow.signature == row.signature,
                )
                .values(state=target, payload=payload, signature=signature)
            )
            if cast(CursorResult[tuple[object, ...]], changed).rowcount != 1:
                raise WorkflowConflict("engineering_admission_fence_rejected")
            session.add(
                EngineeringHistoryRow(
                    family=family,
                    owner=row.owner,
                    state=target,
                    payload=payload,
                    signature=signature,
                )
            )

    def admit(self, admission: ComponentAdmission, owner: str) -> None:
        self._transition(
            admission.family, owner, "engineering", "admitted", admission.model_dump_json()
        )

    def reject(self, family: str, owner: str, reason: str) -> None:
        self._transition(family, owner, "engineering", "rejected", reason, optional=True)

    def invalidate(self, family: str, reason: str) -> None:
        self._transition(family, None, "admitted", "invalidated", reason)
