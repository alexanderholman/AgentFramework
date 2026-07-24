"""Durable single-owner relay for the transactional integration-event outbox."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Literal, Protocol

from af.framework.errors import PlatformReason
from af.framework.relational import DatabaseDriver, DatabaseOperationError, DatabaseStatement
from af.messaging.envelopes import MessageEnvelope
from af.messaging.outbox import OutboxRecord, TransactionalOutbox

__all__ = (
    "OutboxBatchReport",
    "OutboxPublishError",
    "OutboxPublisher",
    "OutboxRelay",
    "OutboxRelayError",
    "OutboxRelayIntegrityError",
    "OutboxRelayLease",
    "OutboxRelayLeaseUnavailable",
    "OutboxRetryPolicy",
)

_LOCK_TABLE = "af_outbox_relay_locks"
_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_MAX_BATCH = 1_000
_MAX_ATTEMPTS = 1_000
_MAX_LEASE = timedelta(hours=24)
_MAX_RETRY_DELAY = timedelta(days=7)


class OutboxPublisher(Protocol):
    """One explicit transport boundary used by the relay."""

    def publish(self, envelope: MessageEnvelope, /) -> None: ...


class OutboxPublishError(DatabaseOperationError):
    """A publisher-declared transport failure with a stable policy code."""

    def __init__(
        self,
        error_code: str,
        *,
        retryable: bool,
        protected_cause: BaseException | None = None,
    ) -> None:
        self.error_code = _identifier(error_code, "outbox publisher error code")
        if type(retryable) is not bool:
            raise ValueError("outbox publisher retryable flag must be boolean")
        self.retryable = retryable
        super().__init__(
            reason=(
                PlatformReason.TRANSIENT_FAILURE if retryable else PlatformReason.TRANSPORT_FAILED
            ),
            stage="outbox.publisher",
            protected_cause=protected_cause,
        )


class OutboxRelayError(DatabaseOperationError):
    """A sanitized relay failure after durable state has been preserved."""

    def __init__(self, *, protected_cause: BaseException | None = None) -> None:
        super().__init__(
            reason=PlatformReason.TRANSIENT_FAILURE,
            stage="outbox.relay",
            protected_cause=protected_cause,
        )


class OutboxRelayIntegrityError(DatabaseOperationError):
    """Relay ownership or outbox state changed outside the accepted transition."""

    def __init__(self, *, protected_cause: BaseException | None = None) -> None:
        super().__init__(
            reason=PlatformReason.INTEGRITY_FAILURE,
            stage="outbox.relay.integrity",
            protected_cause=protected_cause,
        )


class OutboxRelayLeaseUnavailable(DatabaseOperationError):
    """Another owner holds the relay lease or ownership can no longer be proven."""

    def __init__(self, *, protected_cause: BaseException | None = None) -> None:
        super().__init__(
            reason=PlatformReason.TRANSIENT_FAILURE,
            stage="outbox.relay.lease",
            protected_cause=protected_cause,
        )


@dataclass(frozen=True, slots=True)
class OutboxRetryPolicy:
    """Finite retry policy used after a typed publication failure."""

    max_attempts: int = 5
    retry_delay: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= _MAX_ATTEMPTS:
            raise ValueError("outbox retry maximum attempts is invalid")
        if (
            not isinstance(self.retry_delay, timedelta)
            or self.retry_delay <= timedelta(0)
            or self.retry_delay > _MAX_RETRY_DELAY
        ):
            raise ValueError("outbox retry delay is invalid")


@dataclass(frozen=True, slots=True)
class OutboxBatchReport:
    """Count-only relay evidence that never exposes message payloads or identifiers."""

    selected: int
    published: int
    retried: int
    dead_lettered: int

    def __post_init__(self) -> None:
        values = (self.selected, self.published, self.retried, self.dead_lettered)
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("outbox batch counts must be non-negative integers")
        if self.selected != self.published + self.retried + self.dead_lettered:
            raise ValueError("outbox batch counts do not reconcile")


class OutboxRelayLease(AbstractContextManager["OutboxRelayLease"]):
    """A finite renewable database-backed lease for one logical relay lane."""

    def __init__(
        self,
        driver: DatabaseDriver,
        *,
        relay_name: str,
        owner_id: str,
        clock: Callable[[], datetime],
        lease: timedelta,
    ) -> None:
        self._driver = driver
        self._relay_name = _identifier(relay_name, "outbox relay name")
        self._owner_id = _identifier(owner_id, "outbox relay owner id")
        if not callable(clock):
            raise TypeError("outbox relay clock must be callable")
        if not isinstance(lease, timedelta) or lease <= timedelta(0) or lease > _MAX_LEASE:
            raise ValueError("outbox relay lease is invalid")
        self._clock = clock
        self._lease = lease
        self._held = False
        self._finished = False
        self._last_observed_at: datetime | None = None
        self._generation: int | None = None

    def __enter__(self) -> OutboxRelayLease:
        if self._held or self._finished:
            raise OutboxRelayLeaseUnavailable()
        self._finished = True
        now = self._now()
        expires_at = now + self._lease
        try:
            with self._driver.transaction() as transaction:
                changed = transaction.execute(
                    DatabaseStatement(
                        f"""UPDATE {_LOCK_TABLE}
                        SET owner_id = :owner_id,
                            acquired_at = :acquired_at,
                            expires_at = :expires_at,
                            renewal = renewal + 1
                        WHERE relay_name = :relay_name
                          AND expires_at <= :now""",
                        {
                            "owner_id": self._owner_id,
                            "acquired_at": _timestamp(now),
                            "expires_at": _timestamp(expires_at),
                            "relay_name": self._relay_name,
                            "now": _timestamp(now),
                        },
                    )
                )
                if _one_or_zero(changed) == 0:
                    inserted = transaction.execute(
                        DatabaseStatement(
                            f"""INSERT INTO {_LOCK_TABLE} (
                                relay_name,
                                owner_id,
                                acquired_at,
                                expires_at,
                                renewal
                            ) VALUES (
                                :relay_name,
                                :owner_id,
                                :acquired_at,
                                :expires_at,
                                1
                            )""",
                            {
                                "relay_name": self._relay_name,
                                "owner_id": self._owner_id,
                                "acquired_at": _timestamp(now),
                                "expires_at": _timestamp(expires_at),
                            },
                        )
                    )
                    if _one_or_zero(inserted) != 1:
                        raise OutboxRelayIntegrityError()
                rows = transaction.query(
                    DatabaseStatement(
                        f"""SELECT owner_id, expires_at, renewal
                        FROM {_LOCK_TABLE}
                        WHERE relay_name = :relay_name""",
                        {"relay_name": self._relay_name},
                        read_only=True,
                    )
                )
                if len(rows) != 1:
                    raise OutboxRelayIntegrityError()
                row = rows[0]
                if (
                    row["owner_id"] != self._owner_id
                    or row["expires_at"] != _timestamp(expires_at)
                ):
                    raise OutboxRelayIntegrityError()
                generation = _generation(row["renewal"])
        except OutboxRelayIntegrityError:
            raise
        except DatabaseOperationError as error:
            raise OutboxRelayLeaseUnavailable(protected_cause=error) from None
        self._held = True
        self._generation = generation
        self._last_observed_at = now
        return self

    def renew(self) -> None:
        """Extend ownership only while this lease generation remains authoritative."""

        generation = self._generation
        if not self._held or generation is None:
            raise OutboxRelayLeaseUnavailable()
        now = self._now()
        last = self._last_observed_at
        if last is not None and now < last:
            raise OutboxRelayIntegrityError(
                protected_cause=ValueError("outbox relay clock moved backwards")
            )
        expires_at = now + self._lease
        try:
            with self._driver.transaction() as transaction:
                changed = transaction.execute(
                    DatabaseStatement(
                        f"""UPDATE {_LOCK_TABLE}
                        SET expires_at = :expires_at,
                            renewal = renewal + 1
                        WHERE relay_name = :relay_name
                          AND owner_id = :owner_id
                          AND renewal = :renewal
                          AND expires_at > :now""",
                        {
                            "expires_at": _timestamp(expires_at),
                            "relay_name": self._relay_name,
                            "owner_id": self._owner_id,
                            "renewal": generation,
                            "now": _timestamp(now),
                        },
                    )
                )
                if _one_or_zero(changed) != 1:
                    self._held = False
                    self._generation = None
                    raise OutboxRelayLeaseUnavailable()
        except OutboxRelayLeaseUnavailable:
            raise
        except DatabaseOperationError as error:
            self._held = False
            self._generation = None
            raise OutboxRelayLeaseUnavailable(protected_cause=error) from None
        self._generation = generation + 1
        self._last_observed_at = now

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error_type, error, traceback
        generation = self._generation
        if not self._held or generation is None:
            return False
        try:
            with self._driver.transaction() as transaction:
                changed = transaction.execute(
                    DatabaseStatement(
                        f"""DELETE FROM {_LOCK_TABLE}
                        WHERE relay_name = :relay_name
                          AND owner_id = :owner_id
                          AND renewal = :renewal""",
                        {
                            "relay_name": self._relay_name,
                            "owner_id": self._owner_id,
                            "renewal": generation,
                        },
                    )
                )
                if _one_or_zero(changed) != 1:
                    raise OutboxRelayIntegrityError()
        except OutboxRelayIntegrityError:
            raise
        except DatabaseOperationError as release_error:
            raise OutboxRelayIntegrityError(protected_cause=release_error) from None
        finally:
            self._held = False
            self._generation = None
        return False

    def _now(self) -> datetime:
        return _aware_utc(self._clock(), "outbox relay clock")


class OutboxRelay:
    """Publish one finite due batch with explicit at-least-once acknowledgement semantics."""

    def __init__(
        self,
        driver: DatabaseDriver,
        publisher: OutboxPublisher,
        *,
        owner_id: str,
        clock: Callable[[], datetime],
        outbox: TransactionalOutbox | None = None,
        retry_policy: OutboxRetryPolicy | None = None,
        relay_name: str = "default",
        lease: timedelta = timedelta(minutes=5),
    ) -> None:
        publish = getattr(publisher, "publish", None)
        if not callable(publish):
            raise TypeError("outbox publisher must define publish(envelope)")
        if not callable(clock):
            raise TypeError("outbox relay clock must be callable")
        resolved_outbox = TransactionalOutbox() if outbox is None else outbox
        if not isinstance(resolved_outbox, TransactionalOutbox):
            raise TypeError("outbox relay storage must be a TransactionalOutbox")
        resolved_policy = OutboxRetryPolicy() if retry_policy is None else retry_policy
        if not isinstance(resolved_policy, OutboxRetryPolicy):
            raise TypeError("outbox relay retry policy is invalid")
        self._driver = driver
        self._publisher = publisher
        self._owner_id = _identifier(owner_id, "outbox relay owner id")
        self._clock = clock
        self._outbox = resolved_outbox
        self._retry_policy = resolved_policy
        self._relay_name = _identifier(relay_name, "outbox relay name")
        if not isinstance(lease, timedelta) or lease <= timedelta(0) or lease > _MAX_LEASE:
            raise ValueError("outbox relay lease is invalid")
        self._lease = lease

    def install(self) -> None:
        """Install the outbox and global relay-lease tables idempotently."""

        with self._driver.transaction() as transaction:
            self._outbox.install(transaction)
            transaction.execute(
                DatabaseStatement(
                    f"""CREATE TABLE IF NOT EXISTS {_LOCK_TABLE} (
                        relay_name VARCHAR(128) PRIMARY KEY,
                        owner_id VARCHAR(128) NOT NULL,
                        acquired_at VARCHAR(32) NOT NULL,
                        expires_at VARCHAR(32) NOT NULL,
                        renewal BIGINT NOT NULL
                    )"""
                )
            )

    def lock(self) -> OutboxRelayLease:
        """Return a fresh single-use lease for this relay lane."""

        return OutboxRelayLease(
            self._driver,
            relay_name=self._relay_name,
            owner_id=self._owner_id,
            clock=self._clock,
            lease=self._lease,
        )

    def run_once(self, *, limit: int = 100) -> OutboxBatchReport:
        """Publish at most one bounded due batch and durably record every outcome."""

        if type(limit) is not int or not 1 <= limit <= _MAX_BATCH:
            raise ValueError("outbox relay batch limit is invalid")
        selected_at = self._now()
        with self.lock() as lease:
            with self._driver.transaction(read_only=True) as transaction:
                records = self._outbox.pending(transaction, now=selected_at, limit=limit)
            published = 0
            retried = 0
            dead_lettered = 0
            for record in records:
                lease.renew()
                try:
                    self._publisher.publish(record.envelope)
                except OutboxPublishError as publish_error:
                    terminal = self._record_failure(lease, record, publish_error)
                    if terminal:
                        dead_lettered += 1
                    else:
                        retried += 1
                    continue
                except Exception as unexpected:
                    generic = OutboxPublishError(
                        "publisher.failure",
                        retryable=True,
                        protected_cause=unexpected,
                    )
                    self._record_failure(lease, record, generic)
                    raise OutboxRelayError(protected_cause=unexpected) from None
                self._record_published(lease, record)
                published += 1
        return OutboxBatchReport(
            selected=len(records),
            published=published,
            retried=retried,
            dead_lettered=dead_lettered,
        )

    def _record_published(self, lease: OutboxRelayLease, record: OutboxRecord) -> None:
        lease.renew()
        with self._driver.transaction() as transaction:
            changed = self._outbox.mark_published(
                transaction,
                record.envelope.metadata.message_id,
                published_at=self._now(),
            )
        if not changed:
            raise OutboxRelayIntegrityError()

    def _record_failure(
        self,
        lease: OutboxRelayLease,
        record: OutboxRecord,
        error: OutboxPublishError,
    ) -> bool:
        lease.renew()
        failed_at = self._now()
        next_attempt = record.attempt_count + 1
        terminal = not error.retryable or next_attempt >= self._retry_policy.max_attempts
        with self._driver.transaction() as transaction:
            if terminal:
                changed = self._outbox.mark_dead_letter(
                    transaction,
                    record.envelope.metadata.message_id,
                    dead_lettered_at=failed_at,
                    error_code=error.error_code,
                )
            else:
                changed = self._outbox.mark_retry(
                    transaction,
                    record.envelope.metadata.message_id,
                    available_at=failed_at + self._retry_policy.retry_delay,
                    error_code=error.error_code,
                )
        if not changed:
            raise OutboxRelayIntegrityError()
        return terminal

    def _now(self) -> datetime:
        return _aware_utc(self._clock(), "outbox relay clock")


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical identifier")
    return value


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _generation(value: object) -> int:
    if type(value) is not int or value < 1:
        raise OutboxRelayIntegrityError(
            protected_cause=ValueError("outbox relay generation is invalid")
        )
    return value


def _one_or_zero(value: int) -> int:
    if type(value) is not int or value not in (0, 1):
        raise OutboxRelayIntegrityError(
            protected_cause=ValueError("outbox relay row count is invalid")
        )
    return value
