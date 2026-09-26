"""Malformed HTTP and cancellation never become model answers."""

import httpx
import pytest

from af.framework.decisions import DecisionStatus
from af.infrastructure.provider_http import ProviderFailure, decode_json, post_json


@pytest.mark.parametrize(
    "data", ['{"p":NaN}', '{"p":Infinity}', '{"p":0,"p":1}', "not JSON", b"\xff", " " * 65537]
)
def test_json_parser_rejects_ambiguous_and_unbounded_wire_values(data: str | bytes) -> None:
    with pytest.raises(ProviderFailure) as error:
        decode_json(data)
    assert error.value.status is DecisionStatus.MALFORMED


def test_cancel_before_http_never_opens_transport() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not send")

    with pytest.raises(ProviderFailure) as error:
        post_json(
            "http://127.0.0.1",
            b"{}",
            headers={},
            timeout=10,
            transport=httpx.MockTransport(forbidden),
            cancelled=lambda: True,
        )
    assert error.value.status is DecisionStatus.CANCELLED


def test_cancel_during_http_discards_response_and_closes_stream() -> None:
    flag = [False]

    def handler(request: httpx.Request) -> httpx.Response:
        flag[0] = True
        return httpx.Response(200, content=b"{}")

    with pytest.raises(ProviderFailure) as error:
        post_json(
            "http://127.0.0.1",
            b"{}",
            headers={},
            timeout=10,
            transport=httpx.MockTransport(handler),
            cancelled=lambda: flag[0],
        )
    assert error.value.status is DecisionStatus.CANCELLED


def test_network_error_has_no_sensitive_exception_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("password and prompt")

    with pytest.raises(ProviderFailure) as error:
        post_json(
            "http://127.0.0.1",
            b"{}",
            headers={},
            timeout=10,
            transport=httpx.MockTransport(handler),
            cancelled=None,
        )
    assert error.value.status is DecisionStatus.UNAVAILABLE
    assert error.value.__context__ is None
    assert "password" not in str(error.value)


def test_keyboard_interrupt_is_not_reclassified_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        post_json(
            "http://127.0.0.1",
            b"{}",
            headers={},
            timeout=10,
            transport=httpx.MockTransport(handler),
            cancelled=None,
        )
