from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from af.communications.contracts import (
    Channel,
    DeliveryState,
    NotificationIntent,
    Recipient,
    Route,
    Template,
)
from af.communications.delivery import MailManager, NotificationService, RecordingChannel
from af.communications.store import SQLiteDeliveryStore
from af.messaging.envelopes import PolicyContext

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
POLICY = PolicyContext(
    policy_version="v1", decision_digest="a" * 64, effective_scope="notifications"
)


def make_service(path, recipients, authorize=lambda *_: True):
    mail, chat = RecordingChannel(), RecordingChannel()
    store = SQLiteDeliveryStore(path)
    manager = MailManager({"mail": mail, "chat": chat}, default="mail")
    service = NotificationService(
        channels=manager,
        store=store,
        authorize=authorize,
        recipients=lambda key: recipients[key],
        clock=lambda: NOW,
    )
    return service, mail, chat, store


def intent():
    return NotificationIntent(
        "intent-1",
        {
            "en": Template("Hello $name", "Report ready"),
            "fr": Template("Bonjour $name", "Rapport prêt"),
        },
        {"name": "Alex"},
        (Channel.MAIL, Channel.CHAT),
        POLICY,
        "correlation-1",
    )


def test_one_intent_routes_independent_recipients_and_retries_are_idempotent(
    tmp_path: Path,
) -> None:
    recipients = {
        "a": Recipient(
            "a", "en", (Route(Channel.MAIL, "a@example.test", "mail"),), frozenset({Channel.MAIL})
        ),
        "b": Recipient(
            "b", "fr", (Route(Channel.CHAT, "chat:b", "chat"),), frozenset({Channel.CHAT})
        ),
    }
    service, mail, chat, store = make_service(tmp_path / "receipts.sqlite", recipients)
    jobs = service.plan(intent(), tuple(recipients.values()))
    assert len(jobs) == 2
    receipts = [service.deliver(job) for job in jobs]
    assert all(receipt.state is DeliveryState.ACCEPTED for receipt in receipts)
    assert mail.deliveries[0].subject == "Hello Alex"
    assert chat.deliveries[0].subject == "Bonjour Alex"
    assert chat.deliveries[0].locale == "fr"
    assert jobs[1].envelope.metadata.correlation_id == "correlation-1"
    assert jobs[1].envelope.metadata.policy == POLICY
    assert service.deliver(jobs[0]) == receipts[0]
    assert len(mail.deliveries) == 1
    store.close()


def test_revoked_consent_prevents_queued_delivery(tmp_path: Path) -> None:
    original = Recipient(
        "a", "en", (Route(Channel.MAIL, "a@example.test", "mail"),), frozenset({Channel.MAIL})
    )
    recipients = {"a": original}
    service, mail, _, store = make_service(tmp_path / "receipts.sqlite", recipients)
    jobs = service.plan(intent(), (original,))
    recipients["a"] = Recipient("a", "en", original.routes, frozenset())
    assert service.deliver(jobs[0]).state is DeliveryState.SUPPRESSED
    assert mail.deliveries == []
    store.close()


def test_unauthorized_recipients_produce_no_jobs(tmp_path: Path) -> None:
    recipient = Recipient(
        "a", "en", (Route(Channel.MAIL, "a@example.test", "mail"),), frozenset({Channel.MAIL})
    )
    service, mail, _, store = make_service(
        tmp_path / "receipts.sqlite", {"a": recipient}, lambda *_: False
    )
    assert service.plan(intent(), (recipient,)) == ()
    assert mail.deliveries == []
    store.close()


def test_durable_receipts_survive_service_recreation(tmp_path: Path) -> None:
    recipient = Recipient(
        "a", "en", (Route(Channel.MAIL, "a@example.test", "mail"),), frozenset({Channel.MAIL})
    )
    path = tmp_path / "receipts.sqlite"
    first, _, _, first_store = make_service(path, {"a": recipient})
    job = first.plan(intent(), (recipient,))[0]
    receipt = first.deliver(job)
    first_store.close()
    second, mail, _, store = make_service(path, {"a": recipient})
    assert second.deliver(job) == receipt
    assert mail.deliveries == []
    store.close()


