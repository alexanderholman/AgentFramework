"""SMTP submission with explicit acceptance/uncertainty and bounded attachments."""

from __future__ import annotations

import hashlib
import smtplib
import ssl
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.message import EmailMessage
from typing import Literal, Protocol

from af.communications.contracts import (
    Attachment,
    Channel,
    Delivery,
    DeliveryReceipt,
    DeliveryState,
    bounded,
)
from af.filesystem.manager import FilesystemManager


class SMTPConnection(Protocol):
    def send_message(
        self, msg: EmailMessage, *, from_addr: str, to_addrs: list[str]
    ) -> Mapping[str, tuple[int, bytes]]: ...
    def quit(self) -> object: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SMTPConfig:
    host: str
    sender: str
    port: int = 587
    security: Literal["starttls", "tls", "plain"] = "starttls"
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    timeout_seconds: float = 30.0
    max_attachment_bytes: int = 20 * 1024 * 1024

    def __post_init__(self) -> None:
        bounded(self.host, limit=253)
        bounded(self.sender)
        Address(addr_spec=self.sender)
        if self.security not in {"starttls", "tls", "plain"}:
            raise ValueError("Invalid SMTP transport security")
        if (
            type(self.port) is not int
            or not 1 <= self.port <= 65535
            or not 0 < self.timeout_seconds <= 300
        ):
            raise ValueError("Invalid SMTP endpoint limits")
        if type(self.max_attachment_bytes) is not int or self.max_attachment_bytes < 0:
            raise ValueError("Invalid SMTP attachment limit")
        if (self.username is None) != (self.password is None):
            raise ValueError("SMTP authentication requires username and password")
        if self.username is not None and self.security == "plain":
            raise ValueError("SMTP credentials require TLS")


class FilesystemAttachmentLoader:
    def __init__(self, filesystem: FilesystemManager, *, max_bytes: int = 20 * 1024 * 1024) -> None:
        self._filesystem = filesystem
        self._max_bytes = max_bytes

    def __call__(self, attachment: Attachment) -> bytes:
        with self._filesystem.read(attachment.key, name=attachment.disk) as stream:
            data = stream.read(self._max_bytes + 1)
        if (
            len(data) > self._max_bytes
            or hashlib.sha256(data).hexdigest() != attachment.checksum_sha256
        ):
            raise ValueError("Notification attachment limit or integrity check failed")
        return data


class SMTPChannel:
    """One message per recipient; ACCEPTED means SMTP submission, not final delivery."""

    def __init__(
        self,
        config: SMTPConfig,
        *,
        factory: Callable[[], SMTPConnection] | None = None,
        attachments: Callable[[Attachment], bytes] | None = None,
    ) -> None:
        self._config = config
        self._factory = factory if factory is not None else self._connect
        self._attachments = attachments

    def _connect(self) -> SMTPConnection:
        config = self._config
        client = (
            smtplib.SMTP_SSL(
                config.host,
                config.port,
                timeout=config.timeout_seconds,
                context=ssl.create_default_context(),
            )
            if config.security == "tls"
            else smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds)
        )
        try:
            if config.security == "starttls":
                client.starttls(context=ssl.create_default_context())
            if config.username is not None and config.password is not None:
                client.login(config.username, config.password)
            return client
        except BaseException:
            client.close()
            raise

    def _message(self, delivery: Delivery) -> EmailMessage:
        if delivery.route.channel is not Channel.MAIL:
            raise ValueError("SMTP only supports the mail channel")
        Address(addr_spec=delivery.route.address)
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = delivery.route.address
        message["Subject"] = delivery.subject
        message["Message-ID"] = f"<{delivery.delivery_id}@af.local>"
        message["Content-Language"] = delivery.locale
        message.set_content(delivery.body)
        total = 0
        for attachment in delivery.attachments:
            if self._attachments is None:
                raise ValueError("SMTP attachment loading is not configured")
            data = self._attachments(attachment)
            total += len(data)
            if (
                total > self._config.max_attachment_bytes
                or hashlib.sha256(data).hexdigest() != attachment.checksum_sha256
            ):
                raise ValueError("SMTP attachment integrity or size limit failed")
            maintype, subtype = attachment.content_type.split("/", 1)
            message.add_attachment(
                data, maintype=maintype, subtype=subtype, filename=attachment.filename
            )
        return message

    def send(self, delivery: Delivery) -> DeliveryReceipt:
        try:
            message = self._message(delivery)
        except (ValueError, TypeError):
            return DeliveryReceipt(delivery.delivery_id, DeliveryState.REJECTED)
        try:
            client = self._factory()
        except smtplib.SMTPResponseException as error:
            return DeliveryReceipt(delivery.delivery_id, _response_state(error.smtp_code))
        except (OSError, smtplib.SMTPException):
            return DeliveryReceipt(delivery.delivery_id, DeliveryState.RETRYABLE_FAILURE)
        try:
            refused = client.send_message(
                message, from_addr=self._config.sender, to_addrs=[delivery.route.address]
            )
            if refused:
                state = _response_state(next(iter(refused.values()))[0])
                return DeliveryReceipt(delivery.delivery_id, state)
            return DeliveryReceipt(
                delivery.delivery_id, DeliveryState.ACCEPTED, str(message["Message-ID"])
            )
        except smtplib.SMTPRecipientsRefused as error:
            codes = [value[0] for value in error.recipients.values()]
            state = (
                DeliveryState.RETRYABLE_FAILURE
                if codes and all(400 <= code < 500 for code in codes)
                else DeliveryState.REJECTED
            )
            return DeliveryReceipt(delivery.delivery_id, state)
        except smtplib.SMTPResponseException as error:
            return DeliveryReceipt(delivery.delivery_id, _response_state(error.smtp_code))
        except (OSError, smtplib.SMTPException):
            return DeliveryReceipt(delivery.delivery_id, DeliveryState.UNKNOWN)
        finally:
            with suppress(OSError, smtplib.SMTPException):
                client.quit()
            with suppress(OSError, smtplib.SMTPException):
                client.close()


def _response_state(code: int) -> DeliveryState:
    return DeliveryState.RETRYABLE_FAILURE if 400 <= code < 500 else DeliveryState.REJECTED
