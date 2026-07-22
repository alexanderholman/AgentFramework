from __future__ import annotations

import asyncio

import pytest

from af.framework.errors import (
    FailureCategory,
    PlatformError,
    PlatformReason,
    PublicError,
    category_for_reason,
)


@pytest.mark.parametrize("reason", list(PlatformReason))
def test_every_platform_reason_has_stable_category_and_public_projection(
    reason: PlatformReason,
) -> None:
    cause = RuntimeError("private provider token=secret")
    error = PlatformError(reason, stage="test.stage", protected_cause=cause)

    assert isinstance(error.category, FailureCategory)
    assert error.category is category_for_reason(reason)
    assert error.public_error == PublicError(reason.value, error.public_message)
    assert error.protected_cause is cause
    assert "private provider" not in str(error)
    assert "private provider" not in repr(error)
    assert "private provider" not in repr(error.args)


def test_platform_reason_codes_are_explicit_and_not_class_name_derived() -> None:
    assert PlatformReason.CONFIGURATION_INVALID.value == "configuration_invalid"
    assert PlatformReason.CONTEXT_UNAVAILABLE.value == "context_unavailable"
    assert PlatformReason.CANCELLED.value == "cancelled"


def test_cancellation_is_not_a_platform_error() -> None:
    assert not issubclass(asyncio.CancelledError, PlatformError)


def test_platform_error_rejects_untrusted_public_message_and_stage() -> None:
    with pytest.raises(ValueError, match="stage"):
        PlatformError(PlatformReason.TRANSIENT_FAILURE, stage="bad\u2028stage")