def test_reused_id_with_changed_payload_is_rejected(tmp_path: Path) -> None:
    recipient = Recipient(
        "a", "en", (Route(Channel.MAIL, "a@example.test", "mail"),), frozenset({Channel.MAIL})
    )
    service, _, _, store = make_service(tmp_path / "receipts.sqlite", {"a": recipient})
    job = service.plan(intent(), (recipient,))[0]
    service.deliver(job)
    altered = NotificationIntent(
        "intent-1",
        {"en": Template("Changed", "payload")},
        {},
        (Channel.MAIL,),
        POLICY,
        "correlation-1",
    )
    with pytest.raises(ValueError, match="idempotency"):
        service.deliver(service.plan(altered, (recipient,))[0])
    store.close()


def test_quiet_hours_defer_across_midnight_and_delivery_rechecks_current_window(
    tmp_path: Path,
) -> None:
    from af.communications.contracts import DeliveryDeferred, QuietHours

    quiet = QuietHours(11, 13, "UTC")
    recipient = Recipient(
        "a",
        "en",
        (Route(Channel.MAIL, "a@example.test", "mail"),),
        frozenset({Channel.MAIL}),
        quiet,
    )
    service, mail, _, store = make_service(tmp_path / "receipts.sqlite", {"a": recipient})
    job = service.plan(intent(), (recipient,))[0]
    assert job.delay.total_seconds() == 3600
    with pytest.raises(DeliveryDeferred) as error:
        service.deliver(job)
    assert error.value.until.hour == 13
    assert mail.deliveries == []
    across_midnight = QuietHours(22, 7, "Europe/London")
    # The autumn DST repeated hour still advances by actual UTC instants.
    assert across_midnight.next_allowed(datetime(2026, 10, 25, 0, tzinfo=UTC)) == datetime(
        2026, 10, 25, 7, tzinfo=UTC
    )
    store.close()


def test_failover_never_duplicates_a_possibly_accepted_send() -> None:
    from af.communications.contracts import Delivery
    from af.communications.delivery import FailoverChannel, RoundRobinChannel

    request = Delivery(
        "delivery", "a", Route(Channel.MAIL, "a@example.test", "mail"), "en", "Subject", "Body"
    )
    first = RecordingChannel(state=DeliveryState.UNKNOWN)
    second = RecordingChannel()
    assert FailoverChannel((first, second)).send(request).state is DeliveryState.UNKNOWN
    assert second.deliveries == []
    first.state = DeliveryState.RETRYABLE_FAILURE
    assert FailoverChannel((first, second)).send(request).state is DeliveryState.ACCEPTED
    first.state = DeliveryState.ACCEPTED
    robin = RoundRobinChannel((first, second))
    before = len(first.deliveries), len(second.deliveries)
    robin.send(request)
    robin.send(request)
    assert (len(first.deliveries), len(second.deliveries)) == (before[0] + 1, before[1] + 1)


def test_retryable_failure_reclaims_receipt_but_unknown_failure_does_not(tmp_path: Path) -> None:
    recipient = Recipient(
        "a", "en", (Route(Channel.MAIL, "a@example.test", "mail"),), frozenset({Channel.MAIL})
    )
    service, mail, _, store = make_service(tmp_path / "receipts.sqlite", {"a": recipient})
    job = service.plan(intent(), (recipient,))[0]
    mail.state = DeliveryState.RETRYABLE_FAILURE
    with pytest.raises(RuntimeError, match="temporarily"):
        service(job)
    mail.state = DeliveryState.UNKNOWN
    assert service.deliver(job).state is DeliveryState.UNKNOWN
    mail.state = DeliveryState.ACCEPTED
    assert service.deliver(job).state is DeliveryState.UNKNOWN
    assert len(mail.deliveries) == 2
    store.close()


