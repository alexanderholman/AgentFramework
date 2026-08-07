"""Domain model for documents."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class DocumentType(StrEnum):
    """Document classification."""

    PROCEDURE = "procedure"
    AGENT = "agent"
    SPECIFICATION = "specification"
    DECISION = "decision"
    SKILL = "skill"
    WORKFLOW = "workflow"


class DocumentStatus(StrEnum):
    """Document lifecycle status."""

    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class Document:
    """Immutable document representation."""

    id: str
    title: str
    content: str
    doc_type: DocumentType
    tags: tuple[str, ...] = ()
    status: DocumentStatus = DocumentStatus.ACTIVE
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def with_status(self, status: DocumentStatus) -> Document:
        """Return a new document with updated status."""
        return replace_one(self, status=status)

    def with_tags(self, tags: tuple[str, ...]) -> Document:
        """Return a new document with updated tags."""
        return replace_one(self, tags=tags)


def replace_one(doc: Document, **changes: Any) -> Document:
    """Create a new document with selected fields replaced."""
    updated = dict(doc.__dict__)
    updated.update(changes)
    return Document(**updated)
