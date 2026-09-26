"""Operator JSON files stay bounded, unambiguous, and finite before dispatch."""

from pathlib import Path

import pytest

from af.interfaces.json_input import read_json


def test_json_input_preserves_finite_values_at_exact_byte_limit(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    payload = b'{"n":1.25e2,"nested":{"n":2},"items":[null,true,"abc"]}'
    path.write_bytes(payload)
    assert read_json(path, max_bytes=len(payload)) == {
        "n": 125.0,
        "nested": {"n": 2},
        "items": [None, True, "abc"],
    }
    with pytest.raises(ValueError, match="exceeds limit"):
        read_json(path, max_bytes=len(payload) - 1)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"nested":{"key":1,"key":2}}',
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b'{"value":1e9999}',
        b'{"value":-1e9999}',
        b"[" * 2000 + b"0" + b"]" * 2000,
        b'{"value":}',
        b'"\xff"',
    ],
)
def test_json_input_rejects_ambiguous_nonfinite_or_malformed_values(
    tmp_path: Path, payload: bytes
) -> None:
    path = tmp_path / "input.json"
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        read_json(path)


@pytest.mark.parametrize("limit", [-2, -1, 0, True, 1.5])
def test_json_input_rejects_invalid_bounds_before_opening(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        read_json(tmp_path / "does-not-exist", max_bytes=limit)


def test_json_depth_limit_ignores_brackets_and_escaped_quotes_in_strings(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    payload = '["[\\"{",{"text":"\\\\]\\"}"}]'
    path.write_text(payload)
    assert read_json(path) == ['["{', {"text": '\\]"}'}]
    path.write_text("[" * 64 + "0" + "]" * 64)
    assert isinstance(read_json(path), list)
    path.write_text("[" * 65 + "0" + "]" * 65)
    with pytest.raises(ValueError, match="nesting"):
        read_json(path)
