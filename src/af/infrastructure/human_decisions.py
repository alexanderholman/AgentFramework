"""Authenticated SQLite human decisions, append-only state evidence and one-use claims."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from af.framework.human_decisions import HumanDecisionError, PendingHumanDecision


class SQLiteHumanDecisionRepository:
    def __init__(self, path: Path, authentication_key: bytes) -> None:
        if str(path) == ":memory:" or len(authentication_key) < 32 or path.is_symlink():
            raise ValueError("durable decisions require a persistent path and host key")
        self._path, self._key = path, authentication_key
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with self._connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS af_human_decisions (id TEXT PRIMARY KEY, "
                "payload TEXT NOT NULL, signature TEXT NOT NULL, status TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS af_human_decision_history ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, "
                "payload TEXT NOT NULL, signature TEXT NOT NULL, status TEXT NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS af_human_decision_history_id "
                "ON af_human_decision_history(id, sequence)"
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=5)) as db, db:
            yield db

    def _signature(self, payload: str, status: str = "pending") -> str:
        return hmac.new(
            self._key,
            b"af.human-decision/1\0" + status.encode() + b"\0" + payload.encode(),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _identifier(decision_id: str) -> None:
        if type(decision_id) is not str or re.fullmatch(r"[a-f0-9]{32}", decision_id) is None:
            raise HumanDecisionError()

    def _read(self, db: sqlite3.Connection, decision_id: str) -> tuple[str, str, str]:
        self._identifier(decision_id)
        row = db.execute(
            "SELECT payload,signature,status FROM af_human_decisions WHERE id=?", (decision_id,)
        ).fetchone()
        latest = db.execute(
            "SELECT payload,signature,status FROM af_human_decision_history "
            "WHERE id=? ORDER BY sequence DESC LIMIT 1",
            (decision_id,),
        ).fetchone()
        if (
            row is None
            or row != latest
            or any(type(value) is not str for value in row)
            or len(row[0].encode()) > 131_072
            or not hmac.compare_digest(row[1], self._signature(row[0], row[2]))
        ):
            raise HumanDecisionError()
        return str(row[0]), str(row[1]), str(row[2])

    @staticmethod
    def _history(
        db: sqlite3.Connection, decision_id: str, payload: str, signature: str, status: str
    ) -> None:
        db.execute(
            "INSERT INTO af_human_decision_history(id,payload,signature,status) VALUES (?,?,?,?)",
            (decision_id, payload, signature, status),
        )

    def create(self, decision: PendingHumanDecision) -> None:
        payload = decision.model_dump_json()
        if len(payload.encode()) > 131_072:
            raise HumanDecisionError()
        signature = self._signature(payload)
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                # Do not resurrect an identifier whose current row was removed.
                if db.execute(
                    "SELECT 1 FROM af_human_decision_history WHERE id=? LIMIT 1",
                    (decision.decision_id,),
                ).fetchone():
                    raise HumanDecisionError()
                db.execute(
                    "INSERT INTO af_human_decisions VALUES (?, ?, ?, 'pending')",
                    (decision.decision_id, payload, signature),
                )
                self._history(db, decision.decision_id, payload, signature, "pending")
        except sqlite3.IntegrityError:
            raise HumanDecisionError() from None

    def get(self, decision_id: str) -> PendingHumanDecision:
        with self._connection() as db:
            db.execute("BEGIN")
            payload, _, status = self._read(db, decision_id)
        if status != "pending":
            raise HumanDecisionError()
        try:
            result = PendingHumanDecision.model_validate_json(payload)
        except ValueError:
            raise HumanDecisionError() from None
        if result.decision_id != decision_id:
            raise HumanDecisionError()
        return result

    def _transition(self, decision: PendingHumanDecision, status: str) -> None:
        payload = decision.model_dump_json()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            stored, signature, previous = self._read(db, decision.decision_id)
            if stored != payload or previous != "pending":
                raise HumanDecisionError()
            signed = self._signature(payload, status)
            changed = db.execute(
                "UPDATE af_human_decisions SET status=?,signature=? WHERE id=? "
                "AND signature=? AND status='pending'",
                (status, signed, decision.decision_id, signature),
            ).rowcount
            if changed != 1:
                raise HumanDecisionError()
            self._history(db, decision.decision_id, payload, signed, status)

    def claim(self, decision: PendingHumanDecision) -> None:
        self._transition(decision, "claimed")

    def revoke(self, decision: PendingHumanDecision) -> None:
        self._transition(decision, "revoked")

    def finish(self, decision_id: str, status: str) -> None:
        if status not in {"completed", "inconclusive"}:
            raise ValueError("invalid decision terminal status")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            payload, signature, previous = self._read(db, decision_id)
            if previous != "claimed":
                raise HumanDecisionError()
            signed = self._signature(payload, status)
            changed = db.execute(
                "UPDATE af_human_decisions SET status=?,signature=? "
                "WHERE id=? AND status='claimed' AND signature=?",
                (status, signed, decision_id, signature),
            ).rowcount
            if changed != 1:
                raise HumanDecisionError()
            self._history(db, decision_id, payload, signed, status)
