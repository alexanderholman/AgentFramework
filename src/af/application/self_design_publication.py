"""Application controller for explicit self-design publication authority."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from af.framework.self_design_operation import PreparedDesignPlan
from af.framework.self_design_publication import (
    DesignPublicationAuthority,
    DesignPublicationDraft,
    DesignPublicationError,
    DesignPublicationReceipt,
    DesignPublicationRequest,
    DesignPublisher,
)
from af.framework.self_design_recording import DesignRecordResult


class SelfDesignPublicationController:
    """Publish one recorded plan only through an explicitly admitted adapter."""

    __slots__ = ("_clock", "_publisher")

    def __init__(
        self,
        publisher: DesignPublisher,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(getattr(publisher, "publish", None)):
            raise TypeError("self-design publisher is invalid")
        if not callable(clock):
            raise TypeError("self-design publication clock is invalid")
        self._publisher = publisher
        self._clock = clock

    def publish(
        self,
        prepared: PreparedDesignPlan,
        recorded: DesignRecordResult,
        draft: DesignPublicationDraft,
        authority: DesignPublicationAuthority,
    ) -> DesignPublicationReceipt:
        """Validate exact authority before invoking the external publisher once."""
        observed_at = self._clock()
        if not isinstance(observed_at, datetime):
            raise TypeError("self-design publication clock returned an invalid time")
        request = DesignPublicationRequest(
            prepared=prepared,
            recorded=recorded,
            draft=draft,
            authority=authority,
            observed_at=observed_at,
        )
        receipt = self._publisher.publish(request)
        if not isinstance(receipt, DesignPublicationReceipt):
            raise TypeError("self-design publisher returned an invalid receipt")
        receipt_matches = (
            receipt.effect is draft.effect
            and receipt.repository == draft.repository
            and receipt.draft_digest == draft.digest
            and (
                draft.target_issue_number is None
                or receipt.issue_number == draft.target_issue_number
            )
        )
        if not receipt_matches:
            raise DesignPublicationError("self-design publisher receipt binding is invalid")
        return receipt
