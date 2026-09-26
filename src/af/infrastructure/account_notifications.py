"""Encrypted account outbox; local export and explicit SMTP submission are distinct."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from af.communications.contracts import Channel, Delivery, DeliveryState, Route
from af.communications.delivery import MailManager
from af.framework.accounts import (
    AccountNotification,
    AccountNotificationContent,
    SecretCipher,
    account_address,
)
from af.framework.authentication import AuthenticationDenied, identity_value
from af.infrastructure.authentication import SQLiteAuthenticationRepository


class SQLiteAccountNotificationOutbox(SQLiteAuthenticationRepository):
    def __init__(
        self,
        path: str | Path,
        cipher: SecretCipher,
        *,
        tenant_id: str,
        mail: MailManager | None = None,
        mail_driver: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(path)
        identity_value(tenant_id)
        if (mail is None) != (mail_driver is None):
            raise ValueError("account mail requires an explicit named mail driver")
        self._cipher, self._tenant, self._mail, self._driver, self._clock = (
            cipher,
            tenant_id,
            mail,
            mail_driver,
            clock,
        )
        with self._connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS af_account_notifications (message_id TEXT PRIMARY "
                "KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL, ciphertext TEXT NOT NULL, "
                "status TEXT NOT NULL, created_at REAL NOT NULL)"
            )

    def deliver(self, address: str, kind: str, secret: str) -> None:
        address = account_address(address)
        if (
            kind not in {"verify", "reset"}
            or type(secret) is not str
            or not 1 <= len(secret) <= 1024
        ):
            raise ValueError("invalid account notification")
        content = self._cipher.encrypt(
            json.dumps({"address": address, "kind": kind, "secret": secret})
        )
        with self._connection() as db:
            db.execute(
                "INSERT INTO af_account_notifications VALUES (?, ?, ?, ?, 'pending', ?)",
                (uuid4().hex, self._tenant, kind, content, self._clock()),
            )

    def pending(self) -> tuple[AccountNotification, ...]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT message_id, kind, status, created_at FROM af_account_notifications "
                "WHERE tenant_id=? ORDER BY created_at, message_id LIMIT 100",
                (self._tenant,),
            ).fetchall()
        return tuple(AccountNotification(*row) for row in rows)

    def _claim(self, message_id: str, status: str) -> AccountNotificationContent:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT ciphertext FROM af_account_notifications WHERE message_id=? AND "
                "tenant_id=? AND status='pending'",
                (message_id, self._tenant),
            ).fetchone()
            if row is None:
                raise AuthenticationDenied()
            content = json.loads(self._cipher.decrypt(row[0]))
            result = AccountNotificationContent(
                content["address"], content["kind"], content["secret"]
            )
            db.execute(
                "UPDATE af_account_notifications SET status=? WHERE message_id=?",
                (status, message_id),
            )
            return result

    def export(self, message_id: str) -> AccountNotificationContent:
        # Export is host administration and does not establish remote mailbox ownership.
        return self._claim(message_id, "exported-local")

    def dispatch(self, message_id: str) -> str:
        if self._mail is None or self._driver is None:
            raise AuthenticationDenied()
        content = self._claim(message_id, "sending")
        delivery = Delivery(
            message_id,
            self._tenant + ":account",
            Route(Channel.MAIL, content.address, self._driver),
            "en",
            "AgentFramework account " + content.kind,
            "Use this one-time account code within 15 minutes: " + content.secret,
        )
        try:
            result = self._mail.send(delivery)
            status = result.state.value
        except Exception:
            status = DeliveryState.UNKNOWN.value
        with self._connection() as db:
            db.execute(
                "UPDATE af_account_notifications SET status=? WHERE message_id=? AND "
                "status='sending'",
                (status, message_id),
            )
        return status
