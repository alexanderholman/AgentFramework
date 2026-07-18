"""Shared HTTP body limits and bounded request-body helpers."""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable, Iterable, Mapping
from typing import Any

HTTP_MAX_BODY_BYTES = 131_072
BOUNDED_BODY_SCOPE_KEY = "af.bounded_body"


class InvalidContentLengthHeader(Exception):
    """Raised when an HTTP Content-Length header is malformed."""


class RequestBodyTooLarge(Exception):
    """Raised when an HTTP request body exceeds the configured byte limit."""


class InvalidRequestBody(Exception):
    """Raised when an HTTP request body is truncated or mismatched."""


def content_length_headers_from_scope(
    headers: Iterable[tuple[bytes, bytes]],
) -> tuple[str, ...]:
    values: list[str] = []
    for key, value in headers:
        if key.decode("latin-1").lower() == "content-length":
            values.append(value.decode("latin-1"))
    return tuple(values)


def validate_content_length_header(content_length: str | tuple[str, ...] | None) -> int | None:
    """Validate optional Content-Length header values against AF's HTTP body ceiling."""
    if content_length is None:
        return None
    values = (content_length,) if isinstance(content_length, str) else content_length
    if not values:
        return None
    if len(values) != 1:
        raise InvalidContentLengthHeader
    candidate = values[0]
    if not candidate.isascii() or not candidate.isdigit():
        raise InvalidContentLengthHeader
    parsed = int(candidate)
    if parsed > HTTP_MAX_BODY_BYTES:
        raise RequestBodyTooLarge
    return parsed


async def read_bounded_chunks(chunks: AsyncIterable[bytes]) -> bytes:
    """Read a chunk stream into memory while enforcing the shared byte ceiling."""
    size = 0
    collected: list[bytes] = []
    async for chunk in chunks:
        size += len(chunk)
        if size > HTTP_MAX_BODY_BYTES:
            raise RequestBodyTooLarge
        collected.append(chunk)
    return b"".join(collected)


async def read_bounded_receive(
    receive: Callable[[], Awaitable[Mapping[str, Any]]],
    *,
    expected_length: int | None = None,
) -> bytes:
    """Read the ASGI receive channel into memory while enforcing the shared byte ceiling."""

    async def chunks() -> AsyncIterable[bytes]:
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise InvalidRequestBody
            if message["type"] != "http.request":
                continue
            body = message.get("body", b"")
            if not isinstance(body, bytes):
                body = b""
            size += len(body)
            if size > HTTP_MAX_BODY_BYTES:
                raise RequestBodyTooLarge
            yield body
            if not bool(message.get("more_body", False)):
                if expected_length is not None and size != expected_length:
                    raise InvalidRequestBody
                break

    return await read_bounded_chunks(chunks())


def replay_receive(body: bytes) -> Callable[[], Awaitable[dict[str, object]]]:
    """Return an ASGI receive callable that replays a previously buffered body once."""
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive
