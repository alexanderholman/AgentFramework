"""Notification and mail domain contracts for AF Issue #26.

Defines intents, recipients, channels, delivery receipts, and
preference/consent models that the MailManager consumes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class Channel(str, Enum):
    """Communication channel for notification delivery."""

    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    WEBHOOK = "webhook"
    IN_APP = "in_app"


class ChannelPriority(int, Enum):
    """Priority ordering for channel fallback."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


# ---------------------------------------------------------------------------
# Recipient
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Recipient:
    """Addressed recipient with channel-specific address and preferences."""

    id: str
    channel: Channel
    address: str
    locale: str = "en"
    preferences: dict[str, Any] = field(default_factory=dict)
    consented: bool = True
    quiet_hours: Optional[dict[str, Any]] = None
    priority: ChannelPriority = ChannelPriority.NORMAL

    def is_available(self) -> bool:
        """Return True when the recipient has consented."""
        return self.consented


# ---------------------------------------------------------------------------
# Notification Intent
# ---------------------------------------------------------------------------


class IntentStatus(str, Enum):
    """Lifecycle status of a notification intent."""

    PENDING = "pending"
    QUEUED = "queued"
    DELIVERED = "delivered"
    FAILED = "failed"
    BOUNCED = "bounced"
    COMPLAINT = "complaint"
    SUPPRESSED = "suppressed"


@dataclass
class NotificationIntent:
    """High-level description of what to communicate and to whom."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    subject: str = ""
    template: Optional[str] = None
    template_context: dict[str, Any] = field(default_factory=dict)
    body_text: str = ""
    body_html: Optional[str] = None
    recipients: list[Recipient] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    locale: str = "en"
    correlation_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    priority: ChannelPriority = ChannelPriority.NORMAL
    status: IntentStatus = IntentStatus.PENDING
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Populated by the delivery pipeline
    receipts: list[DeliveryReceipt] = field(default_factory=list)

    def add_receipt(self, receipt: DeliveryReceipt) -> None:
        self.receipts.append(receipt)
        self.updated_at = datetime.now(timezone.utc)
        if receipt.status in (
            IntentStatus.DELIVERED,
            IntentStatus.BOUNCED,
            IntentStatus.COMPLAINT,
            IntentStatus.FAILED,
        ):
            self.status = receipt.status

    def is_terminal(self) -> bool:
        """Return True if the intent has reached a terminal state."""
        return self.status in (
            IntentStatus.DELIVERED,
            IntentStatus.BOUNCED,
            IntentStatus.COMPLAINT,
            IntentStatus.FAILED,
            IntentStatus.SUPPRESSED,
        )


# ---------------------------------------------------------------------------
# Delivery Receipt
# ---------------------------------------------------------------------------


class ReceiptStatus(str, Enum):
    """Granular status for a single delivery attempt."""

    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    OPENED = "opened"
    CLICKED = "clicked"
    FAILED = "failed"
    BOUNCED = "bounced"
    COMPLAINT = "complaint"
    SUPPRESSED = "suppressed"
    DEFERRED = "deferred"


class BounceType(str, Enum):
    """Classification of a bounce event."""

    SOFT = "soft"
    HARD = "hard"
    UNKNOWN = "unknown"


@dataclass
class DeliveryReceipt:
    """Per-recipient delivery outcome record."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    intent_id: str = ""
    recipient: Recipient = None  # type: ignore[assignment]
    channel: Channel = Channel.EMAIL
    status: ReceiptStatus = ReceiptStatus.PENDING
    provider_message_id: Optional[str] = None
    bounce_type: Optional[BounceType] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    attempted_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    attempt_number: int = 0


# ---------------------------------------------------------------------------
# Preferences and Policy
# ---------------------------------------------------------------------------


@dataclass
class NotificationPreferences:
    """Per-recipient or per-tenant notification policy."""

    allowed_channels: list[Channel] = field(default_factory=lambda: [Channel.EMAIL])
    suppressed_channels: list[Channel] = field(default_factory=list)
    quiet_hours_start: Optional[str] = None  # ISO time, e.g. "22:00"
    quiet_hours_end: Optional[str] = None  # ISO time, e.g. "07:00"
    locale: str = "en"
    frequency_limit: Optional[int] = None  # max notifications per window
    frequency_window_seconds: int = 3600  # default: 1 hour


@dataclass
class DeliveryPolicy:
    """Policy governing how intents are routed and delivered."""

    max_attempts: int = 3
    retry_delay_seconds: int = 60
    idempotency_ttl_seconds: int = 86400  # 24 hours
    default_channel: Channel = Channel.EMAIL
    fallback_channels: list[Channel] = field(default_factory=list)
    suppress_on_hard_bounce: bool = True
    suppress_on_complaint: bool = True
