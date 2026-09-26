"""Ed25519 trust and an offline, quota-bound, resumable immutable content cache."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from af.framework.content import (
    ArtifactManifest,
    ContentRejected,
    ContentSignature,
    EnvelopeVerifier,
    SignedEnvelope,
    canonical_json,
    content_digest,
    require_digest,
)


class Ed25519Identity:
    """A locally controlled signing key. Keys are never embedded in manifests."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key

    @classmethod
    def generate(cls) -> Ed25519Identity:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_private_bytes(cls, value: bytes) -> Ed25519Identity:
        return cls(Ed25519PrivateKey.from_private_bytes(value))

    def private_key_bytes(self) -> bytes:
        return self._private_key.private_bytes_raw()

    def public_key_bytes(self) -> bytes:
        return self._private_key.public_key().public_bytes_raw()

    @property
    def key_id(self) -> str:
        return content_digest(self.public_key_bytes())

    def sign(self, domain: str, payload: Mapping[str, object]) -> SignedEnvelope:
        placeholder = ContentSignature(self.key_id, "unsigned")
        envelope = SignedEnvelope(domain, canonical_json(dict(payload)), (placeholder,))
        signature = base64.b64encode(self._private_key.sign(envelope.signing_bytes)).decode()
        return replace(envelope, signatures=(ContentSignature(self.key_id, signature),))

    def cosign(self, envelope: SignedEnvelope) -> SignedEnvelope:
        signature = base64.b64encode(self._private_key.sign(envelope.signing_bytes)).decode()
        return replace(
            envelope, signatures=(*envelope.signatures, ContentSignature(self.key_id, signature))
        )


class Ed25519Trust:
    """Only explicit local key material establishes trust; advertisements cannot."""

    def __init__(self, public_keys: Mapping[str, bytes], *, threshold: int = 1) -> None:
        if type(threshold) is not int or not 1 <= threshold <= 16:
            raise ValueError("signature threshold must be between one and sixteen")
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._revoked: set[str] = set()
        self._threshold = threshold
        self._lock = RLock()
        for key_id, material in public_keys.items():
            self.trust(key_id, material)

    def trust(self, key_id: str, public_key: bytes) -> None:
        if content_digest(public_key) != require_digest(key_id):
            raise ValueError("public key fingerprint mismatch")
        key = Ed25519PublicKey.from_public_bytes(public_key)
        with self._lock:
            if key_id in self._revoked:
                raise ContentRejected("revoked_key")
            self._keys[key_id] = key

    def revoke(self, key_id: str) -> None:
        with self._lock:
            self._revoked.add(require_digest(key_id))

    def verify(self, envelope: SignedEnvelope, *, domain: str) -> Mapping[str, object]:
        if envelope.domain != domain:
            raise ContentRejected("signature_domain")
        valid: set[str] = set()
        with self._lock:
            for signature in envelope.signatures:
                key = self._keys.get(signature.key_id)
                if key is None or signature.key_id in self._revoked:
                    continue
                try:
                    raw = base64.b64decode(signature.signature, validate=True)
                    key.verify(raw, envelope.signing_bytes)
                except (InvalidSignature, binascii.Error, ValueError):
                    continue
                valid.add(signature.key_id)
        if len(valid) < self._threshold:
            raise ContentRejected("untrusted_signature")
        data: object = json.loads(envelope.payload_json)
        if not isinstance(data, dict):
            raise ContentRejected("invalid_payload")
        return data


