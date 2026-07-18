from __future__ import annotations

import json

import anyio
import pytest
from fastapi.responses import PlainTextResponse
from starlette.types import Message

from af.framework.errors import InvalidProviderConfiguration
from af.interfaces.http import _BoundedBodyMiddleware, _trusted_hosts_for
from af.interfaces.http_body import HTTP_MAX_BODY_BYTES
from af.interfaces.http_trust import format_origin, parse_origin_value


def test_trusted_hosts_for_loopback_and_non_loopback_hosts() -> None:
    assert _trusted_hosts_for("localhost") == ["127.0.0.1", "::1", "localhost"]
    assert _trusted_hosts_for("127.0.0.1") == ["127.0.0.1", "::1", "localhost"]
    assert _trusted_hosts_for("::1") == ["127.0.0.1", "::1", "localhost"]
    assert _trusted_hosts_for("[::1]") == ["127.0.0.1", "::1", "localhost"]
    assert _trusted_hosts_for("example.com", ("example.com",)) == ["example.com"]


def test_trusted_hosts_for_requires_explicit_hosts_for_non_loopback_binds() -> None:
    with pytest.raises(InvalidProviderConfiguration):
        _trusted_hosts_for("0.0.0.0")

    assert _trusted_hosts_for("0.0.0.0", ("af.internal", "192.168.1.20")) == [
        "192.168.1.20",
        "af.internal",
    ]


def test_trusted_hosts_for_accepts_ipv6_and_rejects_invalid_entries() -> None:
    assert _trusted_hosts_for("::1") == ["127.0.0.1", "::1", "localhost"]
    assert _trusted_hosts_for("0.0.0.0", ("[::1]", "af.internal:8000")) == [
        "[::1]",
        "af.internal:8000",
    ]

    for host in ("*", "evil.invalid/path", "user@evil.invalid"):
        with pytest.raises(InvalidProviderConfiguration):
            _trusted_hosts_for("0.0.0.0", (host,))


def test_parse_origin_value_rejects_ambiguous_forms_and_canonicalizes_default_ports() -> None:
    for origin in (
        " http://af.internal",
        "http://af.internal:",
        "http://af.internal:/",
        "http://af.internal?",
        "http://af.internal#",
        "http://af.internal/\n",
    ):
        with pytest.raises(InvalidProviderConfiguration):
            parse_origin_value(origin, error_message="origin is invalid")

    assert (
        format_origin(parse_origin_value("http://af.internal", error_message="origin is invalid"))
        == "http://af.internal"
    )
    assert (
        format_origin(
            parse_origin_value("http://af.internal:80", error_message="origin is invalid")
        )
        == "http://af.internal"
    )
    assert (
        format_origin(
            parse_origin_value("https://af.internal:443", error_message="origin is invalid")
        )
        == "https://af.internal"
    )
    assert (
        format_origin(
            parse_origin_value("https://af.internal:8443", error_message="origin is invalid")
        )
        == "https://af.internal:8443"
    )


def test_bounded_body_middleware_enforces_content_length_and_completion() -> None:
    async def scenario() -> None:
        body = b"a=b"
        exact_limit = b"x" * HTTP_MAX_BODY_BYTES
        oversized = b"x" * (HTTP_MAX_BODY_BYTES + 1)

        async def exercise(
            *,
            headers: list[tuple[bytes, bytes]] | None = None,
            messages: list[Message],
        ) -> tuple[bool, int, bytes]:
            called = False
            sent: list[Message] = []

            async def app(scope, receive, send):  # type: ignore[no-untyped-def]
                nonlocal called
                called = True
                replayed = b""
                while True:
                    message = await receive()
                    replayed += message.get("body", b"")
                    if not message.get("more_body", False):
                        break
                response = PlainTextResponse(replayed.decode("latin-1"), status_code=200)
                await response(scope, receive, send)

            queue = list(messages)

            async def receive() -> Message:
                if queue:
                    return queue.pop(0)
                return {"type": "http.disconnect"}

            async def send(message: Message) -> None:
                sent.append(message)

            middleware = _BoundedBodyMiddleware(app)
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/v1/tasks",
                "raw_path": b"/api/v1/tasks",
                "query_string": b"",
                "headers": headers or [],
            }

            await middleware(scope, receive, send)

            start = next(message for message in sent if message["type"] == "http.response.start")
            body_message = next(
                message for message in sent if message["type"] == "http.response.body"
            )
            return called, start["status"], body_message.get("body", b"")

        called, status_code, response_body = await exercise(
            messages=[
                {"type": "http.request", "body": body[:1], "more_body": True},
                {"type": "http.request", "body": body[1:], "more_body": False},
            ]
        )
        assert called is True
        assert status_code == 200
        assert response_body == body

        for headers, messages in (
            (
                [(b"content-length", b"1"), (b"content-length", b"1")],
                [{"type": "http.request", "body": b"x", "more_body": False}],
            ),
            (
                [(b"content-length", b"3")],
                [{"type": "http.request", "body": b"xy", "more_body": False}],
            ),
            (
                [(b"content-length", b"1")],
                [{"type": "http.request", "body": b"xy", "more_body": False}],
            ),
            (
                [(b"content-length", b"2")],
                [
                    {"type": "http.request", "body": b"x", "more_body": True},
                    {"type": "http.disconnect"},
                ],
            ),
        ):
            called, status_code, response_body = await exercise(headers=headers, messages=messages)
            assert called is False
            assert status_code == 400
            assert json.loads(response_body) in (
                {"detail": "invalid Content-Length header"},
                {"detail": "invalid request body"},
            )

        called, status_code, response_body = await exercise(
            headers=[(b"content-length", str(HTTP_MAX_BODY_BYTES).encode("ascii"))],
            messages=[{"type": "http.request", "body": exact_limit, "more_body": False}],
        )
        assert called is True
        assert status_code == 200
        assert response_body == exact_limit

        called, status_code, response_body = await exercise(
            messages=[{"type": "http.request", "body": oversized, "more_body": False}]
        )
        assert called is False
        assert status_code == 413
        assert json.loads(response_body) == {
            "detail": f"request body must be at most {HTTP_MAX_BODY_BYTES} bytes"
        }

    anyio.run(scenario)
