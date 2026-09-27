"""Canonical signed data contracts. No manifest carries executable code or paths."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from af.framework.errors import AFError


class ContentRejected(AFError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def require_digest(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError("content digest must be canonical SHA-256")
    return value


def require_text(value: object, *, maximum: int = 256) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("content text must be bounded and non-empty")
    return value


def require_int(value: object, *, maximum: int = 2**40, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("content integer is out of bounds")
    return value


@dataclass(frozen=True, slots=True)
class ContentSignature:
    key_id: str
    signature: str

    def __post_init__(self) -> None:
        require_digest(self.key_id)
        require_text(self.signature, maximum=128)


@dataclass(frozen=True, slots=True)
class SignedEnvelope:
    domain: str
    payload_json: str
    signatures: tuple[ContentSignature, ...]
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.schema_version != "1.0.0":
            raise ValueError("unknown signed envelope version")
        require_text(self.domain, maximum=64)
        if len(self.payload_json.encode()) > 2 * 1_048_576:
            raise ValueError("signed envelope is oversized")
        value = json.loads(self.payload_json)
        if not isinstance(value, dict) or canonical_json(value) != self.payload_json:
            raise ValueError("signed payload must be a canonical JSON object")
        if type(self.signatures) is not tuple or not 1 <= len(self.signatures) <= 16:
            raise ValueError("signed envelope requires bounded immutable signatures")
        if len({signature.key_id for signature in self.signatures}) != len(self.signatures):
            raise ValueError("signature identities must be unique")

    @property
    def signing_bytes(self) -> bytes:
        return canonical_json(
            {
                "domain": self.domain,
                "payload_json": self.payload_json,
                "schema_version": self.schema_version,
            }
        ).encode()

    @property
    def digest(self) -> str:
        return content_digest(self.signing_bytes)

    def to_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "payload_json": self.payload_json,
            "schema_version": self.schema_version,
            "signatures": [
                {"key_id": item.key_id, "signature": item.signature} for item in self.signatures
            ],
        }

    @classmethod
    def from_dict(cls, value: object) -> SignedEnvelope:
        if not isinstance(value, dict) or set(value) != {
            "domain",
            "payload_json",
            "schema_version",
            "signatures",
        }:
            raise ValueError("invalid signed envelope fields")
        signatures = value["signatures"]
        if not isinstance(signatures, list) or len(signatures) > 16:
            raise ValueError("invalid signature list")
        parsed: list[ContentSignature] = []
        for signature in signatures:
            if not isinstance(signature, dict) or set(signature) != {"key_id", "signature"}:
                raise ValueError("invalid signature")
            parsed.append(
                ContentSignature(
                    require_digest(signature["key_id"]),
                    require_text(signature["signature"], maximum=128),
                )
            )
        return cls(
            require_text(value["domain"], maximum=64),
            require_text(value["payload_json"], maximum=2 * 1_048_576),
            tuple(parsed),
            require_text(value["schema_version"]),
        )


class EnvelopeSigner(Protocol):
    @property
    def key_id(self) -> str: ...

    def sign(self, domain: str, payload: Mapping[str, object]) -> SignedEnvelope: ...


class EnvelopeVerifier(Protocol):
    def verify(self, envelope: SignedEnvelope, *, domain: str) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class ContentChunk:
    digest: str
    byte_size: int

    def __post_init__(self) -> None:
        require_digest(self.digest)
        require_int(self.byte_size, minimum=1, maximum=16 * 1_048_576)


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    artifact_id: str
    revision: int
    content_digest: str
    byte_size: int
    chunks: tuple[ContentChunk, ...]
    kind: str
    format: str
    licence: str
    provenance: str
    expires_at: datetime
    runtime_ids: tuple[str, ...]
    architecture: str
    quantization: str
    minimum_memory_mib: int = 0
    locales: tuple[str, ...] = ()
    private: bool = False
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.schema_version != "1.0.0":
            raise ValueError("unknown artifact manifest version")
        for value in (
            self.artifact_id,
            self.format,
            self.licence,
            self.provenance,
            self.architecture,
            self.quantization,
        ):
            require_text(value)
        require_int(self.revision, minimum=1)
        require_int(self.byte_size, minimum=1)
        require_int(self.minimum_memory_mib)
        require_digest(self.content_digest)
        if type(self.chunks) is not tuple or not 1 <= len(self.chunks) <= 100_000:
            raise ValueError("manifest chunks must be bounded and immutable")
        if sum(chunk.byte_size for chunk in self.chunks) != self.byte_size:
            raise ValueError("manifest chunk lengths must equal artifact length")
        if self.kind not in {
            "model",
            "tokenizer",
            "configuration",
            "locale",
            "documentation",
            "fixture",
        }:
            raise ValueError("artifact kind is not eligible immutable data")
        if self.expires_at.utcoffset() is None or type(self.private) is not bool:
            raise ValueError("artifact expiry or privacy flag is invalid")
        for values in (self.runtime_ids, self.locales):
            if type(values) is not tuple or len(values) > 256:
                raise ValueError("manifest metadata must be bounded and immutable")
            for value in values:
                require_text(value)

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "content_digest": self.content_digest,
            "byte_size": self.byte_size,
            "chunks": [
                {"digest": chunk.digest, "byte_size": chunk.byte_size} for chunk in self.chunks
            ],
            "kind": self.kind,
            "format": self.format,
            "licence": self.licence,
            "provenance": self.provenance,
            "expires_at": self.expires_at.isoformat(),
            "runtime_ids": list(self.runtime_ids),
            "architecture": self.architecture,
            "quantization": self.quantization,
            "minimum_memory_mib": self.minimum_memory_mib,
            "locales": list(self.locales),
            "private": self.private,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ArtifactManifest:
        fields = set(cls.__dataclass_fields__)
        if set(data) != fields or not isinstance(data["chunks"], list):
            raise ValueError("invalid artifact manifest fields")
        chunks: list[ContentChunk] = []
        for chunk in data["chunks"]:
            if not isinstance(chunk, dict) or set(chunk) != {"digest", "byte_size"}:
                raise ValueError("invalid chunk fields")
            chunks.append(
                ContentChunk(require_digest(chunk["digest"]), require_int(chunk["byte_size"]))
            )
        runtime_ids, locales = data["runtime_ids"], data["locales"]
        if not isinstance(runtime_ids, list) or not isinstance(locales, list):
            raise ValueError("manifest metadata requires lists")
        private = data["private"]
        if type(private) is not bool:
            raise ValueError("manifest privacy flag requires boolean")
        return cls(
            require_text(data["artifact_id"]),
            require_int(data["revision"]),
            require_digest(data["content_digest"]),
            require_int(data["byte_size"]),
            tuple(chunks),
            require_text(data["kind"]),
            require_text(data["format"]),
            require_text(data["licence"]),
            require_text(data["provenance"]),
            datetime.fromisoformat(require_text(data["expires_at"])),
            tuple(require_text(item) for item in runtime_ids),
            require_text(data["architecture"]),
            require_text(data["quantization"]),
            require_int(data["minimum_memory_mib"]),
            tuple(require_text(item) for item in locales),
            private,
            require_text(data["schema_version"]),
        )
