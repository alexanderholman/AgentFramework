from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from af.framework.content import (
    ArtifactManifest,
    ContentChunk,
    ContentRejected,
    canonical_json,
    content_digest,
)
from af.infrastructure.content_transport import LoopbackContentServer, fetch_local_artifact
from af.infrastructure.trusted_content import Ed25519Identity, Ed25519Trust, VerifiedContentStore


def test_two_local_caches_resume_signed_chunks_over_real_socket(tmp_path: Path) -> None:
    identity = Ed25519Identity.generate()
    trust = Ed25519Trust({identity.key_id: identity.public_key_bytes()})
    source = VerifiedContentStore(tmp_path / "source", trust, max_bytes=100_000)
    target = VerifiedContentStore(tmp_path / "target", trust, max_bytes=100_000)
    data = b"bounded signed selector fixture"
    chunks = (data[:8], data[8:])
    manifest = ArtifactManifest(
        "selector",
        1,
        content_digest(data),
        len(data),
        tuple(ContentChunk(content_digest(chunk), len(chunk)) for chunk in chunks),
        "fixture",
        "json",
        "MIT",
        "local-test",
        datetime.now(UTC) + timedelta(minutes=5),
        ("fixture",),
        "cpu",
        "none",
    )
    envelope = identity.sign("af.artifact.v1", manifest.to_dict())
    handle = source.begin(envelope)
    for index, chunk in enumerate(chunks):
        source.write_chunk(handle, index, chunk)
    target.begin(envelope)
    target.write_chunk(handle, 0, chunks[0])
    with pytest.raises(ContentRejected, match="serving_not_enabled"):
        LoopbackContentServer(source, handle, serving_enabled=False)
    with LoopbackContentServer(source, handle, serving_enabled=True) as server:
        server.pause()
        with pytest.raises(ContentRejected, match="origin_unavailable"):
            fetch_local_artifact(
                server.endpoint,
                expected_manifest_digest=handle,
                destination=target,
                maximum_download_bytes=100_000,
            )
        server.pause(paused=False)
        output = fetch_local_artifact(
            server.endpoint,
            expected_manifest_digest=handle,
            destination=target,
            maximum_download_bytes=100_000,
        )
        assert output.read_bytes() == data
        assert server.served_bytes == len(canonical_json(envelope.to_dict()).encode()) + len(
            chunks[1]
        )
        cancelled = Event()
        cancelled.set()
        with pytest.raises(ContentRejected, match="transfer_cancelled"):
            fetch_local_artifact(
                server.endpoint,
                expected_manifest_digest=handle,
                destination=target,
                maximum_download_bytes=100_000,
                cancellation=cancelled,
            )
        with pytest.raises(ContentRejected, match="manifest_substitution"):
            fetch_local_artifact(
                server.endpoint,
                expected_manifest_digest=content_digest(b"wrong"),
                destination=target,
                maximum_download_bytes=100_000,
            )
        with pytest.raises(ContentRejected, match="download_quota"):
            fetch_local_artifact(
                server.endpoint,
                expected_manifest_digest=handle,
                destination=target,
                maximum_download_bytes=1,
            )
    assert target.materialize(handle).read_bytes() == data


def test_content_serving_and_origin_are_explicitly_bounded(tmp_path: Path) -> None:
    store = VerifiedContentStore(tmp_path, Ed25519Trust({}), max_bytes=100)
    with pytest.raises(ValueError, match="loopback"):
        fetch_local_artifact(
            "https://example.com",
            expected_manifest_digest=content_digest(b"x"),
            destination=store,
            maximum_download_bytes=100,
        )
