from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from af.framework.content import (
    ArtifactManifest,
    ContentChunk,
    ContentSignature,
    SignedEnvelope,
    canonical_json,
)
from af.framework.peers import PeerLease

DIGEST = "sha256:" + "a" * 64


def artifact() -> ArtifactManifest:
    return ArtifactManifest(
        "fixture",
        1,
        DIGEST,
        1,
        (ContentChunk(DIGEST, 1),),
        "fixture",
        "json",
        "MIT",
        "local",
        datetime.now(UTC),
        (),
        "cpu",
        "none",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "2"},
        {"artifact_id": ""},
        {"revision": True},
        {"content_digest": "bad"},
        {"chunks": []},
        {"byte_size": 2},
        {"expires_at": datetime(2026, 1, 1)},
        {"runtime_ids": []},
        {"locales": ("",)},
    ],
)
def test_manifest_rejects_ambiguous_or_mutable_wire_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(artifact(), **changes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("unknown", "field"),
        ("chunks", [None]),
        ("chunks", [{"digest": DIGEST}]),
        ("runtime_ids", "llama"),
        ("private", "false"),
    ],
)
def test_manifest_parser_rejects_unknown_fields_and_wrong_shapes(field: str, value: object) -> None:
    raw = artifact().to_dict()
    raw[field] = value
    with pytest.raises(ValueError):
        ArtifactManifest.from_dict(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "2"},
        {"domain": "a\nb"},
        {"payload_json": "[]"},
        {"payload_json": '{"a":1, "a":2}'},
        {"signatures": ()},
        {"signatures": (ContentSignature(DIGEST, "one"), ContentSignature(DIGEST, "two"))},
        {"payload_json": canonical_json({"huge": "x" * (2 * 1_048_576)})},
    ],
)
def test_signed_envelope_requires_canonical_versioned_bounded_payload(
    changes: dict[str, object],
) -> None:
    envelope = SignedEnvelope("af.test.v1", "{}", (ContentSignature(DIGEST, "signature"),))
    with pytest.raises(ValueError):
        replace(envelope, **changes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("unknown", "field"),
        ("signatures", "signature"),
        ("signatures", [{}]),
    ],
)
def test_signed_envelope_parser_has_no_extensible_authority_fields(
    field: str, value: object
) -> None:
    raw = SignedEnvelope("af.test.v1", "{}", (ContentSignature(DIGEST, "signature"),)).to_dict()
    raw[field] = value
    with pytest.raises(ValueError):
        SignedEnvelope.from_dict(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"issued_at": datetime(2026, 1, 1)},
        {"expires_at": datetime(2099, 1, 1, tzinfo=UTC)},
        {"effect": "write"},
        {"egress": "internet"},
        {"schema_version": "2"},
    ],
)
def test_peer_grant_cannot_widen_execution_scope(changes: dict[str, object]) -> None:
    now = datetime.now(UTC)
    lease = PeerLease(
        "l",
        DIGEST,
        DIGEST,
        "hello",
        "1",
        DIGEST,
        DIGEST,
        DIGEST,
        DIGEST,
        "nonce",
        now,
        now + timedelta(minutes=1),
    )
    with pytest.raises(ValueError):
        replace(lease, **changes)
    raw = lease.to_dict()
    raw["command"] = "arbitrary command"
    with pytest.raises(ValueError):
        PeerLease.from_dict(raw)
