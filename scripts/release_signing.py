#!/usr/bin/env python3
"""Sign and verify bounded AF artifact statements with an operator-provided Ed25519 key.

This custom AF envelope proves integrity under an independently trusted key. It
is not a certificate, transparency-log attestation, SLSA level, or release approval.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import platform
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from release_inventory import (
    canonical,
    read_json,
    read_regular,
    sha256_file,
    source_commit,
    write_json,
)
from release_security import PINS

DOMAIN = b"AF-ARTIFACT-PROVENANCE-v1\x00"


def validate_key_policy(
    policy: dict[str, Any], key_id: str, *, now: datetime | None = None
) -> None:
    """Validate an independently supplied policy; do not authenticate its reviewer."""
    if (
        set(policy)
        != {"schema", "key_id", "valid_from", "valid_until", "revoked", "owner", "review_reference"}
        or policy["schema"] != "af-signing-key-policy/v1"
        or policy["key_id"] != key_id
        or policy["revoked"] is not False
        or any(
            not isinstance(policy[field], str) or not 1 <= len(policy[field]) <= 512
            for field in ("owner", "review_reference")
        )
    ):
        raise ValueError("key policy identity, revocation or review reference invalid")
    start = datetime.fromisoformat(policy["valid_from"])
    end = datetime.fromisoformat(policy["valid_until"])
    observed = now or datetime.now(UTC)
    if start.tzinfo is None or end.tzinfo is None or not start <= observed < end:
        raise ValueError("key policy validity interval invalid")


def validate_scan(scan: dict[str, Any], commit: str, lock_digest: str) -> None:
    if (
        scan.get("schema") != "af-security-scan/v1"
        or scan.get("source_commit") != commit
        or scan.get("source_tree") != "clean"
        or scan.get("status") != "pass"
        or scan.get("lock_sha256") != lock_digest
        or scan.get("tools") != PINS
        or scan.get("certifies_production") is not False
        or not isinstance(scan.get("checks"), dict)
        or set(scan["checks"]) != set(PINS)
        or any(
            not isinstance(item, dict) or item.get("status") != "pass"
            for item in scan["checks"].values()
        )
    ):
        raise ValueError("matching passing security report required")
    observed = datetime.fromisoformat(scan["observed_at"])
    if observed.tzinfo is None or not timedelta(0) <= datetime.now(UTC) - observed <= timedelta(
        hours=24
    ):
        raise ValueError("security report must be fresh within 24 hours")


def file_identities(files: dict[str, Path]) -> dict[str, dict[str, str]]:
    if (
        not files
        or len(files) > 16
        or any(not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", key) for key in files)
    ):
        raise ValueError("bounded named files required")
    if len({path.name for path in files.values()}) != len(files):
        raise ValueError("ambiguous artifact basenames")
    return {
        name: {"name": path.name, "sha256": sha256_file(path)}
        for name, path in sorted(files.items())
    }


def create_statement(
    commit: str,
    artifacts: dict[str, Path],
    materials: dict[str, Path],
    *,
    now: datetime | None = None,
    lifetime_hours: int = 24,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("full source commit required")
    if not 1 <= lifetime_hours <= 168:
        raise ValueError("statement lifetime must be one to 168 hours")
    observed = now or datetime.now(UTC)
    if observed.tzinfo is None:
        raise ValueError("timezone-aware observation required")
    return {
        "schema": "af-artifact-provenance/v1",
        "source_commit": commit,
        "source_tree": "clean",
        "artifacts": file_identities(artifacts),
        "materials": file_identities(materials) if materials else {},
        "created_at": observed.astimezone(UTC).isoformat(),
        "expires_at": (observed + timedelta(hours=lifetime_hours)).astimezone(UTC).isoformat(),
        "builder": {
            "tool": "af-release-signing/v1",
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "certifies_production": False,
    }


def sign_statement(
    statement: dict[str, Any], key: Path, password_file: Path | None = None
) -> dict[str, Any]:
    password = (
        read_regular(password_file, private=True, limit=1024).rstrip(b"\r\n")
        if password_file
        else None
    )
    private = serialization.load_pem_private_key(
        read_regular(key, private=True, limit=16384), password
    )
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError("Ed25519 private key required")
    public = private.public_key().public_bytes_raw()
    return {
        "algorithm": "Ed25519",
        "key_id": hashlib.sha256(public).hexdigest(),
        "statement": statement,
        "signature": base64.b64encode(private.sign(DOMAIN + canonical(statement))).decode("ascii"),
    }


def verify_statement(
    envelope: dict[str, Any],
    public_key: Path,
    artifacts: dict[str, Path],
    materials: dict[str, Path],
    *,
    expected_commit: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if (
        set(envelope) != {"algorithm", "key_id", "statement", "signature"}
        or envelope["algorithm"] != "Ed25519"
    ):
        raise ValueError("unsupported signature envelope")
    public = serialization.load_pem_public_key(read_regular(public_key, limit=16384))
    if (
        not isinstance(public, Ed25519PublicKey)
        or envelope["key_id"] != hashlib.sha256(public.public_bytes_raw()).hexdigest()
    ):
        raise ValueError("signature key does not match trusted public key")
    statement = envelope["statement"]
    try:
        signature = base64.b64decode(envelope["signature"], validate=True)
        public.verify(signature, DOMAIN + canonical(statement))
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise ValueError("invalid signature") from exc
    if (
        not isinstance(statement, dict)
        or set(statement)
        != {
            "schema",
            "source_commit",
            "source_tree",
            "artifacts",
            "materials",
            "created_at",
            "expires_at",
            "builder",
            "certifies_production",
        }
        or statement["schema"] != "af-artifact-provenance/v1"
        or statement["certifies_production"] is not False
    ):
        raise ValueError("unsupported signed statement")
    if statement["source_commit"] != expected_commit or statement["source_tree"] != "clean":
        raise ValueError("source identity mismatch")
    created = datetime.fromisoformat(statement["created_at"])
    expiry = datetime.fromisoformat(statement["expires_at"])
    observed = now or datetime.now(UTC)
    if (
        created.tzinfo is None
        or expiry.tzinfo is None
        or not created <= observed < expiry
        or not timedelta(0) < expiry - created <= timedelta(days=7)
    ):
        raise ValueError("statement expiry or observation invalid")
    if statement["artifacts"] != file_identities(artifacts) or statement["materials"] != (
        file_identities(materials) if materials else {}
    ):
        raise ValueError("artifact or material digest mismatch")
    return statement


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sign", "verify"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--scan", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument(
        "--configuration", type=Path, required=True, help="reviewed non-secret configuration"
    )
    parser.add_argument(
        "--key-policy", type=Path, required=True, help="independently reviewed signing key policy"
    )
    parser.add_argument(
        "--key",
        type=Path,
        required=True,
        help="private key for sign; trusted public key for verify",
    )
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--envelope", type=Path, required=True)
    parser.add_argument("--lifetime-hours", type=int, default=24)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        commit = source_commit(args.root)
        artifacts = {"sdist": args.sdist, "wheel": args.wheel}
        materials = {
            "sbom": args.sbom,
            "security-scan": args.scan,
            "profile": args.profile,
            "configuration": args.configuration,
            "key-policy": args.key_policy,
            "lock": args.root / "uv.lock",
            "project": args.root / "pyproject.toml",
        }
        if args.action == "verify":
            envelope = read_json(args.envelope)
            validate_key_policy(read_json(args.key_policy), envelope.get("key_id", ""))
            verify_statement(envelope, args.key, artifacts, materials, expected_commit=commit)
        elif args.execute:
            if args.envelope.resolve().is_relative_to(args.root.resolve()):
                raise ValueError("release evidence must be written outside the source checkout")
            scan = read_json(args.scan)
            validate_scan(scan, commit, sha256_file(args.root / "uv.lock"))
            bom = read_json(args.sbom)
            lock_properties = bom.get("metadata", {}).get("properties", [])
            if {
                "name": "af:uv-lock-sha256",
                "value": sha256_file(args.root / "uv.lock"),
            } not in lock_properties:
                raise ValueError("SBOM does not match source lock")
            statement = create_statement(
                commit, artifacts, materials, lifetime_hours=args.lifetime_hours
            )
            signed = sign_statement(statement, args.key, args.password_file)
            validate_key_policy(read_json(args.key_policy), signed["key_id"])
            if source_commit(args.root) != commit:
                raise ValueError("source changed during signing")
            write_json(args.envelope, signed)
        print(
            json.dumps(
                {
                    "action": args.action,
                    "executed": args.action == "verify" or args.execute,
                    "certifies_production": False,
                    "maintainer_approval": "not-asserted",
                }
            )
        )
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(
            f"provenance: {type(exc).__name__}; verification/signing not completed", file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
