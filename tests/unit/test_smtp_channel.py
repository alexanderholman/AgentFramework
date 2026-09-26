from __future__ import annotations

import smtplib
from unittest.mock import MagicMock

from af.communications.contracts import Channel, Delivery, DeliveryState, Route
from af.communications.smtp import SMTPChannel, SMTPConfig


def delivery():
    return Delivery(
        "a" * 64,
        "recipient",
        Route(Channel.MAIL, "alex@example.test", "smtp"),
        "en",
        "Report",
        "The report is ready.",
    )


def test_smtp_preserves_single_recipient_and_deterministic_message_id() -> None:
    client = MagicMock()
    client.send_message.return_value = {}
    channel = SMTPChannel(SMTPConfig("localhost", "sender@example.test"), factory=lambda: client)
    receipt = channel.send(delivery())
    message = client.send_message.call_args.args[0]
    assert message["To"] == "alex@example.test"
    assert str(message["Message-ID"]) == "<" + "a" * 64 + "@af.local>"
    assert client.send_message.call_args.kwargs["to_addrs"] == ["alex@example.test"]
    assert receipt.state is DeliveryState.ACCEPTED
    client.quit.assert_called_once()


def test_smtp_distinguishes_definite_temporary_rejection_from_unknown_acceptance() -> None:
    client = MagicMock()
    channel = SMTPChannel(SMTPConfig("localhost", "sender@example.test"), factory=lambda: client)
    client.send_message.side_effect = smtplib.SMTPDataError(451, b"temporary")
    assert channel.send(delivery()).state is DeliveryState.RETRYABLE_FAILURE
    client.send_message.side_effect = smtplib.SMTPServerDisconnected("disconnected after DATA")
    assert channel.send(delivery()).state is DeliveryState.UNKNOWN
    client.send_message.side_effect = smtplib.SMTPDataError(550, b"permanent")
    assert channel.send(delivery()).state is DeliveryState.REJECTED


def test_quit_failure_does_not_turn_acceptance_into_retry() -> None:
    client = MagicMock()
    client.send_message.return_value = {}
    client.quit.side_effect = OSError("disconnect")
    channel = SMTPChannel(SMTPConfig("localhost", "sender@example.test"), factory=lambda: client)
    assert channel.send(delivery()).state is DeliveryState.ACCEPTED


def test_connection_failure_is_safe_to_retry() -> None:
    def connect():
        raise ConnectionRefusedError("localhost")

    channel = SMTPChannel(SMTPConfig("localhost", "sender@example.test"), factory=connect)
    assert channel.send(delivery()).state is DeliveryState.RETRYABLE_FAILURE


def test_smtp_attachments_are_bounded_and_verified_before_connect() -> None:
    import hashlib

    from af.communications.contracts import Attachment
    from af.filesystem.contracts import FilesystemKey

    attachment = Attachment(
        "private",
        FilesystemKey("report.txt"),
        "report.txt",
        "text/plain",
        hashlib.sha256(b"data").hexdigest(),
    )
    base = delivery()
    request = Delivery(
        base.delivery_id,
        base.recipient_id,
        base.route,
        base.locale,
        base.subject,
        base.body,
        (attachment,),
    )
    client = MagicMock()
    client.send_message.return_value = {}
    factory = MagicMock(return_value=client)
    channel = SMTPChannel(
        SMTPConfig("localhost", "sender@example.test"),
        factory=factory,
        attachments=lambda _: b"data",
    )
    assert channel.send(request).state is DeliveryState.ACCEPTED
    message = client.send_message.call_args.args[0]
    assert next(message.iter_attachments()).get_payload(decode=True) == b"data"
    factory.reset_mock()
    corrupt = SMTPChannel(
        SMTPConfig("localhost", "sender@example.test"),
        factory=factory,
        attachments=lambda _: b"wrong",
    )
    assert corrupt.send(request).state is DeliveryState.REJECTED
    factory.assert_not_called()


