"""Durable single-claim notification receipts without an exactly-once claim."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock
from typing import Protocol

from af.communications.contracts import DeliveryReceipt, DeliveryState


class DeliveryStore(Protocol):
    def claim(self, delivery_id: str, fingerprint: str) -> DeliveryReceipt | None: ...
    def complete(self, receipt: DeliveryReceipt) -> None: ...


class SQLiteDeliveryStore:
    """A crashed PENDING send stays pending until reconciled, preventing blind resend.

    Only a definite RETRYABLE_FAILURE can be claimed again automatically. This
    store contains receipt IDs and digests, never addresses or message bodies.
    """

    def __init__(self, path: str | Path) -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS af_delivery_receipts ("
            "id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL, provider_id TEXT)"
        )
        self._connection.commit()

    def claim(self, delivery_id: str, fingerprint: str) -> DeliveryReceipt | None:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT fingerprint, state, provider_id FROM af_delivery_receipts WHERE id=?",
                    (delivery_id,),
                ).fetchone()
                if row is not None and row[0] != fingerprint:
                    raise ValueError("Notification idempotency key reused with different content")
                if row is not None and row[1] != DeliveryState.RETRYABLE_FAILURE.value:
                    return DeliveryReceipt(delivery_id, DeliveryState(row[1]), row[2])
                self._connection.execute(
                    "INSERT INTO af_delivery_receipts VALUES (?, ?, ?, NULL) "
                    "ON CONFLICT(id) DO UPDATE SET state=excluded.state, provider_id=NULL",
                    (delivery_id, fingerprint, DeliveryState.PENDING.value),
                )
                return None
            finally:
                self._connection.commit()

    def complete(self, receipt: DeliveryReceipt) -> None:
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE af_delivery_receipts SET state=?, provider_id=? WHERE id=? AND state=?",
                (
                    receipt.state.value,
                    receipt.provider_id,
                    receipt.delivery_id,
                    DeliveryState.PENDING.value,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("Notification receipt has no active claim")

    def record_feedback(self, delivery_id: str, state: DeliveryState) -> None:
        """Apply authenticated provider feedback after acceptance, never trigger sending."""
        if state not in {DeliveryState.BOUNCED, DeliveryState.COMPLAINT, DeliveryState.SUPPRESSED}:
            raise ValueError("Invalid notification feedback state")
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE af_delivery_receipts SET state=? WHERE id=? AND state IN (?, ?, ?)",
                (
                    state.value,
                    delivery_id,
                    DeliveryState.ACCEPTED.value,
                    DeliveryState.BOUNCED.value,
                    DeliveryState.COMPLAINT.value,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("Feedback requires an accepted notification receipt")

    def close(self) -> None:
        self._connection.close()
