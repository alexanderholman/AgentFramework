"""Tests for TimeClassifier and CurrentTimeProcedure."""

import pytest

from af.domain.classifiers import TimeClassifier
from af.domain.procedures import CurrentTimeProcedure
from af.framework.types import (
    Classification,
    WorkRequest,
    WorkTier,
    ProcedureResult,
)


class TestTimeClassifier:
    """Tests for the TimeClassifier."""

    @pytest.fixture
    def classifier(self):
        return TimeClassifier()

    def test_classifies_what_time_is_it(self, classifier):
        request = WorkRequest(prompt="what time is it")
        result = classifier.classify(request)
        assert result is not None
        assert isinstance(result, Classification)
        assert result.intent == "system.current_time"
        assert result.arguments == {}

    def test_classifies_current_time(self, classifier):
        request = WorkRequest(prompt="what is the current time")
        result = classifier.classify(request)
        assert result is not None
        assert result.intent == "system.current_time"

    def test_classifies_time_with_timezone(self, classifier):
        request = WorkRequest(prompt="what time is it in New York")
        result = classifier.classify(request)
        assert result is not None
        assert result.intent == "system.current_time"
        assert "timezone" in result.arguments

    def test_classifies_time_in_region(self, classifier):
        request = WorkRequest(prompt="what's the time in London")
        result = classifier.classify(request)
        assert result is not None
        assert result.intent == "system.current_time"
        assert "timezone" in result.arguments

    def test_does_not_classify_non_time_request(self, classifier):
        request = WorkRequest(prompt="hello world")
        result = classifier.classify(request)
        assert result is None

    def test_classifies_time_now(self, classifier):
        request = WorkRequest(prompt="what time is it now")
        result = classifier.classify(request)
        assert result is not None
        assert result.intent == "system.current_time"

    def test_classifies_current_time_in_timezone(self, classifier):
        request = WorkRequest(prompt="current time in Tokyo")
        result = classifier.classify(request)
        assert result is not None
        assert result.intent == "system.current_time"
        assert "timezone" in result.arguments


class TestCurrentTimeProcedure:
    """Tests for the CurrentTimeProcedure."""

    @pytest.fixture
    def procedure(self):
        return CurrentTimeProcedure()

    def test_supports_time_classification(self, procedure):
        classification = Classification(
            intent="system.current_time",
            arguments={},
            confidence=1.0,
            classifier="time-classifier",
        )
        assert procedure.supports(classification)

    def test_does_not_support_other_classification(self, procedure):
        classification = Classification(
            intent="text.character_count",
            arguments={"text": "hello", "character": "h"},
            confidence=1.0,
            classifier="character-count-classifier",
        )
        assert not procedure.supports(classification)

    def test_validate_passes(self, procedure):
        classification = Classification(
            intent="system.current_time",
            arguments={},
            confidence=1.0,
            classifier="time-classifier",
        )
        # validate returns None on success
        assert procedure.validate(classification) is None

    def test_validate_raises_on_mismatch(self, procedure):
        classification = Classification(
            intent="text.character_count",
            arguments={"text": "hello", "character": "h"},
            confidence=1.0,
            classifier="character-count-classifier",
        )
        with pytest.raises(Exception):
            procedure.validate(classification)

    def test_execute_returns_procedure_result(self, procedure):
        classification = Classification(
            intent="system.current_time",
            arguments={},
            confidence=1.0,
            classifier="time-classifier",
        )
        result = procedure.execute(classification)
        assert isinstance(result, ProcedureResult)
        assert result.output
        # Output should contain an ISO format timestamp
        assert "T" in result.output

    def test_execute_with_timezone(self, procedure):
        classification = Classification(
            intent="system.current_time",
            arguments={"timezone": "America/New_York"},
            confidence=1.0,
            classifier="time-classifier",
        )
        result = procedure.execute(classification)
        assert isinstance(result, ProcedureResult)
        assert result.output

    def test_tier_is_static(self, procedure):
        assert procedure.tier == WorkTier.STATIC

    def test_is_pure(self, procedure):
        assert procedure.pure is True

    def test_is_replay_safe(self, procedure):
        assert procedure.replay_safe is True