class VerifiedContentStore:
    """Chunk transport is caller supplied. No serving or network access is implicit.

    Quota covers stored chunk bytes plus materialized bytes; SQLite overhead is not
    included. Materialization streams verified chunks and requires temporary capacity.
    """

    def __init__(
        self,
        root: Path,
        trust: EnvelopeVerifier,
        *,
        max_bytes: int,
        allowed_licenses: tuple[str, ...] = ("Apache-2.0", "MIT", "BSD-3-Clause", "CC0-1.0"),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if type(max_bytes) is not int or not 1 <= max_bytes <= 2**50:
            raise ValueError("content quota must be positive and bounded")
        root.mkdir(parents=True, exist_ok=True)
        self._root = root.resolve()
        self._db = self._root / "content.sqlite3"
        self._trust, self._max_bytes = trust, max_bytes
        self._licenses, self._clock = frozenset(allowed_licenses), clock
        self._lock = RLock()
        with self._transaction() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS manifests (
                    digest TEXT PRIMARY KEY, envelope TEXT NOT NULL,
                    artifact_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    materialized_size INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    artifact_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, digest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (digest TEXT PRIMARY KEY, data BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS references_to_chunks (
                    manifest TEXT NOT NULL, position INTEGER NOT NULL, chunk TEXT NOT NULL,
                    PRIMARY KEY (manifest, position)
                );
                CREATE TABLE IF NOT EXISTS revocations (digest TEXT PRIMARY KEY);
            """)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = sqlite3.connect(self._db, timeout=10)
            try:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    yield connection
            finally:
                connection.close()

    def _verify(self, envelope: SignedEnvelope) -> ArtifactManifest:
        manifest = ArtifactManifest.from_dict(self._trust.verify(envelope, domain="af.artifact.v1"))
        if manifest.expires_at <= self._clock():
            raise ContentRejected("manifest_expired")
        if manifest.private:
            raise ContentRejected("private_content_requires_encryption")
        if manifest.licence not in self._licenses:
            raise ContentRejected("licence_denied")
        if manifest.byte_size > self._max_bytes:
            raise ContentRejected("quota_exceeded")
        return manifest

    def begin(self, envelope: SignedEnvelope) -> str:
        manifest = self._verify(envelope)
        handle = envelope.digest
        with self._transaction() as connection:
            self._check_revocation(connection, handle)
            current = connection.execute(
                "SELECT revision, digest FROM revisions WHERE artifact_id=?",
                (manifest.artifact_id,),
            ).fetchone()
            if current and (
                manifest.revision < current[0]
                or (manifest.revision == current[0] and handle != current[1])
            ):
                raise ContentRejected("rollback_or_equivocation")
            connection.execute(
                "INSERT OR REPLACE INTO revisions VALUES (?, ?, ?)",
                (manifest.artifact_id, manifest.revision, handle),
            )
            connection.execute(
                "INSERT OR IGNORE INTO manifests (digest,envelope,artifact_id,revision) "
                "VALUES (?,?,?,?)",
                (
                    handle,
                    canonical_json(envelope.to_dict()),
                    manifest.artifact_id,
                    manifest.revision,
                ),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO references_to_chunks VALUES (?, ?, ?)",
                [(handle, index, chunk.digest) for index, chunk in enumerate(manifest.chunks)],
            )
        return handle

    def _manifest(self, connection: sqlite3.Connection, handle: str) -> ArtifactManifest:
        require_digest(handle)
        self._check_revocation(connection, handle)
        row = connection.execute(
            "SELECT envelope FROM manifests WHERE digest=?", (handle,)
        ).fetchone()
        if row is None:
            raise ContentRejected("unknown_manifest")
        envelope = SignedEnvelope.from_dict(json.loads(row[0]))
        manifest = self._verify(envelope)
        highwater = connection.execute(
            "SELECT revision FROM revisions WHERE artifact_id=?", (manifest.artifact_id,)
        ).fetchone()
        if highwater is None or manifest.revision < highwater[0]:
            raise ContentRejected("rollback_or_equivocation")
        return manifest

    @staticmethod
    def _check_revocation(connection: sqlite3.Connection, handle: str) -> None:
        if connection.execute("SELECT 1 FROM revocations WHERE digest=?", (handle,)).fetchone():
            raise ContentRejected("manifest_revoked")

    @staticmethod
    def _bytes(connection: sqlite3.Connection) -> int:
        row = connection.execute("""SELECT
            (SELECT COALESCE(SUM(length(data)),0) FROM chunks) +
            (SELECT COALESCE(SUM(materialized_size),0) FROM manifests)""").fetchone()
        return int(row[0])

    @property
    def stored_bytes(self) -> int:
        with self._transaction() as connection:
            return self._bytes(connection)

    def write_chunk(self, handle: str, index: int, data: bytes) -> None:
        with self._transaction() as connection:
            manifest = self._manifest(connection, handle)
            if type(index) is not int or not 0 <= index < len(manifest.chunks):
                raise ContentRejected("chunk_index")
            chunk = manifest.chunks[index]
            if (
                type(data) is not bytes
                or len(data) != chunk.byte_size
                or content_digest(data) != chunk.digest
            ):
                raise ContentRejected("chunk_integrity")
            existing = connection.execute(
                "SELECT data FROM chunks WHERE digest=?", (chunk.digest,)
            ).fetchone()
            if existing is not None:
                if bytes(existing[0]) != data:
                    raise ContentRejected("cache_corruption")
                return
            if self._bytes(connection) + len(data) > self._max_bytes:
                raise ContentRejected("quota_exceeded")
            connection.execute("INSERT INTO chunks VALUES (?, ?)", (chunk.digest, data))

    def missing_chunks(self, handle: str) -> tuple[int, ...]:
        with self._transaction() as connection:
            manifest = self._manifest(connection, handle)
            return tuple(
                index
                for index, chunk in enumerate(manifest.chunks)
                if not connection.execute(
                    "SELECT 1 FROM chunks WHERE digest=?", (chunk.digest,)
                ).fetchone()
            )

    def read_chunk(self, handle: str, index: int) -> bytes:
        with self._transaction() as connection:
            manifest = self._manifest(connection, handle)
            return self._read_chunk(connection, manifest, index)

    def manifest_envelope(self, handle: str) -> SignedEnvelope:
        with self._transaction() as connection:
            self._manifest(connection, handle)
            row = connection.execute(
                "SELECT envelope FROM manifests WHERE digest=?", (handle,)
            ).fetchone()
            return SignedEnvelope.from_dict(json.loads(row[0]))

    @staticmethod
    def _read_chunk(
        connection: sqlite3.Connection, manifest: ArtifactManifest, index: int
    ) -> bytes:
        if type(index) is not int or not 0 <= index < len(manifest.chunks):
            raise ContentRejected("chunk_index")
        chunk = manifest.chunks[index]
        row = connection.execute(
            "SELECT data FROM chunks WHERE digest=?", (chunk.digest,)
        ).fetchone()
        if row is None:
            raise ContentRejected("content_incomplete")
        data = bytes(row[0])
        if len(data) != chunk.byte_size or content_digest(data) != chunk.digest:
            raise ContentRejected("cache_corruption")
        return data

    def materialize(self, handle: str) -> Path:
        with self._transaction() as connection:
            manifest = self._manifest(connection, handle)
            target = self._root / handle.removeprefix("sha256:")
            row = connection.execute(
                "SELECT materialized_size FROM manifests WHERE digest=?", (handle,)
            ).fetchone()
            existing_size = int(row[0])
            if existing_size and target.is_file() and not target.is_symlink():
                with target.open("rb") as stream:
                    actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
                if (
                    actual == manifest.content_digest
                    and target.stat().st_size == manifest.byte_size
                ):
                    return target
                raise ContentRejected("cache_corruption")
            if self._bytes(connection) - existing_size + manifest.byte_size > self._max_bytes:
                raise ContentRejected("quota_exceeded")
            temporary = target.with_suffix(".partial")
            temporary.unlink(missing_ok=True)
            hasher = hashlib.sha256()
            try:
                # Exclusive creation refuses attacker-supplied symlinks and stale partial files.
                with temporary.open("xb") as stream:
                    for index in range(len(manifest.chunks)):
                        data = self._read_chunk(connection, manifest, index)
                        stream.write(data)
                        hasher.update(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if "sha256:" + hasher.hexdigest() != manifest.content_digest:
                    raise ContentRejected("artifact_integrity")
                # Re-check current trust immediately before exposing complete bytes.
                self._manifest(connection, handle)
                os.replace(temporary, target)
                connection.execute(
                    "UPDATE manifests SET materialized_size=? WHERE digest=?",
                    (manifest.byte_size, handle),
                )
            finally:
                temporary.unlink(missing_ok=True)
            return target

    def pin(self, handle: str, *, pinned: bool = True) -> None:
        with self._transaction() as connection:
            self._manifest(connection, handle)
            connection.execute(
                "UPDATE manifests SET pinned=? WHERE digest=?", (int(pinned), handle)
            )

    def revoke(self, handle: str) -> None:
        require_digest(handle)
        with self._transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO revocations VALUES (?)", (handle,))

    def remove(self, handle: str) -> None:
        require_digest(handle)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT pinned FROM manifests WHERE digest=?", (handle,)
            ).fetchone()
            if row is not None and row[0]:
                raise ContentRejected("artifact_pinned")
            (self._root / handle.removeprefix("sha256:")).unlink(missing_ok=True)
            connection.execute("DELETE FROM manifests WHERE digest=?", (handle,))
            connection.execute("DELETE FROM references_to_chunks WHERE manifest=?", (handle,))
            connection.execute(
                "DELETE FROM chunks WHERE digest NOT IN (SELECT chunk FROM references_to_chunks)"
            )

    def evict_unpinned(self) -> tuple[str, ...]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT digest FROM manifests WHERE pinned=0 ORDER BY digest"
            ).fetchall()
        handles = tuple(str(row[0]) for row in rows)
        for handle in handles:
            self.remove(handle)
        return handles
