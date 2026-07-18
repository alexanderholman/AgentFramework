"""Shared HTTP body limits and bounded request-body helpers."""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from typing import Any

HTTP_MAX_BODY_BYTES = 131_072
BOUNDED_BODY_SCOPE_KEY = "af.bounded_body"


class InvalidContentLengthHeader(Exception):
    """Raised when an HTTP Content-Length header is malformed."""


class RequestBodyTooLarge(Exception):
    """Raised when an HTTP request body exceeds the configured byte limit."""


def validate_content_length_header(content_length: str | None) -> None:
    """Validate an optional Content-Length header against AF's HTTP body ceiling."""
    if content_length is None:
        return
    try:
        parsed = int(content_length)
    except ValueError:
        raise InvalidContentLengthHeader from None
    if parsed < 0:
        raise InvalidContentLengthHeader
    if parsed > HTTP_MAX_BODY_BYTES:
        raise RequestBodyTooLarge


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
) -> bytes:
    """Read the ASGI receive channel into memory while enforcing the shared byte ceiling."""

    async def chunks() -> AsyncIterable[bytes]:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            body = message.get("body", b"")
            if not isinstance(body, bytes):
                body = b""
            yield body
            if not bool(message.get("more_body", False)):
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
