"""Deterministic recipient routing and queue-backed delivery with current policy checks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from string import Template as StringTemplate
from threading import RLock
from typing import Protocol, cast

from af.communications.contracts import (
    Attachment,
    Channel,
    Delivery,
    DeliveryDeferred,
    DeliveryReceipt,
    DeliveryState,
    NotificationIntent,
    Recipient,
    Route,
)
from af.communications.store import DeliveryStore
from af.filesystem.contracts import FilesystemKey
from af.messaging.envelopes import EnvelopeKind, MessageEnvelope, MessageMetadata, PolicyContext
from af.queue.contracts import QueueDispatchReceipt, QueueJob


class ChannelDriver(Protocol):
    def send(self, delivery: Delivery) -> DeliveryReceipt: ...


class NotificationQueue(Protocol):
    def dispatch(self, job: QueueJob, /) -> QueueDispatchReceipt: ...


class MailManager:
    """Explicit named mailers/channels. No implicit provider or remote fallback."""

    def __init__(self, drivers: Mapping[str, ChannelDriver], *, default: str) -> None:
        if default not in drivers:
            raise ValueError("Default communication driver is not registered")
        self._drivers = dict(drivers)
        self.default = default

    def connection(self, name: str | None = None) -> ChannelDriver:
        return self._drivers[name or self.default]

    def send(self, delivery: Delivery) -> DeliveryReceipt:
        result = self.connection(delivery.route.driver).send(delivery)
        if result.delivery_id != delivery.delivery_id or result.state is DeliveryState.PENDING:
            raise ValueError("Invalid channel delivery receipt")
        return result


class RecordingChannel:
    """A no-network channel fake, recording the exact immutable rendered delivery."""

    def __init__(self, *, state: DeliveryState = DeliveryState.ACCEPTED) -> None:
        self.deliveries: list[Delivery] = []
        self.state = state

    def send(self, delivery: Delivery) -> DeliveryReceipt:
        self.deliveries.append(delivery)
        return DeliveryReceipt(delivery.delivery_id, self.state)


class FailoverChannel:
    """Fail over only after definite non-acceptance; ambiguous sends never fail over."""

    def __init__(self, drivers: tuple[ChannelDriver, ...]) -> None:
        if not drivers:
            raise ValueError("Failover requires communication drivers")
        self._drivers = drivers

    def send(self, delivery: Delivery) -> DeliveryReceipt:
        for driver in self._drivers:
            result = driver.send(delivery)
            if result.state is not DeliveryState.RETRYABLE_FAILURE:
                return result
        return result


class RoundRobinChannel:
    def __init__(self, drivers: tuple[ChannelDriver, ...]) -> None:
        if not drivers:
            raise ValueError("Round-robin requires communication drivers")
        self._drivers = drivers
        self._next = 0
        self._lock = RLock()

    def send(self, delivery: Delivery) -> DeliveryReceipt:
        with self._lock:
            selected = self._drivers[self._next]
            self._next = (self._next + 1) % len(self._drivers)
        return selected.send(delivery)


class NotificationService:
    def __init__(
        self,
        *,
        channels: MailManager,
        store: DeliveryStore,
        authorize: Callable[[Recipient, Route, PolicyContext], bool],
        recipients: Callable[[str], Recipient],
        clock: Callable[[], datetime],
    ) -> None:
        self._channels = channels
        self._store = store
        self._authorize = authorize
        self._recipients = recipients
        self._clock = clock

    def plan(
        self, intent: NotificationIntent, recipients: tuple[Recipient, ...]
    ) -> tuple[QueueJob, ...]:
        now = self._clock()
        jobs: list[QueueJob] = []
        if len({recipient.recipient_id for recipient in recipients}) != len(recipients):
            raise ValueError("Notification recipients must be unique")
        for recipient in recipients:
            locale = (
                recipient.locale if recipient.locale in intent.templates else intent.fallback_locale
            )
            template = intent.templates[locale]
            subject = StringTemplate(template.subject).substitute(intent.parameters)
            body = StringTemplate(template.body).substitute(intent.parameters)
            for route in recipient.routes:
                if (
                    route.channel not in intent.channels
                    or route.channel not in recipient.consented_channels
                ):
                    continue
                if not self._authorize(recipient, route, intent.policy):
                    continue
                self._channels.connection(route.driver)  # Fail before enqueuing unsupported routes.
                key = hashlib.sha256(
                    json.dumps(
                        [intent.intent_id, recipient.recipient_id, route.channel.value]
                    ).encode()
                ).hexdigest()
                delivery = Delivery(
                    key, recipient.recipient_id, route, locale, subject, body, intent.attachments
                )
                payload = _payload(delivery)
                envelope = MessageEnvelope.create(
                    metadata=MessageMetadata(
                        message_id=key,
                        message_type="notification.deliver",
                        schema_version=1,
                        kind=EnvelopeKind.JOB,
                        occurred_at=now,
                        correlation_id=intent.correlation_id,
                        policy=intent.policy,
                    ),
                    payload=payload,
                )
                allowed = recipient.quiet_hours.next_allowed(now) if recipient.quiet_hours else now
                jobs.append(
                    QueueJob(
                        envelope=envelope,
                        lane="notifications",
                        unique_key=key,
                        delay=allowed - now if allowed > now else None,
                        max_attempts=5,
                        backoff=timedelta(minutes=5),
                    )
                )
        return tuple(jobs)

    def enqueue(
        self,
        intent: NotificationIntent,
        recipients: tuple[Recipient, ...],
        queue: NotificationQueue,
    ) -> tuple[QueueDispatchReceipt, ...]:
        jobs = self.plan(intent, recipients)
        return tuple(queue.dispatch(job) for job in jobs)

    def deliver(self, job: QueueJob) -> DeliveryReceipt:
        metadata = job.envelope.metadata
        if (
            metadata.message_type != "notification.deliver"
            or metadata.schema_version != 1
            or metadata.policy is None
        ):
            raise ValueError("Invalid versioned notification delivery job")
        delivery = _delivery(job.envelope.payload_copy())
        if delivery.delivery_id != metadata.message_id:
            raise ValueError("Notification delivery identity mismatch")
        recipient = self._recipients(delivery.recipient_id)
        authorized = (
            recipient.recipient_id == delivery.recipient_id
            and delivery.route in recipient.routes
            and delivery.route.channel in recipient.consented_channels
            and self._authorize(recipient, delivery.route, metadata.policy)
        )
        now = self._clock()
        if authorized and recipient.quiet_hours:
            allowed = recipient.quiet_hours.next_allowed(now)
            if allowed > now:
                raise DeliveryDeferred(allowed)
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "payload": _payload(delivery),
                    "policy": {
                        "version": metadata.policy.policy_version,
                        "digest": metadata.policy.decision_digest,
                        "scope": metadata.policy.effective_scope,
                        "tenant": metadata.policy.tenant_ref,
                    },
                    "correlation": metadata.correlation_id,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        existing = self._store.claim(delivery.delivery_id, fingerprint)
        if existing is not None:
            return existing
        if not authorized:
            receipt = DeliveryReceipt(delivery.delivery_id, DeliveryState.SUPPRESSED)
        else:
            try:
                receipt = self._channels.send(delivery)
            except Exception:
                # A provider exception can follow acceptance. Never blindly resend.
                receipt = DeliveryReceipt(delivery.delivery_id, DeliveryState.UNKNOWN)
        self._store.complete(receipt)
        return receipt

    def __call__(self, job: QueueJob) -> None:
        receipt = self.deliver(job)
        if receipt.state is DeliveryState.RETRYABLE_FAILURE:
            raise RuntimeError("Notification provider temporarily rejected delivery")


def _payload(delivery: Delivery) -> dict[str, object]:
    return {
        "delivery_id": delivery.delivery_id,
        "recipient_id": delivery.recipient_id,
        "channel": delivery.route.channel.value,
        "address": delivery.route.address,
        "driver": delivery.route.driver,
        "locale": delivery.locale,
        "subject": delivery.subject,
        "body": delivery.body,
        "attachments": [
            {
                "disk": value.disk,
                "key": value.key.value,
                "filename": value.filename,
                "content_type": value.content_type,
                "checksum_sha256": value.checksum_sha256,
            }
            for value in delivery.attachments
        ],
    }


def _delivery(payload: object) -> Delivery:
    if not isinstance(payload, dict):
        raise ValueError("Invalid notification delivery payload")
    data = cast(dict[str, object], payload)
    fields = (
        "delivery_id",
        "recipient_id",
        "channel",
        "address",
        "driver",
        "locale",
        "subject",
        "body",
    )
    if any(type(data.get(key)) is not str for key in fields) or not isinstance(
        data.get("attachments"), list
    ):
        raise ValueError("Invalid notification delivery payload")
    strings = {key: cast(str, data[key]) for key in fields}
    attachments: list[Attachment] = []
    for item in cast(list[object], data["attachments"]):
        if not isinstance(item, dict) or any(
            type(item.get(key)) is not str
            for key in ("disk", "key", "filename", "content_type", "checksum_sha256")
        ):
            raise ValueError("Invalid notification attachment")
        attachments.append(
            Attachment(
                item["disk"],
                FilesystemKey(item["key"]),
                item["filename"],
                item["content_type"],
                item["checksum_sha256"],
            )
        )
    return Delivery(
        strings["delivery_id"],
        strings["recipient_id"],
        Route(Channel(strings["channel"]), strings["address"], strings["driver"]),
        strings["locale"],
        strings["subject"],
        strings["body"],
        tuple(attachments),
    )
