"""Limit-independent text encodings for arbitrary-precision Ideon numbers."""

from __future__ import annotations

_DECIMAL_CHUNK_BASE = 1_000_000_000
_DECIMAL_CHUNK_WIDTH = 9


def integer_to_decimal(value: int) -> str:
    """Return canonical decimal text without Python's integer string-size limit."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("value must be an integer")
    if value < 0:
        return "-" + natural_to_decimal(-value)
    return natural_to_decimal(value)


def natural_to_decimal(value: int) -> str:
    """Return decimal text for a natural number of any supported bit length."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("value must be a natural number")
    if value == 0:
        return "0"

    chunks: list[int] = []
    remainder = value
    while remainder:
        remainder, chunk = divmod(remainder, _DECIMAL_CHUNK_BASE)
        chunks.append(chunk)

    head = f"{chunks.pop()}"
    return head + "".join(f"{chunk:0{_DECIMAL_CHUNK_WIDTH}d}" for chunk in reversed(chunks))


def natural_to_hex(value: int) -> str:
    """Return lowercase hexadecimal text for compact internal key encoding."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("value must be a natural number")
    return format(value, "x")
