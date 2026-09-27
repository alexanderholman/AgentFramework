"""Atomic local host keys kept separate from authenticated checkpoint rows."""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import URL


@dataclass(frozen=True, slots=True, repr=False)
class LocalAuthenticationKey:
    value: bytes


def workflow_key(url: URL) -> bytes:
    if url.get_backend_name() == "sqlite" and url.database in (None, "", ":memory:"):
        return secrets.token_bytes(32)
    path = (
        Path(str(url.database)).expanduser().resolve().with_suffix(".workflow-key")
        if url.get_backend_name() == "sqlite"
        else Path("storage/af-workflow.key").resolve()
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        descriptor, temporary = tempfile.mkstemp(prefix=".af-key-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(secrets.token_bytes(32))
                output.flush()
                os.fsync(output.fileno())
            with suppress(FileExistsError):
                os.link(temporary, path)
        finally:
            os.unlink(temporary)
    # Nonblocking open lets the descriptor check reject FIFOs without waiting
    # for another process; it has no effect on a regular key file.
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError("workflow key must be a private regular file")
        key = source.read(33)
        if len(key) != 32:
            raise ValueError("workflow key has invalid length")
        return key
