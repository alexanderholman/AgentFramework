"""Versioned communication intents and channel-neutral delivery values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from zoneinfo import ZoneInfo

from af.filesystem.contracts import FilesystemKey
from af.messaging.envelopes import PolicyContext


def bounded(value: str, *, limit: int = 1024) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > limit
        or re.search(r"[\x00-\x1f\x7f]", value)
    ):
        raise ValueError("Communication header must be bounded single-line text")


class Channel(StrEnum):
    MAIL = "mail"
    SMS = "sms"
    CHAT = "chat"
    WEBHOOK = "webhook"
    PUSH = "push"
    DATABASE = "database"
    REALTIME = "realtime"


class DeliveryState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    RETRYABLE_FAILURE = "retryable-failure"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    BOUNCED = "bounced"
    COMPLAINT = "complaint"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class QuietHours:
    start_hour: int
    end_hour: int
    timezone: str

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or not 0 <= value <= 23
            for value in (self.start_hour, self.end_hour)
        ):
            raise ValueError("Quiet hours must be clock hours")
        if self.start_hour == self.end_hour:
            raise ValueError("Quiet hours must leave a delivery window")
        ZoneInfo(self.timezone)

    def next_allowed(self, now: datetime) -> datetime:
        if now.utcoffset() is None:
            raise ValueError("Notification clock must be timezone-aware")
        zone = ZoneInfo(self.timezone)
        candidate = now.astimezone(UTC)
        # Walk actual instants, handling both DST gaps and repeated local hours.
        for _ in range(1501):
            hour = candidate.astimezone(zone).hour
            quiet = (
                (self.start_hour <= hour < self.end_hour)
                if self.start_hour < self.end_hour
                else (hour >= self.start_hour or hour < self.end_hour)
            )
            if not quiet:
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError("No bounded quiet-hour delivery window")


@dataclass(frozen=True, slots=True, repr=False)
class Route:
    channel: Channel
    address: str
    driver: str

    def __post_init__(self) -> None:
        if not isinstance(self.channel, Channel):
            raise ValueError("Invalid notification channel")
        bounded(self.address)
        bounded(self.driver, limit=128)


@dataclass(frozen=True, slots=True, repr=False)
class Recipient:
    recipient_id: str
    locale: str
    routes: tuple[Route, ...]
    consented_channels: frozenset[Channel]
    quiet_hours: QuietHours | None = None

    def __post_init__(self) -> None:
        bounded(self.recipient_id, limit=128)
        bounded(self.locale, limit=64)
        object.__setattr__(self, "routes", tuple(self.routes))
        object.__setattr__(self, "consented_channels", frozenset(self.consented_channels))
        if len({route.channel for route in self.routes}) != len(self.routes):
            raise ValueError("Recipient may have only one route per channel")


@dataclass(frozen=True, slots=True, repr=False)
class Attachment:
    disk: str
    key: FilesystemKey
    filename: str
    content_type: str
    checksum_sha256: str

    def __post_init__(self) -> None:
        bounded(self.disk, limit=128)
        bounded(self.filename, limit=255)
        bounded(self.content_type, limit=128)
        if (
            not isinstance(self.key, FilesystemKey)
            or re.fullmatch(r"[0-9a-f]{64}", self.checksum_sha256) is None
        ):
            raise ValueError("Attachment requires a logical key and SHA-256 digest")
        if "/" not in self.content_type:
            raise ValueError("Attachment content type is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class Template:
    subject: str
    body: str

    def __post_init__(self) -> None:
        bounded(self.subject, limit=998)
        if type(self.body) is not str or len(self.body) > 1_000_000:
            raise ValueError("Notification body exceeds its bound")


@dataclass(frozen=True, slots=True, repr=False)
class NotificationIntent:
    intent_id: str
    templates: Mapping[str, Template]
    parameters: Mapping[str, str]
    channels: tuple[Channel, ...]
    policy: PolicyContext
    correlation_id: str
    attachments: tuple[Attachment, ...] = ()
    fallback_locale: str = "en"

    def __post_init__(self) -> None:
        bounded(self.intent_id, limit=128)
        bounded(self.correlation_id, limit=200)
        object.__setattr__(self, "templates", MappingProxyType(dict(self.templates)))
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        object.__setattr__(self, "channels", tuple(self.channels))
        object.__setattr__(self, "attachments", tuple(self.attachments))
        if not self.channels or any(not isinstance(channel, Channel) for channel in self.channels):
            raise ValueError("Notification requires valid channels")
        if not isinstance(self.policy, PolicyContext) or self.fallback_locale not in self.templates:
            raise ValueError("Notification requires policy evidence and a fallback template")


@dataclass(frozen=True, slots=True, repr=False)
class Delivery:
    delivery_id: str
    recipient_id: str
    route: Route
    locale: str
    subject: str
    body: str
    attachments: tuple[Attachment, ...] = ()

    def __post_init__(self) -> None:
        for value in (self.delivery_id, self.recipient_id, self.locale, self.subject):
            bounded(value)
        if type(self.body) is not str or len(self.body) > 1_000_000:
            raise ValueError("Notification body exceeds its bound")


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    delivery_id: str
    state: DeliveryState
    provider_id: str | None = None


class DeliveryDeferred(Exception):
    """Current quiet hours require a later queue attempt."""

    def __init__(self, until: datetime) -> None:
        super().__init__("Notification deferred by recipient quiet hours")
        self.until = until
