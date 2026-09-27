#!/usr/bin/env python3
"""Offline, deterministic inventory of every resolution in AF's uv lock.

This is a design-time CycloneDX inventory, not an installed-host inventory. It
includes development/optional dependencies and all platform resolutions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

MAX_JSON_BYTES = 16 * 1024 * 1024


def read_regular(path: Path, *, private: bool = False, limit: int = MAX_JSON_BYTES) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("input must be a bounded regular file")
        if private and (before.st_mode & 0o077 or before.st_uid != os.getuid()):
            raise ValueError("key must be an owner-controlled private file")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or len(data) > limit:
            raise ValueError("input changed during inspection")
        return data


def sha256_file(path: Path) -> str:
    # Distribution files may be larger than JSON evidence, but never unbounded.
    return hashlib.sha256(read_regular(path, limit=512 * 1024 * 1024)).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def read_json(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in items:
            if name in result:
                raise ValueError("duplicate JSON field")
            result[name] = value
        return result

    result = json.loads(read_regular(path), object_pairs_hook=pairs)
    if not isinstance(result, dict):
        raise ValueError("JSON object required")
    canonical(result)  # Also reject NaN/Infinity in nested values.
    return result


def write_json(path: Path, value: object) -> None:
    data = canonical(value) + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        owned = os.fstat(stream.fileno())
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            present = path.stat(follow_symlinks=False)
            if (present.st_dev, present.st_ino) != (owned.st_dev, owned.st_ino):
                raise ValueError("output was replaced while writing")
        except BaseException:
            try:
                present = path.stat(follow_symlinks=False)
                if (present.st_dev, present.st_ino) == (owned.st_dev, owned.st_ino):
                    path.unlink()
            except OSError:
                pass
            raise


def git(root: Path, *arguments: str) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("git executable required")
    environment = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LC_ALL": "C",
    }
    result = subprocess.run(
        [
            executable,
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(root),
            *arguments,
        ],
        env=environment,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode or len(result.stdout) > 128 * 1024 * 1024:
        raise ValueError("source identity command failed")
    return result.stdout


def source_commit(root: Path) -> str:
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("release evidence requires a clean committed source tree")
    commit = git(root, "rev-parse", "--verify", "HEAD").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("full source commit required")
    return commit


def source_snapshot(root: Path, destination: Path, commit: str) -> None:
    """Copy Git objects; reject links/submodules and ignore export-ignore filters."""
    entries = git(root, "ls-tree", "-rz", "--full-tree", commit).split(b"\0")
    total = 0
    for entry in entries:
        if not entry:
            continue
        header, raw_name = entry.split(b"\t", 1)
        mode, kind, object_id = header.decode().split()
        name = PurePosixPath(raw_name.decode("utf-8"))
        if (
            mode not in {"100644", "100755"}
            or kind != "blob"
            or name.is_absolute()
            or ".." in name.parts
        ):
            raise ValueError("source snapshot forbids links, submodules, and unsafe paths")
        data = git(root, "cat-file", "blob", object_id)
        total += len(data)
        if total > 128 * 1024 * 1024:
            raise ValueError("source snapshot exceeds size bound")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if source_commit(root) != commit:
        raise ValueError("source changed during snapshot")


def _public_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("distribution URL required")
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("only credential-free HTTPS distribution URLs allowed")
    return value


def locked_packages(lock: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return _locked_packages(read_regular(lock))


def _locked_packages(raw: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    document = tomllib.loads(raw.decode())
    packages = document.get("package")
    if document.get("version") != 1 or not isinstance(packages, list) or len(packages) > 4096:
        raise ValueError("supported bounded uv lock required")
    project: dict[str, Any] | None = None
    dependencies: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError("locked package must be an object")
        name, version = package.get("name"), package.get("version")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", name):
            raise ValueError("invalid locked package name")
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.!+_-]+", version):
            raise ValueError("exact locked package version required")
        identity = (name, version)
        if identity in identities:
            raise ValueError("duplicate locked package identity")
        identities.add(identity)
        source = package.get("source")
        if source == {"editable": "."} and name == "af-agent-framework":
            if project is not None:
                raise ValueError("one project required")
            project = package
            continue
        if not isinstance(source, dict) or set(source) != {"registry"}:
            raise ValueError("unreviewed local/VCS dependency forbidden")
        _public_url(source["registry"])
        wheels = package.get("wheels", [])
        if not isinstance(wheels, list) or len(wheels) > 1024:
            raise ValueError("bounded locked wheel list required")
        artifacts = list(wheels)
        if "sdist" in package:
            artifacts.append(package["sdist"])
        if not artifacts:
            raise ValueError("locked dependency has no hashed distribution")
        for artifact in artifacts:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("hash"), str):
                raise ValueError("hashed distribution object required")
            _public_url(artifact.get("url"))
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("hash", "")):
                raise ValueError("exact SHA-256 distribution hash required")
        dependencies.append(package)
    if project is None:
        raise ValueError("AF project identity missing")
    return project, sorted(dependencies, key=lambda item: (item["name"], item["version"]))


def _artifacts(package: dict[str, Any]) -> list[dict[str, Any]]:
    return [*package.get("wheels", []), *([package["sdist"]] if "sdist" in package else [])]


def audit_requirements(lock: Path) -> str:
    _, packages = locked_packages(lock)
    return "".join(
        f"{p['name']}=={p['version']} "
        + " ".join(f"--hash={digest}" for digest in sorted({a["hash"] for a in _artifacts(p)}))
        + "\n"
        for p in packages
    )


def build_sbom(lock: Path) -> dict[str, Any]:
    raw = read_regular(lock)
    project, packages = _locked_packages(raw)
    components = [
        {
            "type": "library",
            "bom-ref": f"pkg:pypi/{p['name']}@{quote(p['version'], safe='')}",
            "name": p["name"],
            "version": p["version"],
            "purl": f"pkg:pypi/{p['name']}@{quote(p['version'], safe='')}",
            "externalReferences": [
                {
                    "type": "distribution",
                    "url": a["url"],
                    "hashes": [{"alg": "SHA-256", "content": a["hash"][7:]}],
                }
                for a in sorted(_artifacts(p), key=lambda a: a["url"])
            ],
        }
        for p in packages
    ]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": project["name"],
                "version": project["version"],
            },
            "lifecycles": [{"phase": "pre-build"}],
            "properties": [
                {"name": "af:uv-lock-sha256", "value": hashlib.sha256(raw).hexdigest()},
                {
                    "name": "af:scope",
                    "value": "all lock resolutions, extras and development packages",
                },
                {"name": "af:certifies-production", "value": "false"},
            ],
        },
        "components": components,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        bom = build_sbom(args.lock)
        if args.execute:
            write_json(args.output, bom)
        print(
            json.dumps(
                {
                    "components": len(bom["components"]),
                    "written": args.execute,
                    "certifies_production": False,
                }
            )
        )
        return 0
    except (OSError, ValueError) as exc:
        print(f"inventory: {type(exc).__name__}; no inventory issued", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
