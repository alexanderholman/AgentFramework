"""SMTP driver for MailManager.

Provides a real SMTP transport that can be configured with host, port,
credentials, and TLS settings.  Follows the AF driver pattern.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional

from .contracts import (
    Channel,
    DeliveryPolicy,
    DeliveryReceipt,
    NotificationIntent,
    ReceiptStatus,
    Recipient,
)
from .manager import MailDriver


class SmtpDriver(MailDriver):
    """SMTP-based mail delivery driver.

    Parameters:
        host: SMTP server hostname (default: localhost).
        port: SMTP server port (default: 587).
        username: Auth username (optional).
        password: Auth password (optional).
        use_tls: Enable STARTTLS (default: True).
        sender: Default From address.
    """

    name: str = "smtp"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 587,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_tls: bool = True,
        sender: str = "noreply@example.com",
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.sender = sender
        self._connection: Optional[Any] = None

    def send(
        self,
        intent: NotificationIntent,
        recipient: Recipient,
        policy: DeliveryPolicy,
    ) -> DeliveryReceipt:
        """Send a single intent to a single recipient via SMTP."""
        now = datetime.now(timezone.utc)
        msg = self._build_message(intent, recipient)

        try:
            server = self._get_connection()
            msg_id = uuid.uuid4().hex
            server.sendmail(
                self.sender,
                [recipient.address],
                msg.as_string(),
            )
            server.quit()
            self._connection = None

            return DeliveryReceipt(
                intent_id=intent.id,
                recipient=recipient,
                channel=Channel.EMAIL,
                status=ReceiptStatus.SENT,
                provider_message_id=msg_id,
                attempted_at=now,
                delivered_at=now,
            )
        except Exception as exc:
            return DeliveryReceipt(
                intent_id=intent.id,
                recipient=recipient,
                channel=Channel.EMAIL,
                status=ReceiptStatus.FAILED,
                attempted_at=now,
                error_code="smtp_error",
                error_message=str(exc),
            )

    def close(self) -> None:
        """Close the SMTP connection if open."""
        if self._connection is not None:
            try:
                self._connection.quit()
            except Exception:
                pass
            self._connection = None

    # -- internal helpers ---------------------------------------------------

    def _get_connection(self) -> Any:
        """Lazily create and cache an SMTP connection."""
        if self._connection is not None:
            return self._connection

        import smtplib

        if self.use_tls:
            server = smtplib.SMTP(self.host, self.port, timeout=30)
            server.ehlo()
            server.starttls()
            server.ehlo()
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=30)

        if self.username and self.password:
            server.login(self.username, self.password)

        self._connection = server
        return server

    @staticmethod
    def _build_message(
        intent: NotificationIntent,
        recipient: Recipient,
    ) -> MIMEMultipart:
        """Construct a MIME message from an intent."""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = intent.subject
        msg["From"] = "noreply@example.com"
        msg["To"] = recipient.address

        # Text body
        if intent.body_text:
            msg.attach(MIMEText(intent.body_text, "plain", recipient.locale))

        # HTML body
        if intent.body_html:
            msg.attach(MIMEText(intent.body_html, "html", recipient.locale))

        return msg
