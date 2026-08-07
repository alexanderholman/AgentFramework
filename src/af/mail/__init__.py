"""Mail / notification contracts, manager, drivers, and fakes."""

from af.mail.contracts import (
    BounceType,
    Channel,
    ChannelPriority,
    DeliveryPolicy,
    DeliveryReceipt,
    IntentStatus,
    NotificationIntent,
    NotificationPreferences,
    ReceiptStatus,
    Recipient,
)
from af.mail.fakes import (
    FakeCompositeDriver,
    FakeMailDriver,
    FakeMessage,
    FakeSmtpDriver,
)
from af.mail.manager import MailDriver, MailManager
from af.mail.smtp import SmtpDriver

__all__ = (
    "BounceType",
    "Channel",
    "ChannelPriority",
    "DeliveryPolicy",
    "DeliveryReceipt",
    "FakeCompositeDriver",
    "FakeMailDriver",
    "FakeMessage",
    "FakeSmtpDriver",
    "IntentStatus",
    "MailDriver",
    "MailManager",
    "NotificationIntent",
    "NotificationPreferences",
    "ReceiptStatus",
    "Recipient",
    "SmtpDriver",
)
