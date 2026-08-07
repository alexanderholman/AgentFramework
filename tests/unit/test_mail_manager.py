"""Unit tests for af.mail.manager MailManager and MailDriver protocol."""

import pytest
from af.mail.manager import MailManager
from af.mail.contracts import (
    Channel,
    Recipient,
    NotificationIntent,
    NotificationPreferences,
    DeliveryPolicy,
    ReceiptStatus,
)
from af.mail.fakes import FakeMailDriver


class TestMailManagerIdempotency:
    def test_reject_duplicate_idempotency_key(self):
        driver = FakeMailDriver()
        mgr = MailManager(drivers={Channel.EMAIL: driver}, policy=DeliveryPolicy())
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
            idempotency_key="dedup-1",
        )
        mgr.submit(intent)
        with pytest.raises(ValueError, match="already processed"):
            mgr.submit(intent)

    def test_allow_different_idempotency_keys(self):
        driver = FakeMailDriver()
        mgr = MailManager(drivers={Channel.EMAIL: driver}, policy=DeliveryPolicy())
        intent1 = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
            idempotency_key="key-1",
        )
        intent2 = NotificationIntent(
            id="n2",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
            idempotency_key="key-2",
        )
        mgr.submit(intent1)
        mgr.submit(intent2)


class TestMailManagerQuietHours:
    def test_skip_during_quiet_hours(self):
        driver = FakeMailDriver()
        mgr = MailManager(drivers={Channel.EMAIL: driver}, policy=DeliveryPolicy())
        prefs = NotificationPreferences(
            user_id="u1", quiet_hours_begin="00:00", quiet_hours_end="23:59"
        )
        mgr.set_preferences("u1", prefs)
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        mgr.submit(intent)
        assert len(driver.sent_intents) == 0

    def test_allow_outside_quiet_hours(self):
        driver = FakeMailDriver()
        mgr = MailManager(drivers={Channel.EMAIL: driver}, policy=DeliveryPolicy())
        prefs = NotificationPreferences(
            user_id="u1", quiet_hours_begin="22:00", quiet_hours_end="06:00"
        )
        mgr.set_preferences("u1", prefs)
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        mgr.submit(intent)
        assert len(driver.sent_intents) == 1

    def test_no_quiet_hours(self):
        driver = FakeMailDriver()
        mgr = MailManager(drivers={Channel.EMAIL: driver}, policy=DeliveryPolicy())
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        mgr.submit(intent)
        assert len(driver.sent_intents) == 1


class TestMailManagerChannelResolution:
    def test_resolve_channel_from_recipient(self):
        driver = FakeMailDriver()
        mgr = MailManager(drivers={Channel.EMAIL: driver}, policy=DeliveryPolicy())
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        mgr.submit(intent)
        sent = driver.sent_intents[0]
        assert sent.recipients[0].channel == Channel.EMAIL

    def test_error_when_no_driver_for_channel(self):
        mgr = MailManager(drivers={}, policy=DeliveryPolicy())
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        with pytest.raises(RuntimeError, match="no driver"):
            mgr.submit(intent)


class TestMailManagerDriverDelegation:
    def test_delegate_to_driver(self):
        driver = FakeMailDriver()
        mgr = MailManager(drivers={Channel.EMAIL: driver}, policy=DeliveryPolicy())
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        mgr.submit(intent)
        assert len(driver.sent_intents) == 1
        assert driver.sent_intents[0].id == "n1"

    def test_multiple_recipients(self):
        driver = FakeMailDriver()
        mgr = MailManager(drivers={Channel.EMAIL: driver}, policy=DeliveryPolicy())
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[
                Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL),
                Recipient(id="u2", address="c@d.com", channel=Channel.EMAIL),
            ],
        )
        mgr.submit(intent)
        assert len(driver.sent_intents) == 1


class TestMailManagerReceipts:
    def test_receipt_recorded_on_send(self):
        driver = FakeMailDriver()
        mgr = MailManager(drivers={Channel.EMAIL: driver}, policy=DeliveryPolicy())
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        mgr.submit(intent)
        receipts = driver.get_receipts("n1")
        assert len(receipts) == 1
        assert receipts[0].intent_id == "n1"
        assert receipts[0].status == ReceiptStatus.DELIVERED


class TestMailManagerPreferences:
    def test_channel_disabled_skips_recipient(self):
        driver = FakeMailDriver()
        mgr = MailManager(drivers={Channel.EMAIL: driver}, policy=DeliveryPolicy())
        prefs = NotificationPreferences(user_id="u1", email_enabled=False)
        mgr.set_preferences("u1", prefs)
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        mgr.submit(intent)
        assert len(driver.sent_intents) == 0

    def test_channel_enabled_sends(self):
        driver = FakeMailDriver()
        mgr = MailManager(drivers={Channel.EMAIL: driver}, policy=DeliveryPolicy())
        prefs = NotificationPreferences(user_id="u1", email_enabled=True)
        mgr.set_preferences("u1", prefs)
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        mgr.submit(intent)
        assert len(driver.sent_intents) == 1


class TestMailManagerPolicy:
    def test_policy_stored(self):
        policy = DeliveryPolicy(max_retries=5)
        driver = FakeMailDriver()
        mgr = MailManager(drivers={Channel.EMAIL: driver}, policy=policy)
        assert mgr.policy.max_retries == 5


class TestMailDriverProtocol:
    def test_fake_driver_implements_protocol(self):
        driver: MailDriver = FakeMailDriver()
        assert isinstance(driver, FakeMailDriver)