def test_smtp_tls_and_auth_are_explicit_and_credentials_do_not_appear_in_repr() -> None:
    from unittest.mock import patch

    import pytest

    config = SMTPConfig(
        "mail.example.test", "sender@example.test", username="user", password="secret"
    )
    assert "secret" not in repr(config)
    with pytest.raises(ValueError):
        SMTPConfig(
            "localhost", "sender@example.test", security="plain", username="user", password="secret"
        )
    with patch("af.communications.smtp.smtplib.SMTP") as smtp:
        smtp.return_value.send_message.return_value = {}
        assert SMTPChannel(config).send(delivery()).state is DeliveryState.ACCEPTED
        smtp.return_value.starttls.assert_called_once()
        smtp.return_value.login.assert_called_once_with("user", "secret")
    with patch("af.communications.smtp.smtplib.SMTP_SSL") as smtp:
        smtp.return_value.send_message.return_value = {}
        assert (
            SMTPChannel(
                SMTPConfig("mail.example.test", "sender@example.test", port=465, security="tls")
            )
            .send(delivery())
            .state
            is DeliveryState.ACCEPTED
        )


def test_smtp_recipient_rejection_and_authentication_failure_are_permanent() -> None:
    client = MagicMock()
    client.send_message.side_effect = smtplib.SMTPRecipientsRefused(
        {"alex@example.test": (550, b"no such recipient")}
    )
    channel = SMTPChannel(SMTPConfig("localhost", "sender@example.test"), factory=lambda: client)
    assert channel.send(delivery()).state is DeliveryState.REJECTED

    def auth_failure():
        raise smtplib.SMTPAuthenticationError(535, b"invalid credentials")

    channel = SMTPChannel(SMTPConfig("localhost", "sender@example.test"), factory=auth_failure)
    assert channel.send(delivery()).state is DeliveryState.REJECTED


def test_filesystem_attachment_loader_verifies_bytes_and_size(tmp_path) -> None:
    import hashlib
    from io import BytesIO

    import pytest

    from af.communications.contracts import Attachment
    from af.communications.smtp import FilesystemAttachmentLoader
    from af.filesystem.contracts import FilesystemKey
    from af.filesystem.manager import FilesystemManager
    from af.filesystem.memory import memory_filesystem_registration

    manager = FilesystemManager(
        connections={"private": {"driver": "memory"}},
        default="private",
        deployment_profile="test",
        policy_version="v1",
    )
    manager.install(memory_filesystem_registration())
    key = FilesystemKey("report")
    manager.write(key, BytesIO(b"report"))
    attachment = Attachment(
        "private", key, "report.txt", "text/plain", hashlib.sha256(b"report").hexdigest()
    )
    assert FilesystemAttachmentLoader(manager)(attachment) == b"report"
    with pytest.raises(ValueError):
        FilesystemAttachmentLoader(manager, max_bytes=3)(attachment)
    manager.close()


def test_smtp_configuration_and_unconfigured_attachments_fail_before_connection() -> None:
    import pytest

    from af.communications.contracts import Attachment
    from af.filesystem.contracts import FilesystemKey

    for options in (
        {"security": "wrong"},
        {"port": 0},
        {"timeout_seconds": 0},
        {"max_attachment_bytes": -1},
        {"username": "only"},
    ):
        with pytest.raises(ValueError):
            SMTPConfig("localhost", "sender@example.test", **options)
    client = MagicMock()
    factory = MagicMock(return_value=client)
    base = delivery()
    request = Delivery(
        base.delivery_id,
        base.recipient_id,
        base.route,
        base.locale,
        base.subject,
        base.body,
        (Attachment("disk", FilesystemKey("key"), "file", "text/plain", "a" * 64),),
    )
    channel = SMTPChannel(SMTPConfig("localhost", "sender@example.test"), factory=factory)
    assert channel.send(request).state is DeliveryState.REJECTED
    chat = Delivery(
        base.delivery_id,
        base.recipient_id,
        Route(Channel.CHAT, "chat:a", "smtp"),
        base.locale,
        base.subject,
        base.body,
    )
    assert channel.send(chat).state is DeliveryState.REJECTED
    factory.assert_not_called()
