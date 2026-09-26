"""Bounded unambiguous JSON files at operator-controlled transport boundaries."""

from __future__ import annotations

import json
from pathlib import Path


def read_json(path: Path, *, max_bytes: int = 128_000) -> object:
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("JSON input exceeds limit")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError("nonfinite JSON number")

    try:
        result: object = json.loads(data, object_pairs_hook=unique, parse_constant=reject_constant)
    except RecursionError:
        raise ValueError("JSON nesting exceeds limit") from None
    return result
