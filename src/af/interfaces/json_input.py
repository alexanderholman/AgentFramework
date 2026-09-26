"""Bounded unambiguous JSON files at operator-controlled transport boundaries."""

from __future__ import annotations

import json
from math import isfinite
from pathlib import Path


def _check_nesting(text: str) -> None:
    depth = 0
    quoted = escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > 64:
                raise ValueError("JSON nesting exceeds limit")
        elif character in "]}":
            depth -= 1


def read_json(path: Path, *, max_bytes: int = 128_000) -> object:
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("JSON byte limit must be a positive integer")
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("JSON input exceeds limit")
    text = data.decode("utf-8")
    _check_nesting(text)

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError("nonfinite JSON number")

    def finite_number(value: str) -> float:
        number = float(value)
        if not isfinite(number):
            raise ValueError("nonfinite JSON number")
        return number

    result: object = json.loads(
        text,
        object_pairs_hook=unique,
        parse_constant=reject_constant,
        parse_float=finite_number,
    )
    return result
