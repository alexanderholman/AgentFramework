#!/usr/bin/env python3
"""Pinned release scans on immutable Git objects; no installs, fixes or suppressions.

Dependency advisory requests disclose locked package names/versions to PyPI and
require --allow-advisory-network. Offline source checks never verify secrets online.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from release_inventory import (
    MAX_JSON_BYTES,
    audit_requirements,
    locked_packages,
    read_json,
    sha256_file,
    source_commit,
    source_snapshot,
    write_json,
)

PINS = {"pip-audit": "2.10.1", "bandit": "1.9.4", "detect-secrets": "1.5.0"}
Runner = Callable[[list[str], Path, Path], tuple[int, dict[str, Any]]]


def run_json_tool(
    arguments: list[str], working: Path, temporary: Path
) -> tuple[int, dict[str, Any]]:
    # Fresh homes/caches and a minimal environment avoid inherited credentials,
    # proxy settings, user config, plugin paths and stale advisory-cache results.
    environment = {
        "PATH": os.defpath,
        "HOME": str(temporary),
        "LC_ALL": "C.UTF-8",
        "XDG_CACHE_HOME": str(temporary / "cache"),
        "PYTHONNOUSERSITE": "1",
    }
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        result = subprocess.run(
            arguments,
            cwd=working,
            env=environment,
            stdout=output,
            stderr=error,
            timeout=180,
            check=False,
        )
        output.seek(0, os.SEEK_END)
        if output.tell() > MAX_JSON_BYTES:
            raise ValueError("scanner output exceeds bound")
        output.seek(0)
        raw = output.read()
        # Do not expose stderr or raw findings: scanners can include source secrets.
        target = temporary / "tool-result.json"
        target.write_bytes(raw)
        try:
            document = read_json(target)
        finally:
            target.unlink(missing_ok=True)
        return result.returncode, document


def audit_status(document: dict[str, Any], expected: set[tuple[str, str]]) -> tuple[str, int]:
    dependencies = document.get("dependencies")
    if not isinstance(dependencies, list) or len(dependencies) != len(expected):
        return "error", 0
    seen: set[tuple[str, str]] = set()
    count = 0
    for item in dependencies:
        if (
            not isinstance(item, dict)
            or "skip_reason" in item
            or not isinstance(item.get("vulns"), list)
        ):
            return "error", 0
        identity = (item.get("name"), item.get("version"))
        if identity not in expected or identity in seen:
            return "error", 0
        seen.add(identity)
        count += len(item["vulns"])
    return ("fail" if count else "pass"), count


def _source_results(name: str, document: dict[str, Any], code: int) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    if name == "bandit":
        results = document.get("results")
        if code not in {0, 1} or not isinstance(results, list) or document.get("errors") != []:
            raise ValueError("static scanner failed or skipped files")
        for item in results:
            if not isinstance(item, dict) or not isinstance(item.get("test_id"), str):
                raise ValueError("malformed static finding")
            findings.append(
                {
                    key: item.get(key)
                    for key in (
                        "filename",
                        "line_number",
                        "test_id",
                        "issue_severity",
                        "issue_confidence",
                    )
                }
            )
    else:
        results = document.get("results")
        if code != 0 or not isinstance(results, dict) or document.get("version") != PINS[name]:
            raise ValueError("secret scanner failed")
        for filename, items in results.items():
            if not isinstance(items, list):
                raise ValueError("malformed secret finding")
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("hashed_secret"), str):
                    raise ValueError("malformed secret finding")
                findings.append(
                    {
                        "filename": filename,
                        "line_number": item.get("line_number"),
                        "type": item.get("type"),
                        "fingerprint": item["hashed_secret"],
                    }
                )
    # A nonzero static exit with no findings is still an error, never clean.
    if code and not findings:
        raise ValueError("scanner exited unsuccessfully")
    return {"status": "fail" if findings else "pass", "count": len(findings), "findings": findings}


def reviewed_secret_candidates(
    snapshot: Path, result: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Apply expiring reviews bound to one exact candidate and source line.

    No path patterns, folder exclusions or advisory/static suppressions exist.
    Retain every candidate in the report, including its review disposition.
    """
    path = snapshot / "release" / "secret-reviews.json"
    if not path.exists():
        return result
    document = read_json(path)
    if set(document) != {"schema", "reviews"} or document["schema"] != "af-secret-reviews/v1":
        raise ValueError("invalid secret review ledger")
    entries = document["reviews"]
    if not isinstance(entries, list) or len(entries) > 1000:
        raise ValueError("invalid secret review count")
    observed = now or datetime.now(UTC)
    available = {(f["filename"], f["type"], f["fingerprint"]): f for f in result["findings"]}
    matched: set[tuple[str, str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "filename",
            "type",
            "fingerprint",
            "line_sha256",
            "reason",
            "reviewer",
            "reviewed_at",
            "expires_at",
        }:
            raise ValueError("invalid secret review fields")
        entry = dict(entry)
        # Byte arrays distinguish public review hashes from candidate secret strings.
        for name, size in (("fingerprint", 20), ("line_sha256", 32)):
            octets = entry[name]
            if (
                not isinstance(octets, list)
                or len(octets) != size
                or any(type(value) is not int or not 0 <= value <= 255 for value in octets)
            ):
                raise ValueError("review hash must contain exact byte values")
            entry[name] = bytes(octets).hex()
        if any(not isinstance(value, str) for value in entry.values()):
            raise ValueError("invalid secret review values")
        key = (entry["filename"], entry["type"], entry["fingerprint"])
        relative = PurePosixPath(entry["filename"])
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or key in matched
            or key not in available
        ):
            raise ValueError("stale or ambiguous secret review")
        issued = datetime.fromisoformat(entry["reviewed_at"])
        expires = datetime.fromisoformat(entry["expires_at"])
        if (
            issued.utcoffset() is None
            or expires.utcoffset() is None
            or not issued <= observed < expires
        ):
            raise ValueError("secret review is expired or future-dated")
        if not 0 < (expires - issued).total_seconds() <= 180 * 86400:
            raise ValueError("secret review expiry exceeds bound")
        if not 12 <= len(entry["reason"]) <= 500 or not 1 <= len(entry["reviewer"]) <= 100:
            raise ValueError("secret review needs a bounded reason and reviewer")
        finding = available[key]
        source = snapshot.joinpath(*relative.parts)
        if source.is_symlink() or source.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("invalid reviewed source")
        lines = source.read_bytes().splitlines()
        number = finding["line_number"]
        if type(number) is not int or not 1 <= number <= len(lines):
            raise ValueError("invalid reviewed line")
        digest = hashlib.sha256(lines[number - 1]).hexdigest()
        if digest != entry["line_sha256"]:
            raise ValueError("reviewed source line changed")
        matched.add(key)
        finding["review"] = {
            k: entry[k] for k in ("reason", "reviewer", "reviewed_at", "expires_at")
        }
    unreviewed = len(result["findings"]) - len(matched)
    return dict(
        result,
        status="fail" if unreviewed else "pass",
        unreviewed=unreviewed,
        reviewed=len(matched),
    )