def test_unknown_exception_is_recorded_without_provider_details(tmp_path: Path) -> None:
    class Broken:
        def send(self, delivery):
            raise ConnectionError("secret provider response")

    recipient = Recipient(
        "a", "en", (Route(Channel.MAIL, "a@example.test", "mail"),), frozenset({Channel.MAIL})
    )
    store = SQLiteDeliveryStore(tmp_path / "receipts.sqlite")
    service = NotificationService(
        channels=MailManager({"mail": Broken()}, default="mail"),
        store=store,
        authorize=lambda *_: True,
        recipients=lambda _: recipient,
        clock=lambda: NOW,
    )
    receipt = service.deliver(service.plan(intent(), (recipient,))[0])
    assert receipt.state is DeliveryState.UNKNOWN
    assert "secret" not in repr(receipt)
    store.close()


def test_feedback_changes_receipt_without_resending(tmp_path: Path) -> None:
    recipient = Recipient(
        "a", "en", (Route(Channel.MAIL, "a@example.test", "mail"),), frozenset({Channel.MAIL})
    )
    service, mail, _, store = make_service(tmp_path / "receipts.sqlite", {"a": recipient})
    job = service.plan(intent(), (recipient,))[0]
    service.deliver(job)
    store.record_feedback(job.envelope.metadata.message_id, DeliveryState.BOUNCED)
    assert service.deliver(job).state is DeliveryState.BOUNCED
    store.record_feedback(job.envelope.metadata.message_id, DeliveryState.COMPLAINT)
    assert service.deliver(job).state is DeliveryState.COMPLAINT
    assert len(mail.deliveries) == 1
    with pytest.raises(ValueError):
        store.record_feedback("missing", DeliveryState.BOUNCED)
    with pytest.raises(ValueError):
        store.record_feedback("missing", DeliveryState.ACCEPTED)
    store.close()


def test_attachment_refs_survive_queue_codec_and_delivery(tmp_path: Path) -> None:
    from af.communications.contracts import Attachment
    from af.filesystem.contracts import FilesystemKey
    from af.messaging.envelopes import JsonEnvelopeCodec
    from af.queue.contracts import QueueJob

    attachment = Attachment(
        "private", FilesystemKey("reports/result.pdf"), "result.pdf", "application/pdf", "a" * 64
    )
    recipient = Recipient(
        "a", "en", (Route(Channel.MAIL, "a@example.test", "mail"),), frozenset({Channel.MAIL})
    )
    service, mail, _, store = make_service(tmp_path / "receipts.sqlite", {"a": recipient})
    source = intent()
    with_attachment = NotificationIntent(
        source.intent_id,
        source.templates,
        source.parameters,
        source.channels,
        source.policy,
        source.correlation_id,
        (attachment,),
    )
    job = service.plan(with_attachment, (recipient,))[0]
    codec = JsonEnvelopeCodec()
    restored = QueueJob(envelope=codec.decode(codec.encode(job.envelope)))
    service(restored)
    assert mail.deliveries[0].attachments == (attachment,)
    store.close()


def test_sqlite_claim_is_exclusive_across_independent_connections(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    first = SQLiteDeliveryStore(tmp_path / "receipts.sqlite")
    second = SQLiteDeliveryStore(tmp_path / "receipts.sqlite")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda store: store.claim("id", "digest"), (first, second)))
    assert results.count(None) == 1
    assert next(item for item in results if item is not None).state is DeliveryState.PENDING
    first.close()
    second.close()


