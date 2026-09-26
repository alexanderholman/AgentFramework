"""Bounded peer grants for one exact, already installed, read-only procedure."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from af.framework.content import (
    SignedEnvelope,
    canonical_json,
    content_digest,
    require_digest,
    require_int,
    require_text,
)
from af.framework.contracts import Procedure, Verifier
from af.framework.types import Classification, ProcedureResult


@dataclass(frozen=True, slots=True)
class PeerLease:
    lease_id: str
    peer_key_id: str
    host_key_id: str
    procedure_id: str
    procedure_version: str
    descriptor_digest: str
    implementation_digest: str
    arguments_digest: str
    scope_digest: str
    nonce: str
    issued_at: datetime
    expires_at: datetime
    max_input_bytes: int = 16_384
    max_output_bytes: int = 16_384
    max_wall_ms: int = 10_000
    max_cpu_ms: int = 5_000
    max_memory_mib: int = 256
    effect: str = "read_only"
    egress: str = "none"
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        for text in (self.lease_id, self.procedure_id, self.procedure_version, self.nonce):
            require_text(text, maximum=128)
        for digest in (
            self.peer_key_id,
            self.host_key_id,
            self.descriptor_digest,
            self.implementation_digest,
            self.arguments_digest,
            self.scope_digest,
        ):
            require_digest(digest)
        if self.issued_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("peer lease timestamps must be timezone aware")
        duration = (self.expires_at - self.issued_at).total_seconds()
        if not 0 < duration <= 3600:
            raise ValueError("peer lease lifetime must be at most one hour")
        require_int(self.max_input_bytes, minimum=1, maximum=1_048_576)
        require_int(self.max_output_bytes, minimum=1, maximum=1_048_576)
        require_int(self.max_wall_ms, minimum=1, maximum=600_000)
        require_int(self.max_cpu_ms, minimum=1, maximum=600_000)
        require_int(self.max_memory_mib, minimum=32, maximum=1_048_576)
        if self.schema_version != "1.0.0" or self.effect != "read_only" or self.egress != "none":
            raise ValueError("peer v1 permits only bounded read-only work with no egress")

    @property
    def digest(self) -> str:
        return content_digest(canonical_json(self.to_dict()).encode())

    def to_dict(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "peer_key_id": self.peer_key_id,
            "host_key_id": self.host_key_id,
            "procedure_id": self.procedure_id,
            "procedure_version": self.procedure_version,
            "descriptor_digest": self.descriptor_digest,
            "implementation_digest": self.implementation_digest,
            "arguments_digest": self.arguments_digest,
            "scope_digest": self.scope_digest,
            "nonce": self.nonce,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "max_input_bytes": self.max_input_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_wall_ms": self.max_wall_ms,
            "max_cpu_ms": self.max_cpu_ms,
            "max_memory_mib": self.max_memory_mib,
            "effect": self.effect,
            "egress": self.egress,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PeerLease:
        if set(data) != set(cls.__dataclass_fields__):
            raise ValueError("peer lease has unknown or missing fields")
        return cls(
            lease_id=require_text(data["lease_id"]),
            peer_key_id=require_digest(data["peer_key_id"]),
            host_key_id=require_digest(data["host_key_id"]),
            procedure_id=require_text(data["procedure_id"]),
            procedure_version=require_text(data["procedure_version"]),
            descriptor_digest=require_digest(data["descriptor_digest"]),
            implementation_digest=require_digest(data["implementation_digest"]),
            arguments_digest=require_digest(data["arguments_digest"]),
            scope_digest=require_digest(data["scope_digest"]),
            nonce=require_text(data["nonce"]),
            issued_at=datetime.fromisoformat(require_text(data["issued_at"])),
            expires_at=datetime.fromisoformat(require_text(data["expires_at"])),
            max_input_bytes=require_int(data["max_input_bytes"]),
            max_output_bytes=require_int(data["max_output_bytes"]),
            max_wall_ms=require_int(data["max_wall_ms"]),
            max_cpu_ms=require_int(data["max_cpu_ms"]),
            max_memory_mib=require_int(data["max_memory_mib"]),
            effect=require_text(data["effect"]),
            egress=require_text(data["egress"]),
            schema_version=require_text(data["schema_version"]),
        )


class PeerLedger(Protocol):
    def grant(self, lease: PeerLease, envelope: SignedEnvelope) -> None: ...

    def admit(self, lease: PeerLease, request_digest: str, now: datetime) -> bool: ...

    def current(self, lease: PeerLease, now: datetime) -> bool: ...

    def finish(self, lease: PeerLease, status: str, now: datetime) -> bool: ...

    def revoke(self, lease_id: str) -> None: ...

    def recover_interrupted(self) -> tuple[str, ...]: ...


class PeerCancellation(Protocol):
    def __call__(self) -> bool: ...


class PeerProcedureExecutor(Protocol):
    def execute(
        self,
        procedure: Procedure,
        classification: Classification,
        verifier: Verifier,
        lease: PeerLease,
        cancelled: PeerCancellation,
    ) -> ProcedureResult: ...
