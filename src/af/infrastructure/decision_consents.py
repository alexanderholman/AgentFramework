"""SQLite-backed remote consent with atomic one-use consumption and receipts.

Only token hashes, disclosure metadata and result digests are persisted. A crash
between consumption and response leaves a consumed receipt and cannot replay a
billable request. Caller-owned instances hold no open connections.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from af.framework.decision_consent import (
    DecisionConsent,
    DecisionPreview,
    DecisionReceipt,
    consent_scope,
    decision_receipt,
    validate_approval,
)
from af.framework.decisions import DecisionAnswer


class SQLiteDecisionConsentRegistry:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = str(path)
        if self._path == ":memory:":
            raise ValueError("durable decision consent requires a file-backed database")
        self._clock = clock
        with self._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS af_decision_consents (
                token_hash TEXT PRIMARY KEY,
                preview TEXT NOT NULL,
                scope TEXT NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL,
                receipt TEXT
            )""")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=30)) as db, db:
            yield db

    @staticmethod
    def _key(consent: DecisionConsent) -> str:
        return hashlib.sha256(consent.token.encode()).hexdigest()

    def approve(self, preview: DecisionPreview, *, max_cost_usd: Decimal) -> DecisionConsent:
        validate_approval(preview, max_cost_usd, self._clock())
        grant = DecisionConsent(secrets.token_urlsafe(32))
        data = asdict(preview)
        data["max_cost_usd"] = str(preview.max_cost_usd)
        with self._connection() as db:
            db.execute(
                "INSERT INTO af_decision_consents VALUES (?, ?, ?, ?, 'approved', NULL)",
                (
                    self._key(grant),
                    json.dumps(data, sort_keys=True),
                    consent_scope(preview),
                    preview.expires_at,
                ),
            )
        return grant

    def revoke(self, consent: DecisionConsent) -> None:
        with self._connection() as db:
            db.execute(
                "UPDATE af_decision_consents SET status='revoked' "
                "WHERE token_hash=? AND status='approved'",
                (self._key(consent),),
            )

    def consume(self, consent: DecisionConsent, *, expected: DecisionPreview) -> bool:
        key = self._key(consent)
        now = self._clock()
        receipt = json.dumps(asdict(decision_receipt(expected, now)), sort_keys=True)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """UPDATE af_decision_consents SET status='consumed', receipt=?
                WHERE token_hash=? AND status='approved' AND expires_at>? AND scope=?""",
                (receipt, key, now, consent_scope(expected)),
            )
            return cursor.rowcount == 1

    def record(self, consent: DecisionConsent, answer: DecisionAnswer) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT preview FROM af_decision_consents WHERE token_hash=? AND status='consumed'",
                (self._key(consent),),
            ).fetchone()
            if row is None:
                raise ValueError("decision consent has no unfinished receipt")
            data = json.loads(row[0])
            for name in ("data_categories", "effects", "risks"):
                data[name] = tuple(data[name])
            data["max_cost_usd"] = Decimal(data["max_cost_usd"])
            preview = DecisionPreview(**data)
            receipt = decision_receipt(preview, self._clock(), answer)
            db.execute(
                "UPDATE af_decision_consents SET status=?, receipt=? "
                "WHERE token_hash=? AND status='consumed'",
                (
                    answer.status.value,
                    json.dumps(asdict(receipt), sort_keys=True),
                    self._key(consent),
                ),
            )

    def receipt(self, consent: DecisionConsent) -> DecisionReceipt | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT receipt FROM af_decision_consents WHERE token_hash=?", (self._key(consent),)
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return DecisionReceipt(**json.loads(row[0]))
