from __future__ import annotations

import asyncio

import pytest

from af.infrastructure.http_operations import SQLiteHttpOperations
from af.interfaces.http_operations import HttpOperationsMiddleware
from af.interfaces.rest_negotiation import RestNegotiationMiddleware


def _scope(*, headers=(), method="POST", path="/api/v1/tasks"):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": list(headers),
        "query_string": b"",
    }


def _run(middleware, scope, messages):
    sent = []
    remaining = iter(messages)

    async def receive():
        return next(remaining, {"type": "http.disconnect"})

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    return sent


def test_idempotency_preserves_disconnect_and_uncertain_oversized_response():
    store = SQLiteHttpOperations(":memory:")
    calls = []

    async def app(scope, receive, send):
        calls.append("run")
        request = await receive()
        assert request["body"] == b"ab"
        assert await receive() == {"type": "http.disconnect"}
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"x" * 1_048_577})

    middleware = HttpOperationsMiddleware(app, operations=store)
    scope = _scope(headers=[(b"idempotency-key", b"key")])
    assert _run(middleware, scope, [{"type": "http.disconnect"}]) == []
    messages = [
        {"type": "http.request", "body": b"a", "more_body": True},
        {"type": "http.request", "body": b"b"},
    ]
    first = _run(middleware, scope, messages)
    assert first[0]["status"] == 503
    again = _run(middleware, scope, messages)
    assert again[0]["status"] == 409
    assert calls == ["run"]
    too_large = _run(
        middleware,
        _scope(headers=[(b"idempotency-key", b"another")]),
        [{"type": "http.request", "body": b"x" * 1_048_577}],
    )
    assert too_large[0]["status"] == 413
    store.close()


def test_middleware_policy_and_non_http_boundaries():
    store = SQLiteHttpOperations(":memory:")
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["type"])

    with pytest.raises(ValueError):
        HttpOperationsMiddleware(app, operations=store, policy_version="")
    _run(HttpOperationsMiddleware(app, operations=store), {"type": "websocket"}, [])
    _run(RestNegotiationMiddleware(app), {"type": "websocket"}, [])
    assert calls == ["websocket", "websocket"]
    store.close()


def test_negotiation_body_stream_disconnect_limits_and_json_errors():
    calls = []

    async def app(scope, receive, send):
        calls.append(await receive())
        assert await receive() == {"type": "http.disconnect"}
        await send(
            {
                "type": "http.response.start",
                "status": 201,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"items":[{"nullable":null,"text":"ok","verified":true}]}',
                "more_body": False,
            }
        )

    middleware = RestNegotiationMiddleware(app)
    headers = [(b"content-type", b"application/xml"), (b"accept", b"application/xml")]
    assert _run(middleware, _scope(headers=headers), [{"type": "http.disconnect"}]) == []
    raw = [
        {"type": "http.request", "body": b"<task><prompt>", "more_body": True},
        {"type": "http.request", "body": b"hello</prompt></task>"},
    ]
    result = _run(middleware, _scope(headers=headers), raw)
    assert result[0]["status"] == 201
    assert b"<nullable />" in result[1]["body"]
    assert len(calls) == 1
    too_large = _run(
        middleware, _scope(headers=headers), [{"type": "http.request", "body": b"x" * 16385}]
    )
    assert too_large[0]["status"] == 400
    invalid = _run(
        middleware,
        _scope(headers=[(b"content-type", b"application/xml")]),
        [{"type": "http.request", "body": b"<bad/>"}],
    )
    assert invalid[0]["status"] == 400
    assert b"invalid_xml_request" in invalid[1]["body"]


def test_negotiation_rejects_ambiguous_headers_and_unsafe_response_fields():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"invalid field":"value"}'})

    middleware = RestNegotiationMiddleware(app)
    repeated = _run(
        middleware,
        _scope(headers=[(b"accept", b"application/xml"), (b"accept", b"application/json")]),
        [],
    )
    assert repeated[0]["status"] == 406
    with pytest.raises(ValueError, match="invalid response field"):
        _run(middleware, _scope(headers=[(b"accept", b"application/xml")]), [])

    async def huge(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x" * 1048577})

    with pytest.raises(ValueError, match="bounded"):
        _run(RestNegotiationMiddleware(huge), _scope(headers=[(b"accept", b"application/xml")]), [])
