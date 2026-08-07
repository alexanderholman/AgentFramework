"""Unit tests for af.mail.contracts domain models."""

import pytest
from datetime import datetime, timezone
from af.mail.contracts import (
    Channel,
    Recipient,
    NotificationIntent,
    DeliveryReceipt,
    NotificationPreferences,
    DeliveryPolicy,
    IntentStatus,
    ReceiptStatus,
    BounceType,
)


class TestChannel:
    def test_email_channel(self):
        assert Channel.EMAIL.value == "email"

    def test_sms_channel(self):
        assert Channel.SMS.value == "sms"

    def test_push_channel(self):
        assert Channel.PUSH.value == "push"


class TestRecipient:
    def test_basic_recipient(self):
        r = Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)
        assert r.id == "u1"
        assert r.address == "a@b.com"
        assert r.channel == Channel.EMAIL

    def test_recipient_with_locale(self):
        r = Recipient(id="u2", address="+1234", channel=Channel.SMS, locale="es")
        assert r.locale == "es"

    def test_recipient_with_metadata(self):
        r = Recipient(id="u3", address="x@y.com", channel=Channel.EMAIL, metadata={"tag": "promo"})
        assert r.metadata == {"tag": "promo"}


class TestNotificationIntent:
    def test_minimal_intent(self):
        ni = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        assert ni.id == "n1"
        assert ni.subject == "Hi"
        assert ni.template_id is None
        assert ni.template_vars is None
        assert ni.locale is None
        assert ni.priority == "normal"
        assert ni.idempotency_key is None
        assert ni.status == IntentStatus.PENDING
        assert ni.attachments == []

    def test_intent_with_template(self):
        ni = NotificationIntent(
            id="n2",
            subject="Welcome",
            body="",
            recipients=[],
            template_id="welcome_v1",
            template_vars={"name": "Alice"},
            locale="en",
        )
        assert ni.template_id == "welcome_v1"
        assert ni.template_vars == {"name": "Alice"}
        assert ni.locale == "en"

    def test_intent_with_idempotency_key(self):
        ni = NotificationIntent(
            id="n3",
            subject="Test",
            body="",
            recipients=[],
            idempotency_key="dedup-1",
        )
        assert ni.idempotency_key == "dedup-1"

    def test_intent_with_attachments(self):
        ni = NotificationIntent(
            id="n4",
            subject="Test",
            body="",
            recipients=[],
            attachments=[{"filename": "a.txt", "data": b"hi"}],
        )
        assert len(ni.attachments) == 1

    def test_intent_high_priority(self):
        ni = NotificationIntent(
            id="n5",
            subject="Urgent",
            body="",
            recipients=[],
            priority="high",
        )
        assert ni.priority == "high"


class TestDeliveryReceipt:
    def test_pending_receipt(self):
        dr = DeliveryReceipt(
            id="r1",
            intent_id="n1",
            recipient_id="u1",
            channel=Channel.EMAIL,
            status=ReceiptStatus.PENDING,
        )
        assert dr.status == ReceiptStatus.PENDING
        assert dr.error is None
        assert dr.external_id is None

    def test_delivered_receipt(self):
        ts = datetime.now(timezone.utc)
        dr = DeliveryReceipt(
            id="r2",
            intent_id="n1",
            recipient_id="u1",
            channel=Channel.EMAIL,
            status=ReceiptStatus.DELIVERED,
            delivered_at=ts,
        )
        assert dr.status == ReceiptStatus.DELIVERED
        assert dr.delivered_at == ts

    def test_failed_receipt_with_bounce(self):
        dr = DeliveryReceipt(
            id="r3",
            intent_id="n1",
            recipient_id="u1",
            channel=Channel.EMAIL,
            status=ReceiptStatus.FAILED,
            error="SMTP 550",
            bounce_type=BounceType.PERMANENT,
        )
        assert dr.status == ReceiptStatus.FAILED
        assert dr.bounce_type == BounceType.PERMANENT


class TestNotificationPreferences:
    def test_all_enabled(self):
        prefs = NotificationPreferences(user_id="u1")
        assert prefs.email_enabled is True
        assert prefs.sms_enabled is True
        assert prefs.push_enabled is True

    def test_channel_disabled(self):
        prefs = NotificationPreferences(user_id="u1", email_enabled=False)
        assert prefs.email_enabled is False

    def test_quiet_hours(self):
        prefs = NotificationPreferences(
            user_id="u1", quiet_hours_begin="22:00", quiet_hours_end="07:00"
        )
        assert prefs.quiet_hours_begin == "22:00"
        assert prefs.quiet_hours_end == "07:00"

    def test_locale(self):
        prefs = NotificationPreferences(user_id="u1", locale="fr")
        assert prefs.locale == "fr"


class TestDeliveryPolicy:
    def test_default_policy(self):
        dp = DeliveryPolicy()
        assert dp.max_retries == 3
        assert dp.retry_delay_seconds == 60
        assert dp.suppress_on_permanent_bounce is True

    def test_custom_policy(self):
        dp = DeliveryPolicy(
            max_retries=5, retry_delay_seconds=120, suppress_on_permanent_bounce=False
        )
        assert dp.max_retries == 5
        assert dp.retry_delay_seconds == 120
        assert dp.suppress_on_permanent_bounce is False


class TestIntentStatus:
    def test_all_statuses(self):
        assert IntentStatus.PENDING.value == "pending"
        assert IntentStatus.QUEUED.value == "queued"
        assert IntentStatus.SENT.value == "sent"
        assert IntentStatus.FAILED.value == "failed"
        assert IntentStatus.CANCELLED.value == "cancelled"


class TestReceiptStatus:
    def test_all_statuses(self):
        assert ReceiptStatus.PENDING.value == "pending"
        assert ReceiptStatus.SENT.value == "sent"
        assert ReceiptStatus.DELIVERED.value == "delivered"
        assert ReceiptStatus.FAILED.value == "failed"
        assert ReceiptStatus.BOUNCED.value == "bounced"
        assert ReceiptStatus.COMPLAINT.value == "complaint"


class TestBounceType:
    def test_all_bounce_types(self):
        assert BounceType.PERMANENT.value == "permanent"
        assert BounceType.TEMPORARY.value == "temporary"
        assert BounceType.UNSUBSCRIBE.value == "unsubscribe"
