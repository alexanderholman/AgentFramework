"""Approved documents are data, scoped to a schema, policy and tenant."""

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol


def query_digest(document: str) -> str:
    return sha256(document.encode("utf-8")).hexdigest()


def validate_digest(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("invalid persisted query digest")


@dataclass(frozen=True, slots=True)
class PersistedQuery:
    document: str
    schema_digest: str
    operation_name: str | None = None
    tenant_id: str | None = None
    policy_version: str = "1"

    def __post_init__(self) -> None:
        if not isinstance(self.document, str) or not 1 <= len(self.document.encode()) <= 65_536:
            raise ValueError("invalid persisted query document bounds")
        validate_digest(self.schema_digest)
        for value in (self.operation_name, self.tenant_id, self.policy_version):
            if value is not None and (
                not isinstance(value, str)
                or not value
                or len(value) > 200
                or any(ord(char) < 32 for char in value)
            ):
                raise ValueError("invalid persisted query scope")

    @property
    def digest(self) -> str:
        return query_digest(self.document)


class PersistedQueryRegistry(Protocol):
    def approve(self, query: PersistedQuery) -> None: ...

    def resolve(
        self,
        digest: str,
        *,
        operation_name: str | None = None,
        tenant_id: str | None = None,
        policy_version: str = "1",
    ) -> PersistedQuery | None: ...

    def revoke(
        self,
        digest: str,
        *,
        operation_name: str | None = None,
        tenant_id: str | None = None,
        policy_version: str = "1",
    ) -> None: ...
