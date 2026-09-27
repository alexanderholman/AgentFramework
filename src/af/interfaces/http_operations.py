"""HTTP admission/replay middleware and strict conditional request translation."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from hashlib import sha256

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from af.application.http_operations import ApiRejection, HttpOperations, StoredResponse
from af.domain.tasks import Task, task_version
from af.framework.context import maybe_current_context

_REPLAY_HEADERS = frozenset({b"content-type", b"content-language", b"location", b"etag", b"vary"})


def task_etag(task: Task, representation: str = "json") -> str:
    return f'"{task_version(task)}-{representation}"'


def expected_version(request: Request, representation: str | None = None) -> str | None:
    representation = representation or str(getattr(request.state, "af_representation", "json"))
    values = request.headers.getlist("if-match")
    if not values:
        return None
    if (
        len(values) != 1
        or re.fullmatch(r'"[0-9a-f]{64}-' + representation + r'"', values[0]) is None
    ):
        raise HTTPException(400, detail="if_match_requires_one_strong_resource_etag")
    return values[0][1:65]


class HttpOperationsMiddleware:
    """Re-authenticated replay, exact request binding, bounded response buffering.

    The authentication/context middleware MUST enclose this middleware. In-flight,
    server-error, interrupted or oversized responses remain uncertain in the ledger;
    they cannot be automatically reclaimed, even after a process restart.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        operations: HttpOperations,
        policy_version: str = "af-api-v1",
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(policy_version, str) or not 1 <= len(policy_version) <= 128:
            raise ValueError("API policy version must be bounded")
        self._app = app
        self._operations = operations
        self._policy_version = policy_version
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not (
            scope["path"].startswith("/api/") or scope["path"] in {"/soap", "/graphql"}
        ):
            await self._app(scope, receive, send)
            return
        context = maybe_current_context()
        principal = context.principal if context else None
        identity = {
            "tenant": context.tenant_id if context else None,
            "subject": principal.subject_id if principal else "anonymous-local",
            "type": principal.principal_type if principal else "anonymous",
        }
        scope_key = self._digest(identity)
        try:
            await run_in_threadpool(self._operations.admit, scope_key, now=self._clock())
            keys = [v for k, v in scope.get("headers", []) if k.lower() == b"idempotency-key"]
            if not keys:
                await self._app(scope, receive, send)
                return
            if not (
                scope["path"] in {"/api/v1/tasks", "/api/v1/xml/tasks"}
                or scope["path"].startswith(("/api/v1/tasks/", "/api/v1/xml/tasks/"))
            ):
                raise ApiRejection(400, "idempotency_unsupported_resource")
            if (
                scope["method"] not in {"POST", "PUT", "PATCH", "DELETE"}
                or len(keys) != 1
                or re.fullmatch(rb"[A-Za-z0-9._:-]{1,128}", keys[0]) is None
            ):
                raise ApiRejection(400, "invalid_idempotency_key")
            raw = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                raw.extend(message.get("body", b""))
                if len(raw) > 1_048_576:
                    raise ApiRejection(413, "request_too_large")
                if not message.get("more_body", False):
                    break
            fingerprint = self._digest(
                {
                    "method": scope["method"],
                    "path": scope["path"],
                    "query": scope.get("query_string", b"").hex(),
                    "body": sha256(raw).hexdigest(),
                    "headers": sorted(
                        (k.lower().hex(), v.hex())
                        for k, v in scope.get("headers", [])
                        if k.lower()
                        not in {
                            b"authorization",
                            b"idempotency-key",
                            b"connection",
                            b"host",
                            b"content-length",
                            b"user-agent",
                        }
                    ),
                    "policy_version": self._policy_version,
                    "authorization": asdict(principal) if principal else None,
                }
            )
            key = sha256(keys[0]).hexdigest()
            replay = await run_in_threadpool(
                self._operations.claim, scope_key, key, fingerprint, now=self._clock()
            )
            if replay is not None:
                response = Response(replay.body, status_code=replay.status)
                response.raw_headers.extend(replay.headers)
                response.headers["Idempotency-Replayed"] = "true"
                await response(scope, receive, send)
                return
            consumed = False

            async def replay_receive() -> Message:
                nonlocal consumed
                if consumed:
                    return await receive()
                consumed = True
                return {"type": "http.request", "body": bytes(raw), "more_body": False}

            status = 500
            headers: tuple[tuple[bytes, bytes], ...] = ()
            body = bytearray()

            async def capture(message: Message) -> None:
                nonlocal status, headers
                if message["type"] == "http.response.start":
                    status = message["status"]
                    headers = tuple(
                        (k.lower(), v)
                        for k, v in message.get("headers", [])
                        if k.lower() in _REPLAY_HEADERS
                    )
                elif message["type"] == "http.response.body":
                    body.extend(message.get("body", b""))
                    if len(body) > 1_048_576:
                        raise ApiRejection(503, "idempotency_response_too_large_uncertain")

            await self._app(scope, replay_receive, capture)
            if 200 <= status < 500:
                stored = StoredResponse(status, headers, bytes(body))
                await run_in_threadpool(
                    self._operations.complete,
                    scope_key,
                    key,
                    fingerprint,
                    stored,
                    now=self._clock(),
                )
            response = Response(bytes(body), status_code=status)
            response.raw_headers.extend(headers)
            await response(scope, receive, send)
        except ApiRejection as error:
            headers_out = (
                {} if error.retry_after is None else {"Retry-After": str(error.retry_after)}
            )
            await JSONResponse(
                {"detail": error.reason}, status_code=error.status, headers=headers_out
            )(scope, receive, send)

    @staticmethod
    def _digest(value: object) -> str:
        return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
