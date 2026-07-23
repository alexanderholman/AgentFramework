from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from af.testing import (
    FakeCache,
    FakeClock,
    FakeEventBus,
    FakeFiles,
    FakeIds,
    FakeLock,
    FakeMail,
    FakeNotifications,
    FakeQueue,
    FakeSearch,
    FakeSubsystems,
    FakeTransport,
)


def test_fake_clock_and_ids_are_deterministic_and_assertable() -> None:
    start = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    ids = FakeIds(("id-1", "id-2"))

    assert clock() == start
    assert clock.advance(timedelta(minutes=5)) == start + timedelta(minutes=5)
    assert ids() == "id-1"
    assert ids() == "id-2"
    ids.assert_generated("id-1", "id-2")
    ids.assert_exhausted()

    with pytest.raises(AssertionError, match="no queued identifiers"):
        ids()
    with pytest.raises(ValueError, match="non-negative"):
        clock.advance(timedelta(seconds=-1))


def test_fake_clock_ids_and_payload_snapshots_fail_clearly() -> None:
    class Unsnapshotable:
        def __deepcopy__(self, memo: object) -> object:
            del memo
            raise RuntimeError("private copy failure")

    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 7, 23, 12, 0))
    with pytest.raises(ValueError, match="fake identifier"):
        FakeIds(("",))

    ids = FakeIds(("remaining",))
    with pytest.raises(AssertionError, match="retains 1"):
        ids.assert_exhausted()
    with pytest.raises(AssertionError, match="generated identifiers differ"):
        ids.assert_generated("missing")

    with pytest.raises(ValueError, match="deterministic snapshotting"):
        FakeTransport().dispatch(Unsnapshotable(), destination="mcp")


def test_fake_subsystems_assert_payload_destination_attempts_and_absence() -> None:
    fakes = FakeSubsystems()
    payload = {"task": "one"}

    fakes.events.dispatch(payload, destination="task.created")
    payload["task"] = "mutated"
    fakes.queue.dispatch({"task": "two"}, destination="critical", attempt=2)
    fakes.mail.dispatch("hello", destination="user@example.test")

    event = fakes.events.assert_dispatched(
        {"task": "one"},
        destination="task.created",
        attempt=1,
        times=1,
    )
    assert event[0].sequence == 1
    returned_payload = event[0].payload
    assert isinstance(returned_payload, dict)
    returned_payload["task"] = "changed through observation"
    fakes.events.assert_dispatched({"task": "one"})
    fakes.queue.assert_dispatched(
        {"task": "two"},
        destination="critical",
        attempt=2,
    )
    fakes.notifications.assert_not_dispatched()
    fakes.mail.assert_not_dispatched(destination="other@example.test")

    with pytest.raises(AssertionError, match="expected 2 matching dispatches"):
        fakes.events.assert_dispatched({"task": "one"}, times=2)
    with pytest.raises(AssertionError, match="unexpected dispatch"):
        fakes.mail.assert_not_dispatched("hello")


def test_fake_subsystems_preserve_local_and_cross_subsystem_order() -> None:
    fakes = FakeSubsystems()

    fakes.files.dispatch("first", destination="documents/a")
    fakes.search.dispatch("second", destination="index")
    fakes.files.dispatch("third", destination="documents/b")
    fakes.transport.dispatch("fourth", destination="mcp")

    fakes.files.assert_order("first", "third")
    fakes.assert_order(
        ("files", "first"),
        ("search", "second"),
        ("files", "third"),
        ("transport", "fourth"),
    )

    with pytest.raises(AssertionError, match="dispatch order"):
        fakes.files.assert_order("third", "first")
    with pytest.raises(AssertionError, match="global dispatch order"):
        fakes.assert_order(("transport", "fourth"), ("files", "first"))


def test_all_required_fake_catalogue_entries_share_recording_semantics() -> None:
    fakes = FakeSubsystems()

    assert isinstance(fakes.events, FakeEventBus)
    assert isinstance(fakes.queue, FakeQueue)
    assert isinstance(fakes.cache, FakeCache)
    assert isinstance(fakes.locks, FakeLock)
    assert isinstance(fakes.files, FakeFiles)
    assert isinstance(fakes.search, FakeSearch)
    assert isinstance(fakes.mail, FakeMail)
    assert isinstance(fakes.notifications, FakeNotifications)
    assert isinstance(fakes.transport, FakeTransport)

    for fake in (
        fakes.events,
        fakes.queue,
        fakes.cache,
        fakes.locks,
        fakes.files,
        fakes.search,
        fakes.mail,
        fakes.notifications,
        fakes.transport,
    ):
        fake.dispatch("payload", destination="destination", attempt=3)
        fake.assert_dispatched(
            "payload",
            destination="destination",
            attempt=3,
            times=1,
        )
        assert fake.certifies_production is False


@pytest.mark.parametrize(
    ("destination", "attempt"),
    [
        ("", 1),
        ("contains\nnewline", 1),
        ("destination", 0),
        ("destination", True),
    ],
)
def test_fake_dispatch_rejects_ambiguous_record_metadata(
    destination: str,
    attempt: int,
) -> None:
    fake = FakeTransport()

    with pytest.raises(ValueError):
        fake.dispatch("payload", destination=destination, attempt=attempt)
