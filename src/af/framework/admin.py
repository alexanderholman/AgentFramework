"""Optional composable admin resources dispatch only installed application callbacks."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from af.framework.context import Principal
from af.framework.observability_products import SafeAttribute, identifier


@dataclass(frozen=True, slots=True)
class AdminRow:
    reference: str
    state: str
    attributes: tuple[tuple[str, SafeAttribute], ...] = ()

    def __post_init__(self) -> None:
        if (
            not 1 <= len(self.reference) <= 200
            or not 1 <= len(self.state) <= 100
            or len(self.attributes) > 16
        ):
            raise ValueError("admin row exceeds presentation bounds")
        for key, _ in self.attributes:
            identifier(key)
            if any(
                term in key
                for term in (
                    "password",
                    "secret",
                    "token",
                    "credential",
                    "payload",
                    "body",
                    "content",
                )
            ):
                raise ValueError("admin metadata contains a sensitive field")


@dataclass(frozen=True, slots=True)
class AdminActionResult:
    status: str
    reference: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"completed", "queued", "denied", "failed"} or (
            self.reference is not None and not 1 <= len(self.reference) <= 200
        ):
            raise ValueError("invalid admin action result")


@dataclass(frozen=True, slots=True)
class AdminAction:
    name: str
    required_scope: str
    execute: Callable[[Principal, Mapping[str, SafeAttribute]], AdminActionResult]
    requires_confirmation: bool = True


@dataclass(frozen=True, slots=True)
class AdminResource:
    name: str
    read_scope: str
    query: Callable[[Principal, int], tuple[AdminRow, ...]]
    actions: tuple[AdminAction, ...] = ()
