"""Deterministic recording fakes for extension and application tests."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import ClassVar

__all__ = (
    "FakeCache",
    "FakeCall",
    "FakeClock",
    "FakeEventBus",
    "FakeFiles",
    "FakeIds",
    "FakeLock",
    "FakeMail",
    "FakeNotifications",
    "FakeQueue",
    "FakeSearch",
    "FakeSubsystems",
    "FakeTransport",
)

_MAX_CALLS = 10_000
_MAX_TEXT = 512
_UNSAFE_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_UNSET = object()


def _bounded_text(value: object, label: str) -> str:
    if type(value) is not str or not value or len(value) > _MAX_TEXT or _UNSAFE_TEXT.search(value):
        raise ValueError(f"{label} must be bounded non-empty single-line text")
    return value


@dataclass(frozen=True, slots=True)
class FakeCall:
    """One snapshotted fake interaction; it is evidence only for test behavior."""

    sequence: int
    subsystem: str
    payload: object
    destination: str
    attempt: int

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("fake call sequence must be a positive integer")
        _bounded_text(self.subsystem, "fake subsystem")
        _bounded_text(self.destination, "fake destination")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("fake attempt must be a positive integer")


class _Journal:
    def __init__(self) -> None:
        self._calls: list[FakeCall] = []
        self._lock = Lock()

    def record(
        self,
        subsystem: str,
        payload: object,
        destination: str,
        attempt: int,
    ) -> FakeCall:
        _bounded_text(destination, "fake destination")
        if type(attempt) is not int or attempt < 1:
            raise ValueError("fake attempt must be a positive integer")
        try:
            snapshot = deepcopy(payload)
        except Exception as error:
            raise ValueError("fake payload must support deterministic snapshotting") from error
        with self._lock:
            if len(self._calls) >= _MAX_CALLS:
                raise ValueError("fake call journal exceeds the supported limit")
            call = FakeCall(
                sequence=len(self._calls) + 1,
                subsystem=subsystem,
                payload=snapshot,
                destination=destination,
                attempt=attempt,
            )
            self._calls.append(call)
            return deepcopy(call)

    def calls(self, subsystem: str | None = None) -> tuple[FakeCall, ...]:
        with self._lock:
            if subsystem is None:
                selected = tuple(self._calls)
            else:
                selected = tuple(call for call in self._calls if call.subsystem == subsystem)
            return deepcopy(selected)


class _RecordingFake:
    subsystem: ClassVar[str]

    def __init__(self, journal: _Journal | None = None) -> None:
        self._journal = journal or _Journal()

    @property
    def certifies_production(self) -> bool:
        """Recording fakes never certify timing, durability, isolation, or real I/O."""
        return False

    @property
    def calls(self) -> tuple[FakeCall, ...]:
        return self._journal.calls(self.subsystem)

    def dispatch(
        self,
        payload: object,
        *,
        destination: str,
        attempt: int = 1,
    ) -> FakeCall:
        return self._journal.record(self.subsystem, payload, destination, attempt)

    def assert_dispatched(
        self,
        payload: object = _UNSET,
        *,
        destination: object = _UNSET,
        attempt: object = _UNSET,
        times: int | None = None,
    ) -> tuple[FakeCall, ...]:
        if times is not None and (type(times) is not int or times < 0):
            raise ValueError("expected dispatch count must be a non-negative integer")
        matched = self._matching(payload, destination, attempt)
        expected = 1 if times is None else times
        if (times is None and not matched) or (times is not None and len(matched) != times):
            raise AssertionError(
                f"expected {expected} matching dispatches for {self.subsystem}; "
                f"observed {len(matched)}"
            )
        return matched

    def assert_not_dispatched(
        self,
        payload: object = _UNSET,
        *,
        destination: object = _UNSET,
        attempt: object = _UNSET,
    ) -> None:
        matched = self._matching(payload, destination, attempt)
        if matched:
            raise AssertionError(
                f"unexpected dispatch for {self.subsystem}; observed {len(matched)}"
            )

    def assert_order(self, *payloads: object) -> None:
        actual = tuple(call.payload for call in self.calls)
        if actual != payloads:
            raise AssertionError(
                f"dispatch order for {self.subsystem} differs: "
                f"expected {payloads!r}, observed {actual!r}"
            )

    def _matching(
        self,
        payload: object,
        destination: object,
        attempt: object,
    ) -> tuple[FakeCall, ...]:
        return tuple(
            call
            for call in self.calls
            if (payload is _UNSET or call.payload == payload)
            and (destination is _UNSET or call.destination == destination)
            and (attempt is _UNSET or call.attempt == attempt)
        )


class FakeEventBus(_RecordingFake):
    subsystem = "events"


class FakeQueue(_RecordingFake):
    subsystem = "queue"


class FakeCache(_RecordingFake):
    subsystem = "cache"


class FakeLock(_RecordingFake):
    subsystem = "locks"


class FakeFiles(_RecordingFake):
    subsystem = "files"


class FakeSearch(_RecordingFake):
    subsystem = "search"


class FakeMail(_RecordingFake):
    subsystem = "mail"


class FakeNotifications(_RecordingFake):
    subsystem = "notifications"


class FakeTransport(_RecordingFake):
    subsystem = "transport"


class FakeSubsystems:
    """One shared ordered journal containing the standard AF subsystem fakes."""

    def __init__(self) -> None:
        journal = _Journal()
        self.events = FakeEventBus(journal)
        self.queue = FakeQueue(journal)
        self.cache = FakeCache(journal)
        self.locks = FakeLock(journal)
        self.files = FakeFiles(journal)
        self.search = FakeSearch(journal)
        self.mail = FakeMail(journal)
        self.notifications = FakeNotifications(journal)
        self.transport = FakeTransport(journal)
        self._journal = journal

    @property
    def calls(self) -> tuple[FakeCall, ...]:
        return self._journal.calls()

    def assert_order(self, *expected: tuple[str, object]) -> None:
        actual = tuple((call.subsystem, call.payload) for call in self.calls)
        if actual != expected:
            raise AssertionError(
                f"global dispatch order differs: expected {expected!r}, observed {actual!r}"
            )


class FakeClock:
    """Thread-safe controllable timezone-aware clock."""

    def __init__(self, current: datetime) -> None:
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise ValueError("fake clock requires a timezone-aware datetime")
        self._current = current
        self._lock = Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self._current

    def advance(self, duration: timedelta) -> datetime:
        if not isinstance(duration, timedelta) or duration < timedelta(0):
            raise ValueError("fake clock duration must be non-negative")
        with self._lock:
            self._current += duration
            return self._current


class FakeIds:
    """Finite deterministic identifier source with generation assertions."""

    def __init__(self, values: Iterable[str]) -> None:
        normalized = tuple(_bounded_text(value, "fake identifier") for value in values)
        if len(normalized) > _MAX_CALLS:
            raise ValueError("fake identifiers exceed the supported limit")
        self._remaining = deque(normalized)
        self._generated: list[str] = []
        self._lock = Lock()

    def __call__(self) -> str:
        with self._lock:
            if not self._remaining:
                raise AssertionError("fake ID source has no queued identifiers")
            value = self._remaining.popleft()
            self._generated.append(value)
            return value

    def assert_generated(self, *values: str) -> None:
        with self._lock:
            actual = tuple(self._generated)
        if actual != values:
            raise AssertionError(
                f"generated identifiers differ: expected {values!r}, observed {actual!r}"
            )

    def assert_exhausted(self) -> None:
        with self._lock:
            remaining = tuple(self._remaining)
        if remaining:
            raise AssertionError(f"fake ID source retains {len(remaining)} identifiers")
