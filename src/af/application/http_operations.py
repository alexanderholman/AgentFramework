"""Bounded transport request admission and replay port; no task business logic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ApiRejection(Exception):
    def __init__(self, status: int, reason: str, *, retry_after: int | None = None) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes

    def __post_init__(self) -> None:
        if type(self.status) is not int or not 200 <= self.status < 500:
            raise ValueError("only definitive non-server-error responses can be replayed")
        if not isinstance(self.body, bytes) or len(self.body) > 1_048_576:
            raise ValueError("replay response exceeds the bounded profile")
        if len(self.headers) > 64 or any(
            not isinstance(key, bytes)
            or not isinstance(value, bytes)
            or len(key) > 128
            or len(value) > 8192
            for key, value in self.headers
        ):
            raise ValueError("replay response headers exceed the bounded profile")


class HttpOperations(Protocol):
    def admit(self, principal: str, *, now: float) -> None: ...

    def claim(
        self, scope: str, key: str, fingerprint: str, *, now: float
    ) -> StoredResponse | None: ...

    def complete(
        self, scope: str, key: str, fingerprint: str, response: StoredResponse, *, now: float
    ) -> None: ...

    def close(self) -> None: ...
