from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from af.framework.content import ArtifactManifest, ContentChunk, ContentRejected
from af.infrastructure.trusted_content import Ed25519Identity, Ed25519Trust, VerifiedContentStore


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def manifest(data: bytes = b"model weights", *, revision: int = 1) -> ArtifactManifest:
    pieces = (data[:5], data[5:])
    return ArtifactManifest(
        artifact_id="selector.fixture",
        revision=revision,
        content_digest=digest(data),
        byte_size=len(data),
        chunks=tuple(ContentChunk(digest(piece), len(piece)) for piece in pieces),
        kind="model",
        format="gguf",
        licence="Apache-2.0",
        provenance="local.test",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        runtime_ids=("llama.cpp",),
        architecture="cpu",
        quantization="q4",
    )


def test_signed_chunk_resume_restart_and_offline_materialization(tmp_path: Path) -> None:
    publisher = Ed25519Identity.generate()
    trust = Ed25519Trust({publisher.key_id: publisher.public_key_bytes()})
    store = VerifiedContentStore(tmp_path, trust, max_bytes=1000)
    artifact = manifest()
    signed = publisher.sign("af.artifact.v1", artifact.to_dict())
    handle = store.begin(signed)
    store.write_chunk(handle, 0, b"model")
    assert store.missing_chunks(handle) == (1,)
    reopened = VerifiedContentStore(tmp_path, trust, max_bytes=1000)
    assert reopened.begin(signed) == handle
    reopened.write_chunk(handle, 1, b" weights")
    path = reopened.materialize(handle)
    assert path.read_bytes() == b"model weights"
    assert digest(path.read_bytes()) == artifact.content_digest
    assert reopened.read_chunk(handle, 0) == b"model"
    reopened.remove(handle)
    assert not path.exists()
    assert reopened.stored_bytes == 0


def test_corrupt_oversized_unknown_publisher_and_substitution_are_rejected(tmp_path: Path) -> None:
    publisher = Ed25519Identity.generate()
    trust = Ed25519Trust({publisher.key_id: publisher.public_key_bytes()})
    store = VerifiedContentStore(tmp_path, trust, max_bytes=1000)
    artifact = manifest()
    with pytest.raises(ContentRejected, match="untrusted_signature"):
        store.begin(Ed25519Identity.generate().sign("af.artifact.v1", artifact.to_dict()))
    handle = store.begin(publisher.sign("af.artifact.v1", artifact.to_dict()))
    for invalid in (b"other", b"model long"):
        with pytest.raises(ContentRejected, match="chunk_integrity"):
            store.write_chunk(handle, 0, invalid)
    with pytest.raises(ContentRejected, match="incomplete"):
        store.materialize(handle)
    envelope = publisher.sign("af.artifact.v1", artifact.to_dict())
    with pytest.raises(ContentRejected, match="untrusted_signature"):
        store.begin(replace(envelope, payload_json=envelope.payload_json.replace("q4", "q8")))


def test_revocation_expiry_rollback_quota_and_executable_rejection(tmp_path: Path) -> None:
    publisher = Ed25519Identity.generate()
    trust = Ed25519Trust({publisher.key_id: publisher.public_key_bytes()})
    store = VerifiedContentStore(tmp_path, trust, max_bytes=14)
    current = manifest(revision=2)
    handle = store.begin(publisher.sign("af.artifact.v1", current.to_dict()))
    with pytest.raises(ContentRejected, match="rollback"):
        store.begin(publisher.sign("af.artifact.v1", manifest().to_dict()))
    with pytest.raises(ContentRejected, match="expired"):
        store.begin(
            publisher.sign(
                "af.artifact.v1",
                replace(
                    current, revision=3, expires_at=datetime.now(UTC) - timedelta(seconds=1)
                ).to_dict(),
            )
        )
    with pytest.raises(ValueError, match="kind"):
        replace(current, kind="executable")
    store.write_chunk(handle, 0, b"model")
    store.write_chunk(handle, 1, b" weights")
    with pytest.raises(ContentRejected, match="quota"):
        store.materialize(handle)
    trust.revoke(publisher.key_id)
    with pytest.raises(ContentRejected, match="untrusted_signature"):
        store.read_chunk(handle, 0)


def test_multisignature_threshold_and_explicit_key_rotation(tmp_path: Path) -> None:
    first, second = Ed25519Identity.generate(), Ed25519Identity.generate()
    trust = Ed25519Trust(
        {
            first.key_id: first.public_key_bytes(),
            second.key_id: second.public_key_bytes(),
        },
        threshold=2,
    )
    store = VerifiedContentStore(tmp_path, trust, max_bytes=1000)
    one = first.sign("af.artifact.v1", manifest().to_dict())
    with pytest.raises(ContentRejected, match="untrusted_signature"):
        store.begin(one)
    two = second.cosign(one)
    store.begin(two)
    trust.revoke(first.key_id)
    with pytest.raises(ContentRejected, match="untrusted_signature"):
        store.begin(two)


