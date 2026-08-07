"""Unit tests for af.mail driver implementations."""

import pytest
from af.mail.fakes import FakeMailDriver, FakeSmtpDriver, FakeCompositeDriver
from af.mail.contracts import (
    Channel,
    Recipient,
    NotificationIntent,
    DeliveryReceipt,
    ReceiptStatus,
    BounceType,
)


class TestFakeMailDriver:
    def test_send_records_intent(self):
        driver = FakeMailDriver()
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        driver.send(intent)
        assert len(driver.sent_intents) == 1
        assert driver.sent_intents[0].id == "n1"

    def test_send_returns_receipt(self):
        driver = FakeMailDriver()
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        receipt = driver.send(intent)
        assert receipt.status == ReceiptStatus.DELIVERED
        assert receipt.intent_id == "n1"
        assert receipt.recipient_id == "u1"

    def test_get_receipts(self):
        driver = FakeMailDriver()
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        driver.send(intent)
        receipts = driver.get_receipts("n1")
        assert len(receipts) == 1
        assert receipts[0].intent_id == "n1"

    def test_get_receipts_empty(self):
        driver = FakeMailDriver()
        receipts = driver.get_receipts("nonexistent")
        assert len(receipts) == 0

    def test_multiple_sends(self):
        driver = FakeMailDriver()
        for i in range(3):
            intent = NotificationIntent(
                id=f"n{i}",
                subject="Hi",
                body="Hello",
                recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
            )
            driver.send(intent)
        assert len(driver.sent_intents) == 3


class TestFakeSmtpDriver:
    def test_send_records_as_smtp(self):
        driver = FakeSmtpDriver()
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        receipt = driver.send(intent)
        assert receipt.status == ReceiptStatus.DELIVERED
        assert receipt.channel == Channel.EMAIL

    def test_get_receipts(self):
        driver = FakeSmtpDriver()
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        driver.send(intent)
        receipts = driver.get_receipts("n1")
        assert len(receipts) == 1


class TestFakeCompositeDriver:
    def test_route_to_sub_driver(self):
        email_driver = FakeMailDriver()
        driver = FakeCompositeDriver({Channel.EMAIL: email_driver})
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        receipt = driver.send(intent)
        assert receipt.status == ReceiptStatus.DELIVERED
        assert len(email_driver.sent_intents) == 1

    def test_error_no_sub_driver(self):
        driver = FakeCompositeDriver({})
        intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        with pytest.raises(RuntimeError, match="no driver"):
            driver.send(intent)

    def test_multiple_channels(self):
        email_driver = FakeMailDriver()
        sms_driver = FakeMailDriver()
        driver = FakeCompositeDriver(
            {
                Channel.EMAIL: email_driver,
                Channel.SMS: sms_driver,
            }
        )
        email_intent = NotificationIntent(
            id="n1",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u1", address="a@b.com", channel=Channel.EMAIL)],
        )
        sms_intent = NotificationIntent(
            id="n2",
            subject="Hi",
            body="Hello",
            recipients=[Recipient(id="u2", address="+1234", channel=Channel.SMS)],
        )
        driver.send(email_intent)
        driver.send(sms_intent)
        assert len(email_driver.sent_intents) == 1
        assert len(sms_driver.sent_intents) == 1