def test_notification_enqueue_preserves_distinct_jobs_and_rejects_duplicate_recipient_ids(
    tmp_path: Path,
) -> None:
    from unittest.mock import MagicMock

    recipient = Recipient(
        "a", "en", (Route(Channel.MAIL, "a@example.test", "mail"),), frozenset({Channel.MAIL})
    )
    service, _, _, store = make_service(tmp_path / "receipts.sqlite", {"a": recipient})
    queue = MagicMock()
    service.enqueue(intent(), (recipient,), queue)
    queued = queue.dispatch.call_args.args[0]
    assert queued.envelope.metadata.policy == POLICY
    with pytest.raises(ValueError, match="unique"):
        service.plan(intent(), (recipient, recipient))
    store.close()


def test_invalid_transport_payloads_fail_before_any_channel_send(tmp_path: Path) -> None:
    from dataclasses import replace

    from af.messaging.envelopes import MessageEnvelope
    from af.queue.contracts import QueueJob

    recipient = Recipient(
        "a", "en", (Route(Channel.MAIL, "a@example.test", "mail"),), frozenset({Channel.MAIL})
    )
    service, mail, _, store = make_service(tmp_path / "receipts.sqlite", {"a": recipient})
    job = service.plan(intent(), (recipient,))[0]
    payload = job.envelope.payload_copy()
    variants = [None, {}, {**payload, "delivery_id": "changed"}, {**payload, "attachments": [{}]}]
    for variant in variants:
        bad = QueueJob(
            envelope=MessageEnvelope.create(metadata=job.envelope.metadata, payload=variant)
        )
        with pytest.raises(ValueError):
            service.deliver(bad)
    bad_metadata = replace(job.envelope.metadata, schema_version=2)
    with pytest.raises(ValueError):
        service.deliver(
            QueueJob(envelope=MessageEnvelope.create(metadata=bad_metadata, payload=payload))
        )
    assert mail.deliveries == []
    store.close()


def test_header_injection_and_invalid_recipient_routes_are_rejected() -> None:
    from af.communications.contracts import Attachment, Delivery, QuietHours
    from af.communications.delivery import FailoverChannel, RoundRobinChannel
    from af.filesystem.contracts import FilesystemKey

    attempts = [
        lambda: Route(Channel.MAIL, "a@example.test\r\nBcc:other@example.test", "mail"),
        lambda: Route("not-channel", "target", "mail"),
        lambda: Recipient(
            "a",
            "en",
            (
                Route(Channel.MAIL, "one@example.test", "mail"),
                Route(Channel.MAIL, "two@example.test", "mail"),
            ),
            frozenset({Channel.MAIL}),
        ),
        lambda: QuietHours(22, 22, "UTC"),
        lambda: QuietHours(24, 7, "UTC"),
        lambda: Attachment("disk", FilesystemKey("key"), "file", "invalid", "a" * 64),
        lambda: Attachment("disk", FilesystemKey("key"), "file", "text/plain", "invalid"),
        lambda: Template("Subject", "x" * 1_000_001),
        lambda: Delivery(
            "id",
            "a",
            Route(Channel.MAIL, "a@example.test", "mail"),
            "en",
            "Subject",
            "x" * 1_000_001,
        ),
        lambda: NotificationIntent("id", {}, {}, (Channel.MAIL,), POLICY, "correlation"),
        lambda: NotificationIntent(
            "id", {"en": Template("Sub", "Body")}, {}, (), POLICY, "correlation"
        ),
        lambda: MailManager({}, default="missing"),
        lambda: FailoverChannel(()),
        lambda: RoundRobinChannel(()),
    ]
    for attempt in attempts:
        with pytest.raises(ValueError):
            attempt()
    with pytest.raises(ValueError):
        QuietHours(22, 7, "UTC").next_allowed(datetime(2026, 1, 1))


def test_quiet_hours_delay_measures_actual_elapsed_time_across_dst() -> None:
    from zoneinfo import ZoneInfo

    from af.communications.contracts import QuietHours

    now = datetime(2026, 10, 25, 0, 30, tzinfo=ZoneInfo("Europe/London"))
    allowed = QuietHours(22, 7, "Europe/London").next_allowed(now)
    assert (allowed - now).total_seconds() == 7.5 * 3600
