"""Exclusive credential output with owned-file cleanup and authority compensation."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path


def write_private_json(
    output: Path,
    operation: Callable[[], dict[str, object]],
    compensate: Callable[[dict[str, object]], None],
) -> dict[str, object]:
    owned: os.stat_result | None = None
    result: dict[str, object] | None = None
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            owned = os.fstat(stream.fileno())
            result = operation()
            json.dump(result, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
            current = output.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
                raise OSError("private output destination changed")
        return result
    except BaseException:
        if result is not None:
            with suppress(Exception):
                compensate(result)
        if owned is not None:
            with suppress(OSError):
                current = output.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                    output.unlink()
        raise