def test_cache_corruption_and_complete_artifact_digest_are_checked(tmp_path: Path) -> None:
    publisher = Ed25519Identity.generate()
    trust = Ed25519Trust({publisher.key_id: publisher.public_key_bytes()})
    store = VerifiedContentStore(tmp_path, trust, max_bytes=1000)
    wrong = replace(manifest(), content_digest=digest(b"wrong assembled bytes"))
    handle = store.begin(publisher.sign("af.artifact.v1", wrong.to_dict()))
    store.write_chunk(handle, 0, b"model")
    store.write_chunk(handle, 0, b"model")
    store.write_chunk(handle, 1, b" weights")
    with pytest.raises(ContentRejected, match="artifact_integrity"):
        store.materialize(handle)
    with sqlite3.connect(tmp_path / "content.sqlite3") as connection:
        connection.execute("UPDATE chunks SET data=? WHERE digest=?", (b"xxxxx", digest(b"model")))
    with pytest.raises(ContentRejected, match="cache_corruption"):
        store.read_chunk(handle, 0)
    with pytest.raises(ContentRejected, match="cache_corruption"):
        store.write_chunk(handle, 0, b"model")
    with pytest.raises(ContentRejected, match="chunk_index"):
        store.write_chunk(handle, 99, b"data")
    with pytest.raises(ContentRejected, match="chunk_index"):
        store.read_chunk(handle, -1)


def test_materialized_content_is_rehashed_and_eviction_preserves_pins(tmp_path: Path) -> None:
    publisher = Ed25519Identity.generate()
    trust = Ed25519Trust({publisher.key_id: publisher.public_key_bytes()})
    store = VerifiedContentStore(tmp_path, trust, max_bytes=1000)
    handle = store.begin(publisher.sign("af.artifact.v1", manifest().to_dict()))
    store.write_chunk(handle, 0, b"model")
    store.write_chunk(handle, 1, b" weights")
    target = store.materialize(handle)
    assert store.materialize(handle) == target
    store.pin(handle)
    with pytest.raises(ContentRejected, match="pinned"):
        store.remove(handle)
    assert store.evict_unpinned() == ()
    target.write_bytes(b"corrupt")
    with pytest.raises(ContentRejected, match="cache_corruption"):
        store.materialize(handle)
    store.pin(handle, pinned=False)
    assert store.evict_unpinned() == (handle,)
    with pytest.raises(ContentRejected, match="unknown_manifest"):
        store.read_chunk(handle, 0)


def test_persistent_manifest_revocation_and_cross_artifact_quota(tmp_path: Path) -> None:
    publisher = Ed25519Identity.generate()
    trust = Ed25519Trust({publisher.key_id: publisher.public_key_bytes()})
    store = VerifiedContentStore(tmp_path, trust, max_bytes=20)
    first = manifest()
    handle = store.begin(publisher.sign("af.artifact.v1", first.to_dict()))
    store.write_chunk(handle, 0, b"model")
    store.write_chunk(handle, 1, b" weights")
    second = replace(manifest(b"other content"), artifact_id="other")
    another = store.begin(publisher.sign("af.artifact.v1", second.to_dict()))
    store.write_chunk(another, 0, b"other")
    with pytest.raises(ContentRejected, match="quota"):
        store.write_chunk(another, 1, b" content")
    store.revoke(handle)
    reopened = VerifiedContentStore(tmp_path, trust, max_bytes=20)
    with pytest.raises(ContentRejected, match="manifest_revoked"):
        reopened.read_chunk(handle, 0)
    with pytest.raises(ContentRejected, match="manifest_revoked"):
        reopened.begin(publisher.sign("af.artifact.v1", first.to_dict()))


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"private": True}, "encryption"),
        ({"licence": "unresolved"}, "licence"),
    ],
)
def test_private_and_unresolved_licence_assets_fail_closed(
    tmp_path: Path,
    change: dict[str, object],
    reason: str,
) -> None:
    publisher = Ed25519Identity.generate()
    trust = Ed25519Trust({publisher.key_id: publisher.public_key_bytes()})
    store = VerifiedContentStore(tmp_path, trust, max_bytes=1000)
    with pytest.raises(ContentRejected, match=reason):
        store.begin(publisher.sign("af.artifact.v1", replace(manifest(), **change).to_dict()))


def test_key_material_recovery_domain_separation_and_revoked_rotation() -> None:
    publisher = Ed25519Identity.generate()
    recovered = Ed25519Identity.from_private_bytes(publisher.private_key_bytes())
    assert publisher.key_id == recovered.key_id
    with pytest.raises(ValueError, match="fingerprint"):
        Ed25519Trust({digest(b"unrelated"): publisher.public_key_bytes()})
    with pytest.raises(ValueError, match="threshold"):
        Ed25519Trust({}, threshold=0)
    trust = Ed25519Trust({publisher.key_id: publisher.public_key_bytes()})
    envelope = publisher.sign("af.artifact.v1", manifest().to_dict())
    with pytest.raises(ContentRejected, match="signature_domain"):
        trust.verify(envelope, domain="af.peer.request.v1")
    trust.revoke(publisher.key_id)
    with pytest.raises(ContentRejected, match="revoked_key"):
        trust.trust(publisher.key_id, publisher.public_key_bytes())


def test_manifest_size_limit_and_rollback_apply_after_restart(tmp_path: Path) -> None:
    publisher = Ed25519Identity.generate()
    trust = Ed25519Trust({publisher.key_id: publisher.public_key_bytes()})
    with pytest.raises(ValueError, match="quota"):
        VerifiedContentStore(tmp_path, trust, max_bytes=0)
    small = VerifiedContentStore(tmp_path, trust, max_bytes=10)
    with pytest.raises(ContentRejected, match="quota"):
        small.begin(publisher.sign("af.artifact.v1", manifest().to_dict()))
    store = VerifiedContentStore(tmp_path, trust, max_bytes=1000)
    old = store.begin(publisher.sign("af.artifact.v1", manifest().to_dict()))
    store.begin(publisher.sign("af.artifact.v1", manifest(revision=2).to_dict()))
    with pytest.raises(ContentRejected, match="rollback"):
        store.read_chunk(old, 0)