def scan_snapshot(
    snapshot: Path,
    temporary: Path,
    *,
    allow_advisory_network: bool,
    runner: Runner = run_json_tool,
    version: Callable[[str], str] = importlib.metadata.version,
) -> dict[str, Any]:
    python = sys.executable
    # Explicit empty configuration avoids project-local Bandit suppressions.
    bandit_config = temporary / "bandit.yaml"
    bandit_config.write_text("{}\n")
    bandit_ini = temporary / "bandit.ini"
    bandit_ini.write_text("[bandit]\n")
    commands = {
        "bandit": [
            python,
            "-I",
            "-m",
            "bandit",
            "-r",
            "src",
            "scripts",
            "--configfile",
            str(bandit_config),
            "--ini",
            str(bandit_ini),
            "--ignore-nosec",
            "--severity-level",
            "high",
            "--confidence-level",
            "high",
            "--format",
            "json",
            "--quiet",
        ],
        "detect-secrets": [
            str(Path(python).parent / "detect-secrets"),
            "scan",
            "--all-files",
            "--no-verify",
            "--disable-filter",
            "detect_secrets.filters.allowlist.is_line_allowlisted",
            ".",
        ],
    }
    checks: dict[str, Any] = {}
    for name, arguments in commands.items():
        try:
            if version(name) != PINS[name]:
                raise ValueError("scanner version differs from exact pin")
            code, document = runner(arguments, snapshot, temporary)
            checks[name] = _source_results(name, document, code)
            if name == "detect-secrets":
                checks[name] = reviewed_secret_candidates(snapshot, checks[name])
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            importlib.metadata.PackageNotFoundError,
            subprocess.SubprocessError,
        ):
            checks[name] = {"status": "error", "reason": "missing, mismatched or failed scanner"}
    if not allow_advisory_network:
        checks["pip-audit"] = {"status": "unknown", "reason": "advisory network not authorized"}
    else:
        try:
            if version("pip-audit") != PINS["pip-audit"]:
                raise ValueError("scanner version differs from exact pin")
            requirements = temporary / "locked-requirements.txt"
            requirements.write_text(audit_requirements(snapshot / "uv.lock"))
            _, packages = locked_packages(snapshot / "uv.lock")
            code, document = runner(
                [
                    python,
                    "-I",
                    "-m",
                    "pip_audit",
                    "--requirement",
                    str(requirements),
                    "--no-deps",
                    "--disable-pip",
                    "--require-hashes",
                    "--strict",
                    "--format",
                    "json",
                    "--progress-spinner",
                    "off",
                    "--timeout",
                    "15",
                    "--vulnerability-service",
                    "pypi",
                    "--cache-dir",
                    str(temporary / "audit-cache"),
                ],
                snapshot,
                temporary,
            )
            status, count = audit_status(document, {(p["name"], p["version"]) for p in packages})
            if code not in {0, 1} or (code != 0 and status == "pass"):
                status = "error"
            # Only package coordinates, advisory IDs and fix versions are retained.
            findings = [
                {
                    "name": p.get("name"),
                    "version": p.get("version"),
                    "advisories": [
                        {"id": v.get("id"), "fix_versions": v.get("fix_versions", [])}
                        for v in p.get("vulns", [])
                    ],
                }
                for p in document.get("dependencies", [])
                if p.get("vulns")
            ]
            checks["pip-audit"] = {"status": status, "count": count, "findings": findings}
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            importlib.metadata.PackageNotFoundError,
            subprocess.SubprocessError,
        ):
            checks["pip-audit"] = {
                "status": "error",
                "reason": "missing, mismatched or failed scanner",
            }
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-advisory-network", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        print(
            json.dumps(
                {
                    "tools": PINS,
                    "advisory_network": args.allow_advisory_network,
                    "executed": False,
                    "certifies_production": False,
                }
            )
        )
        return 0
    try:
        root = args.root.resolve()
        if args.output.resolve().is_relative_to(root):
            raise ValueError("release evidence must be written outside source checkout")
        commit = source_commit(root)
        with tempfile.TemporaryDirectory(prefix="af-security-") as directory:
            temporary = Path(directory)
            snapshot = temporary / "source"
            snapshot.mkdir()
            source_snapshot(root, snapshot, commit)
            checks = scan_snapshot(
                snapshot, temporary, allow_advisory_network=args.allow_advisory_network
            )
            lock_digest = sha256_file(snapshot / "uv.lock")
        if source_commit(root) != commit:
            raise ValueError("source changed during scan")
        status = (
            "pass" if all(check["status"] == "pass" for check in checks.values()) else "blocked"
        )
        report = {
            "schema": "af-security-scan/v1",
            "source_commit": commit,
            "source_tree": "clean",
            "lock_sha256": lock_digest,
            "observed_at": datetime.now(UTC).isoformat(),
            "python": platform.python_version(),
            "system": platform.system(),
            "tools": PINS,
            "policy": (
                "all advisories; high-severity/high-confidence static; "
                "all secret candidates retained; exact expiring false-positive reviews only"
            ),
            "checks": checks,
            "status": status,
            "certifies_production": False,
        }
        write_json(args.output, report)
        print(
            json.dumps(
                {
                    "status": status,
                    "checks": {name: item["status"] for name, item in checks.items()},
                    "certifies_production": False,
                }
            )
        )
        return 0 if status == "pass" else 1
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"security: {type(exc).__name__}; no release evidence issued", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
