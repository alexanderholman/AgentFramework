"""Governed contracts for recording one exact self-design plan locally."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from af.framework.errors import AFError
from af.framework.self_design_operation import PreparedDesignPlan
from af.framework.version_control import VersionControlInitialization

SELF_DESIGN_RECORD_SCHEMA = "af.self-design-record/v1"
SELF_DESIGN_RECORD_MAX_BYTES = 256 * 1024
_MAX_AUTHORITY_LIFETIME_SECONDS = 3_600
_AUTHORITY_ID = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
_REF = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,254})$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DesignRecordingError(AFError):
    """A sanitized fail-closed self-design recording error."""


class DesignRecordingEffect(StrEnum):
    """Effects understood by the first governed recording slice."""

    LOCAL_RECORD = "local_record"


class DesignRecordStatus(StrEnum):
    """Stable outcomes from the idempotent local recorder."""

    RECORDED = "recorded"
    UNCHANGED = "unchanged"


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"self-design recording {field_name} must be timezone aware")
    return value.astimezone(UTC)


def _iso8601(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DesignRecordAuthority:
    """Exact, expiring authority for one local record and no external publication."""

    authority_id: str
    repository: str
    base_ref: str
    head_sha: str
    snapshot_digest: str
    plan_digest: str
    version_control_manifest_digest: str
    issued_at: datetime
    expires_at: datetime
    allowed_effects: tuple[DesignRecordingEffect, ...]

    def __post_init__(self) -> None:
        if type(self.authority_id) is not str or _AUTHORITY_ID.fullmatch(self.authority_id) is None:
            raise ValueError("self-design recording authority identity is invalid")
        if type(self.repository) is not str or _REPOSITORY.fullmatch(self.repository) is None:
            raise ValueError("self-design recording repository identity is invalid")
        if type(self.base_ref) is not str or _REF.fullmatch(self.base_ref) is None:
            raise ValueError("self-design recording base ref is invalid")
        if type(self.head_sha) is not str or _SHA1.fullmatch(self.head_sha) is None:
            raise ValueError("self-design recording head SHA is invalid")
        for value, label in (
            (self.snapshot_digest, "snapshot digest"),
            (self.plan_digest, "plan digest"),
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise ValueError(f"self-design recording {label} is invalid")
        if (
            type(self.version_control_manifest_digest) is not str
            or _HEX_SHA256.fullmatch(self.version_control_manifest_digest) is None
        ):
            raise ValueError("self-design recording version-control digest is invalid")
        issued_at = _aware_utc(self.issued_at, "authority issue time")
        expires_at = _aware_utc(self.expires_at, "authority expiry time")
        if expires_at <= issued_at:
            raise ValueError("self-design recording authority expiry is invalid")
        if (expires_at - issued_at).total_seconds() > _MAX_AUTHORITY_LIFETIME_SECONDS:
            raise ValueError("self-design recording authority lifetime is too long")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        if self.allowed_effects != (DesignRecordingEffect.LOCAL_RECORD,):
            raise ValueError("self-design recording authority grants unsupported effects")

    def canonical_json(self) -> str:
        """Return the deterministic authority representation bound into the record."""
        return _canonical_json(
            {
                "allowed_effects": [effect.value for effect in self.allowed_effects],
                "authority_id": self.authority_id,
                "base_ref": self.base_ref,
                "expires_at": _iso8601(self.expires_at),
                "head_sha": self.head_sha,
                "issued_at": _iso8601(self.issued_at),
                "plan_digest": self.plan_digest,
                "repository": self.repository,
                "snapshot_digest": self.snapshot_digest,
                "version_control_manifest_digest": self.version_control_manifest_digest,
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())

    @classmethod
    def from_dict(cls, payload: object) -> DesignRecordAuthority:
        if type(payload) is not dict:
            raise TypeError("self-design recording authority payload is invalid")
        allowed_effects = payload.get("allowed_effects")
        if type(allowed_effects) is not list:
            raise ValueError("self-design recording authority effects are invalid")
        try:
            authority_id = payload["authority_id"]
            repository = payload["repository"]
            base_ref = payload["base_ref"]
            head_sha = payload["head_sha"]
            snapshot_digest = payload["snapshot_digest"]
            plan_digest = payload["plan_digest"]
            version_control_manifest_digest = payload["version_control_manifest_digest"]
            effects = tuple(DesignRecordingEffect(item) for item in allowed_effects)
            issued_at = datetime.fromisoformat(str(payload["issued_at"]).replace("Z", "+00:00"))
            expires_at = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("self-design recording authority payload is invalid") from error
        string_fields = (
            authority_id,
            repository,
            base_ref,
            head_sha,
            snapshot_digest,
            plan_digest,
            version_control_manifest_digest,
        )
        if any(type(value) is not str for value in string_fields):
            raise ValueError("self-design recording authority payload is invalid")
        return cls(
            authority_id=authority_id,
            repository=repository,
            base_ref=base_ref,
            head_sha=head_sha,
            snapshot_digest=snapshot_digest,
            plan_digest=plan_digest,
            version_control_manifest_digest=version_control_manifest_digest,
            issued_at=issued_at,
            expires_at=expires_at,
            allowed_effects=effects,
        )


@dataclass(frozen=True, slots=True)
class DesignRecordingRequest:
    """One exact prepared plan, local coordination state, and bounded authority."""

    prepared: PreparedDesignPlan
    authority: DesignRecordAuthority
    coordination: VersionControlInitialization
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.prepared, PreparedDesignPlan):
            raise TypeError("self-design prepared plan is invalid")
        if not isinstance(self.authority, DesignRecordAuthority):
            raise TypeError("self-design recording authority is invalid")
        if not isinstance(self.coordination, VersionControlInitialization):
            raise TypeError("self-design coordination state is invalid")
        observed_at = _aware_utc(self.observed_at, "observation time")
        object.__setattr__(self, "observed_at", observed_at)
        if not self.authority.issued_at <= observed_at <= self.authority.expires_at:
            raise DesignRecordingError("self-design recording authority is not currently valid")

        snapshot = self.prepared.snapshot
        plan = self.prepared.plan
        manifest = self.coordination.manifest
        expected_manifest_path = (
            Path(manifest.repository.git_common_dir) / "af" / "version-control.json"
        )
        bindings_match = (
            snapshot.repository == self.authority.repository
            and snapshot.base_ref == self.authority.base_ref
            and snapshot.head_sha == self.authority.head_sha
            and snapshot.digest == self.authority.snapshot_digest
            and plan.digest == self.authority.plan_digest
            and manifest.digest() == self.authority.version_control_manifest_digest
            and manifest.repository.branch == snapshot.base_ref
            and manifest.repository.head_sha == snapshot.head_sha
            and self.coordination.manifest_path == str(expected_manifest_path)
        )
        if not bindings_match:
            raise DesignRecordingError("self-design recording evidence binding is invalid")


@dataclass(frozen=True, slots=True)
class DesignPlanRecord:
    """Canonical durable evidence for one prepared plan and its local authority."""

    prepared: PreparedDesignPlan
    authority: DesignRecordAuthority
    version_control_manifest_digest: str
    recorded_at: datetime
    schema_version: str = field(default=SELF_DESIGN_RECORD_SCHEMA, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.prepared, PreparedDesignPlan):
            raise TypeError("self-design record prepared plan is invalid")
        if not isinstance(self.authority, DesignRecordAuthority):
            raise TypeError("self-design record authority is invalid")
        if (
            type(self.version_control_manifest_digest) is not str
            or _HEX_SHA256.fullmatch(self.version_control_manifest_digest) is None
            or self.version_control_manifest_digest
            != self.authority.version_control_manifest_digest
        ):
            raise ValueError("self-design record version-control digest is invalid")
        object.__setattr__(
            self,
            "recorded_at",
            _aware_utc(self.recorded_at, "record time"),
        )

    @classmethod
    def from_request(cls, request: DesignRecordingRequest) -> DesignPlanRecord:
        if not isinstance(request, DesignRecordingRequest):
            raise TypeError("self-design recording request is invalid")
        return cls(
            prepared=request.prepared,
            authority=request.authority,
            version_control_manifest_digest=request.coordination.manifest.digest(),
            recorded_at=request.observed_at,
        )

    @classmethod
    def from_canonical_bytes(
        cls,
        content: bytes,
        prepared: PreparedDesignPlan,
    ) -> DesignPlanRecord:
        if not isinstance(prepared, PreparedDesignPlan):
            raise TypeError("self-design prepared plan is invalid")
        if type(content) is not bytes:
            raise TypeError("self-design record bytes are invalid")
        try:
            payload = json.loads(content)
            authority = DesignRecordAuthority.from_dict(payload["authority"])
            recorded_at = datetime.fromisoformat(str(payload["recorded_at"]).replace("Z", "+00:00"))
            version_control_manifest_digest = payload["version_control_manifest_digest"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise DesignRecordingError("self-design record state is invalid") from error
        record = cls(
            prepared=prepared,
            authority=authority,
            version_control_manifest_digest=version_control_manifest_digest,
            recorded_at=recorded_at,
        )
        if not hmac.compare_digest(record.canonical_bytes(), content):
            raise DesignRecordingError("self-design record state is invalid")
        return record

    def payload(self) -> dict[str, object]:
        return {
            "authority": json.loads(self.authority.canonical_json()),
            "authority_digest": self.authority.digest,
            "plan": json.loads(self.prepared.plan.canonical_json()),
            "plan_digest": self.prepared.plan.digest,
            "recorded_at": _iso8601(self.recorded_at),
            "schema_version": self.schema_version,
            "snapshot": json.loads(self.prepared.snapshot.canonical_json()),
            "snapshot_digest": self.prepared.snapshot.digest,
            "version_control_manifest_digest": self.version_control_manifest_digest,
        }

    def canonical_bytes(self) -> bytes:
        return (_canonical_json(self.payload()) + "\n").encode("utf-8")

    @property
    def digest(self) -> str:
        return _digest(self.canonical_bytes().decode("utf-8"))


@dataclass(frozen=True, slots=True)
class DesignRecordResult:
    status: DesignRecordStatus
    record: DesignPlanRecord
    record_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, DesignRecordStatus):
            raise TypeError("self-design record status is invalid")
        if not isinstance(self.record, DesignPlanRecord):
            raise TypeError("self-design record is invalid")
        if type(self.record_path) is not str or not Path(self.record_path).is_absolute():
            raise ValueError("self-design record path must be absolute")

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "record": {
                "digest": self.record.digest,
                "path": self.record_path,
                "plan_digest": self.record.prepared.plan.digest,
                "snapshot_digest": self.record.prepared.snapshot.digest,
            },
        }


class DesignPlanRecorder(Protocol):
    """Persist one already-authorized plan without widening its effect scope."""

    def record(self, request: DesignRecordingRequest) -> DesignRecordResult:
        """Record the exact request idempotently or fail closed."""
        ...
