"""JSON/XML negotiation for the canonical versioned Task REST resource routes."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from af.interfaces.xml_api import _fields, _parse


def _representation(scope: Scope) -> str:
    values = [v.decode("latin-1") for k, v in scope.get("headers", []) if k.lower() == b"accept"]
    if not values:
        return "json"
    if len(values) != 1 or len(values[0]) > 4096:
        raise ValueError("unsupported Accept header")
    candidates: dict[str, tuple[int, float]] = {}
    for entry in values[0].split(","):
        parts = [part.strip() for part in entry.split(";")]
        media = parts[0].lower()
        quality = 1.0
        if len(parts) > 2 or (
            len(parts) == 2
            and re.fullmatch(r"q=(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)", parts[1]) is None
        ):
            raise ValueError("unsupported Accept header")
        if len(parts) == 2:
            quality = float(parts[1][2:])
        for name, actual in (("json", "application/json"), ("xml", "application/xml")):
            specificity = (
                2
                if media == actual
                else 1
                if media == "application/*"
                else 0
                if media == "*/*"
                else -1
            )
            if specificity >= 0 and (name not in candidates or specificity > candidates[name][0]):
                candidates[name] = (specificity, quality)
    available = [
        (quality, specificity, name == "json", name)
        for name, (specificity, quality) in candidates.items()
        if quality > 0
    ]
    if not available:
        raise ValueError("unsupported Accept header")
    return max(available)[3]


def _populate(parent: ET.Element, value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            # Only serializer-owned JSON response keys become XML element names.
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", str(key)) is None:
                raise ValueError("invalid response field")
            _populate(ET.SubElement(parent, str(key)), child)
    elif isinstance(value, list):
        for child in value:
            _populate(ET.SubElement(parent, "item"), child)
    elif isinstance(value, bool):
        parent.text = "true" if value else "false"
    elif value is not None:
        parent.text = str(value)


class RestNegotiationMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path == "/api/v1/tasks" or path.startswith("/api/v1/tasks/")
        ):
            await self._app(scope, receive, send)
            return
        try:
            representation = _representation(scope)
        except ValueError:
            await JSONResponse({"detail": "not_acceptable"}, status_code=406)(scope, receive, send)
            return
        scope.setdefault("state", {})["af_representation"] = representation
        types = [v for k, v in scope.get("headers", []) if k.lower() == b"content-type"]
        source = receive
        if scope["method"] in {"POST", "PUT", "PATCH"} and types:
            media = types[0].decode("latin-1").split(";", 1)[0].strip()
            if len(types) != 1 or media not in {"application/json", "application/xml"}:
                await JSONResponse({"detail": "unsupported_media_type"}, status_code=415)(
                    scope, receive, send
                )
                return
            if media == "application/xml":
                raw = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    raw.extend(message.get("body", b""))
                    if len(raw) > 16_384:
                        break
                    if not message.get("more_body", False):
                        break
                try:
                    root = _parse(bytes(raw))
                    if root.tag != "task":
                        raise ValueError("invalid task document")
                    payload = json.dumps(_fields(root, ("prompt",))).encode()
                except (ValueError, UnicodeError, ET.ParseError):
                    response: Response = JSONResponse(
                        {"detail": "invalid_xml_request"}, status_code=400
                    )
                    if representation == "xml":
                        response = Response(
                            b"<error><detail>invalid_xml_request</detail></error>",
                            status_code=400,
                            media_type="application/xml",
                        )
                    await response(scope, receive, send)
                    return
                scope = dict(scope)
                scope["headers"] = [
                    (k, v)
                    for k, v in scope["headers"]
                    if k.lower() not in {b"content-type", b"content-length"}
                ]
                scope["headers"].extend(
                    [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode()),
                    ]
                )
                consumed = False

                async def converted_receive() -> Message:
                    nonlocal consumed
                    if consumed:
                        return await source()
                    consumed = True
                    return {"type": "http.request", "body": payload, "more_body": False}

                receive = converted_receive
        if representation == "json":

            async def send_json(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(scope=message)
                    existing = headers.get("Vary", "")
                    headers["Vary"] = (existing + ", " if existing else "") + "Accept"
                await send(message)

            await self._app(scope, receive, send_json)
            return
        start: Message = {"type": "http.response.start", "status": 500, "headers": []}
        body = bytearray()

        async def capture(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body":
                body.extend(message.get("body", b""))
                if len(body) > 1_048_576:
                    raise ValueError("XML response exceeds bounded profile")

        await self._app(scope, receive, capture)
        if start["status"] == 204:
            await Response(status_code=204)(scope, receive, send)
            return
        payload_out = json.loads(body)
        root = ET.Element(
            "error"
            if start["status"] >= 400
            else "tasks"
            if isinstance(payload_out, dict) and "items" in payload_out
            else "task"
        )
        _populate(root, payload_out)
        response = Response(
            ET.tostring(root, encoding="utf-8"),
            status_code=start["status"],
            media_type="application/xml",
        )
        response.raw_headers.extend(
            (k, v)
            for k, v in start.get("headers", [])
            if k.lower() not in {b"content-type", b"content-length", b"vary"}
        )
        response.headers["Vary"] = "Accept"
        await response(scope, receive, send)
