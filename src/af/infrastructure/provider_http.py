"""Small bounded HTTP/JSON boundary shared by optional inference adapters."""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx

from af.framework.decisions import DecisionStatus
from af.framework.errors import AFError

MAX_RESPONSE_BYTES = 65_536


class ProviderFailure(AFError):
    def __init__(self, status: DecisionStatus) -> None:
        super().__init__(status.value)
        self.status = status


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("non-finite JSON number")


def decode_json(payload: bytes | str) -> object:
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ProviderFailure(DecisionStatus.MALFORMED)
    try:
        result: object = json.loads(
            payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ProviderFailure(DecisionStatus.MALFORMED) from None
    return result


def object_map(value: object) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ProviderFailure(DecisionStatus.MALFORMED)
    return value


def post_json(
    endpoint: str,
    payload: bytes,
    *,
    headers: dict[str, str],
    timeout: float,
    transport: httpx.BaseTransport | None,
    cancelled: Callable[[], bool] | None,
) -> object:
    """No redirects, environment proxies, retries, raw response logging or fallback."""
    if cancelled is not None and cancelled():
        raise ProviderFailure(DecisionStatus.CANCELLED)
    deadline = time.monotonic() + timeout
    failure: DecisionStatus | None = None
    body = bytearray()
    try:
        with (
            httpx.Client(
                transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
            ) as client,
            client.stream(
                "POST",
                endpoint,
                content=payload,
                headers={"Content-Type": "application/json", **headers},
            ) as response,
        ):
            if response.status_code != 200:
                raise ProviderFailure(DecisionStatus.UNAVAILABLE)
            for chunk in response.iter_bytes(chunk_size=4096):
                if cancelled is not None and cancelled():
                    raise ProviderFailure(DecisionStatus.CANCELLED)
                if time.monotonic() > deadline:
                    raise ProviderFailure(DecisionStatus.TIMEOUT)
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ProviderFailure(DecisionStatus.MALFORMED)
    except httpx.TimeoutException:
        failure = DecisionStatus.TIMEOUT
    except httpx.HTTPError:
        failure = DecisionStatus.UNAVAILABLE
    if failure is not None:
        raise ProviderFailure(failure)
    if cancelled is not None and cancelled():
        raise ProviderFailure(DecisionStatus.CANCELLED)
    return decode_json(bytes(body))
