"""Clock observations cannot become exact reusable answers or ignore zones."""

from datetime import UTC, datetime, timedelta

import pytest

import af.domain.procedures as procedures
from af.domain.classifiers import TimeClassifier
from af.domain.procedures import CurrentTimeProcedure
from af.framework.errors import InvalidSelectionProposal
from af.framework.types import Classification, WorkRequest


@pytest.mark.parametrize("prompt", ["current time", "current time in UTC"])
def test_two_identical_time_requests_read_the_changed_clock(application, monkeypatch, prompt):
    class Clock(datetime):
        moment = datetime(2026, 9, 26, 12, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.moment.astimezone(tz)

    monkeypatch.setattr(procedures.dt, "datetime", Clock)
    first = application.tasks.execute(prompt)
    Clock.moment += timedelta(minutes=5)
    second = application.tasks.execute(prompt)
    assert first.output == "2026-09-26T12:00:00+00:00"
    assert second.output == "2026-09-26T12:05:00+00:00"
    assert first.verified and second.verified
    assert not first.reused and not second.reused


def test_explicit_iana_zone_is_preserved_through_classification_and_execution(application):
    classification = TimeClassifier().classify(WorkRequest(prompt="current time in Asia/Tokyo?"))
    assert classification is not None
    assert classification.arguments == {"timezone": "Asia/Tokyo"}
    outcome = application.tasks.execute("current time in Asia/Tokyo?")
    assert datetime.fromisoformat(outcome.output).utcoffset() == timedelta(hours=9)


@pytest.mark.parametrize("zone", ["", " ", "Unknown/City", "/etc/passwd", "../UTC", "x" * 65])
def test_invalid_timezone_cannot_silently_become_utc(zone):
    classification = Classification("system.current_time", {"timezone": zone}, 1, "test")
    procedure = CurrentTimeProcedure()
    with pytest.raises(InvalidSelectionProposal):
        procedure.validate(classification)
    with pytest.raises(InvalidSelectionProposal):
        procedure.execute(classification)
