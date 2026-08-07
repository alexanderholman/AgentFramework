"""Fake drivers for MailManager testing.

Provides in-memory, deterministic fakes that record sent messages
without performing I/O.  Follows the AF fake pattern from
`af.testing.fakes`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


@dataclass
class FakeMessage:
    """Record of a message sent through a fake driver."""

    intent: NotificationIntent
    recipient: Recipient
    channel: Channel
    sent_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class FakeMailDriver(MailDriver):
    """In-memory mail driver that records messages without I/O.

    Attributes:
        messages: List of FakeMessage records for assertion.
        fail_on: If set, simulate failure for matching conditions.
    """

    name: str = "fake"

    def __init__(
        self,
        fail_on: Optional[dict[str, Any]] = None,
        default_status: ReceiptStatus = ReceiptStatus.SENT,
    ) -> None:
        self.messages: list[FakeMessage] = []
        self.fail_on = fail_on or {}
        self.default_status = default_status

    def send(
        self,
        intent: NotificationIntent,
        recipient: Recipient,
        policy: DeliveryPolicy,
    ) -> DeliveryReceipt:
        """Record the message and return a success receipt."""
        now = datetime.now(timezone.utc)

        # Simulate failure if configured
        if self._should_fail(intent, recipient):
            return DeliveryReceipt(
                intent_id=intent.id,
                recipient=recipient,
                channel=recipient.channel,
                status=ReceiptStatus.FAILED,
                attempted_at=now,
                error_code="fake_failure",
                error_message=self.fail_on.get("message", "simulated failure"),
            )

        msg = FakeMessage(
            intent=intent,
            recipient=recipient,
            channel=recipient.channel,
        )
        self.messages.append(msg)

        return DeliveryReceipt(
            intent_id=intent.id,
            recipient=recipient,
            channel=recipient.channel,
            status=self.default_status,
            provider_message_id=uuid.uuid4().hex,
            attempted_at=now,
            delivered_at=now,
        )

    def clear(self) -> None:
        """Clear recorded messages."""
        self.messages.clear()

    def _should_fail(
        self,
        intent: NotificationIntent,
        recipient: Recipient,
    ) -> bool:
        """Check if this send should simulate a failure."""
        if "always" in self.fail_on:
            return self.fail_on["always"]
        if "recipient_id" in self.fail_on and recipient.id == self.fail_on["recipient_id"]:
            return True
        if "address" in self.fail_on and recipient.address == self.fail_on["address"]:
            return True
        return False


class FakeSmtpDriver(FakeMailDriver):
    """Fake driver that mimics SmtpDriver behavior for testing."""

    name: str = "fake-smtp"


class FakeCompositeDriver(MailDriver):
    """Composite driver that delegates to multiple drivers with failover.

    Tries drivers in order until one succeeds.  Useful for testing
    failover and round-robin routing.
    """

    name: str = "fake-composite"

    def __init__(self, drivers: list[MailDriver]) -> None:
        self.drivers = drivers

    def send(
        self,
        intent: NotificationIntent,
        recipient: Recipient,
        policy: DeliveryPolicy,
    ) -> DeliveryReceipt:
        """Try each driver in order until one succeeds."""
        last_error: Optional[str] = None
        for driver in self.drivers:
            try:
                receipt = driver.send(intent, recipient, policy)
                if receipt.status not in (ReceiptStatus.FAILED, ReceiptStatus.BOUNCED):
                    return receipt
                last_error = receipt.error_message
            except Exception as exc:
                last_error = str(exc)

        return DeliveryReceipt(
            intent_id=intent.id,
            recipient=recipient,
            channel=recipient.channel,
            status=ReceiptStatus.FAILED,
            error_message=f"all drivers failed: {last_error}",
        )
