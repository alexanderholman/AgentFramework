"""MailManager – orchestrates notification intent delivery.

Follows the AF Manager pattern: thin composition root that delegates to
typed drivers, enforces idempotency, applies preferences/policy, and
emits delivery receipts.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Optional

from .contracts import (
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


# ---------------------------------------------------------------------------
# Driver protocol (structural, no ABC overhead)
# ---------------------------------------------------------------------------


class MailDriver:
    """Base class that drivers inherit from for type clarity.

    Concrete drivers must implement `send` and `close`.
    """

    name: str = "base"

    def send(
        self,
        intent: NotificationIntent,
        recipient: Recipient,
        policy: DeliveryPolicy,
    ) -> DeliveryReceipt:
        raise NotImplementedError

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class MailManager:
    """Deterministic mail/notification delivery orchestrator.

    Responsibilities:
    - Maintain a registry of named MailDriver instances.
    - Resolve the best channel per recipient using preferences.
    - Enforce idempotency keys to prevent duplicate delivery.
    - Queue delivery jobs through the queue manager integration.
    - Collect and attach DeliveryReceipt records.
    """

    def __init__(
        self,
        drivers: Optional[Mapping[str, MailDriver]] = None,
        policy: Optional[DeliveryPolicy] = None,
    ) -> None:
        self._drivers: dict[str, MailDriver] = dict(drivers or {})
        self._policy: DeliveryPolicy = policy or DeliveryPolicy()
        self._idempotency_cache: dict[str, DeliveryReceipt] = {}

    # -- public API ---------------------------------------------------------

    def register_driver(self, name: str, driver: MailDriver) -> None:
        """Register a named driver for later resolution."""
        self._drivers[name] = driver

    def get_driver(self, name: str) -> Optional[MailDriver]:
        """Look up a registered driver by name."""
        return self._drivers.get(name)

    def list_drivers(self) -> list[str]:
        """Return sorted list of registered driver names."""
        return sorted(self._drivers.keys())

    def send(
        self,
        intent: NotificationIntent,
        preferences: Optional[NotificationPreferences] = None,
        driver_name: Optional[str] = None,
    ) -> list[DeliveryReceipt]:
        """Process an intent and return per-recipient receipts.

        Flow:
        1. Idempotency check – if key already processed, return cached receipt.
        2. Filter recipients by consent and channel preferences.
        3. Resolve driver for each recipient.
        4. Delegate send to driver, collect receipt.
        5. Cache idempotency result.
        """
        prefs = preferences or NotificationPreferences()
        receipts: list[DeliveryReceipt] = []

        # Idempotency guard
        if intent.idempotency_key:
            if intent.idempotency_key in self._idempotency_cache:
                cached = self._idempotency_cache[intent.idempotency_key]
                intent.add_receipt(cached)
                return [cached]
            ttl_key = f"{intent.idempotency_key}:{int(datetime.now(timezone.utc).timestamp())}"
            self._idempotency_cache[ttl_key] = None  # type: ignore[assignment]

        for recipient in intent.recipients:
            receipt = self._deliver_to_recipient(intent, recipient, prefs, driver_name)
            receipts.append(receipt)
            intent.add_receipt(receipt)

        # Cache idempotency result
        if intent.idempotency_key and receipts:
            combined = receipts[0]
            self._idempotency_cache[intent.idempotency_key] = combined

        return receipts

    def queue_delivery(
        self,
        intent: NotificationIntent,
        preferences: Optional[NotificationPreferences] = None,
        driver_name: Optional[str] = None,
    ) -> str:
        """Enqueue intent for deferred delivery. Returns job ID."""
        job_id = uuid.uuid4().hex
        intent.metadata["job_id"] = job_id
        intent.metadata["driver_name"] = driver_name or ""
        intent.metadata["preferences"] = {
            "allowed_channels": [
                c.value for c in (preferences or NotificationPreferences()).allowed_channels
            ],
            "locale": (preferences or NotificationPreferences()).locale,
        }
        intent.status = IntentStatus.QUEUED
        return job_id

    def get_receipts(self, intent: NotificationIntent) -> list[DeliveryReceipt]:
        """Return all receipts attached to an intent."""
        return list(intent.receipts)

    def clear_idempotency_cache(self) -> None:
        """Clear the in-memory idempotency cache (useful for testing)."""
        self._idempotency_cache.clear()

    def close(self) -> None:
        """Close all registered drivers."""
        for driver in self._drivers.values():
            driver.close()

    # -- internal helpers ---------------------------------------------------

    def _deliver_to_recipient(
        self,
        intent: NotificationIntent,
        recipient: Recipient,
        prefs: NotificationPreferences,
        driver_name: Optional[str],
    ) -> DeliveryReceipt:
        """Deliver a single intent to a single recipient."""
        # Consent check
        if not recipient.consented:
            receipt = self._make_receipt(
                intent,
                recipient,
                recipient.channel,
                ReceiptStatus.SUPPRESSED,
                error_message="consent withdrawn",
            )
            return receipt

        # Channel preference check
        if recipient.channel not in prefs.allowed_channels:
            receipt = self._make_receipt(
                intent,
                recipient,
                recipient.channel,
                ReceiptStatus.SUPPRESSED,
                error_message="channel not allowed",
            )
            return receipt

        # Quiet hours check
        if self._is_quiet_hours(recipient, prefs):
            receipt = self._make_receipt(
                intent,
                recipient,
                recipient.channel,
                ReceiptStatus.DEFERRED,
                error_message="quiet hours",
            )
            return receipt

        # Resolve driver
        driver = self._resolve_driver(recipient.channel, driver_name)
        if driver is None:
            receipt = self._make_receipt(
                intent,
                recipient,
                recipient.channel,
                ReceiptStatus.FAILED,
                error_message="no driver available",
            )
            return receipt

        # Delegate to driver
        try:
            receipt = driver.send(intent, recipient, self._policy)
        except Exception as exc:
            receipt = self._make_receipt(
                intent, recipient, recipient.channel, ReceiptStatus.FAILED, error_message=str(exc)
            )

        return receipt

    def _resolve_driver(
        self,
        channel: Channel,
        explicit_name: Optional[str],
    ) -> Optional[MailDriver]:
        """Find the best driver for a channel."""
        if explicit_name and explicit_name in self._drivers:
            return self._drivers[explicit_name]

        # Try channel-matched driver names first
        for name, driver in self._drivers.items():
            if channel.value in name.lower():
                return driver

        # Fallback: first available driver
        if self._drivers:
            return next(iter(self._drivers.values()))

        return None

    def _is_quiet_hours(
        self,
        recipient: Recipient,
        prefs: NotificationPreferences,
    ) -> bool:
        """Check if current time falls within quiet hours."""
        if not prefs.quiet_hours_start or not prefs.quiet_hours_end:
            return False
        now = datetime.now(timezone.utc).strftime("%H:%M")
        start = prefs.quiet_hours_start.replace(":", ":")
        end = prefs.quiet_hours_end.replace(":", ":")
        # Simple same-day range check
        if start <= end:
            return start <= now <= end
        # Overnight range (e.g. 22:00 - 07:00)
        return now >= start or now <= end

    @staticmethod
    def _make_receipt(
        intent: NotificationIntent,
        recipient: Recipient,
        channel: Channel,
        status: ReceiptStatus,
        error_message: Optional[str] = None,
    ) -> DeliveryReceipt:
        """Construct a delivery receipt for an intent/recipient pair."""
        now = datetime.now(timezone.utc)
        receipt = DeliveryReceipt(
            intent_id=intent.id,
            recipient=recipient,
            channel=channel,
            status=status,
            attempted_at=now,
            error_message=error_message,
        )
        return receipt
