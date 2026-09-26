"""Bounded UTF-8 XML and SOAP 1.2 adapters over the shared Task controller.

The finite document/literal profile accepts one operation, scalar arguments,
and no DTDs, entity declarations, processing instructions, or header modules.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Mapping

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from af.application.tasks import TaskController
from af.framework.errors import AFError, TaskNotFound
from af.interfaces.presenters import outcome_to_dict, task_to_dict

SOAP = "http://www.w3.org/2003/05/soap-envelope"
_FIELDS = {
    "Hello": (),
    "Ask": ("prompt",),
    "CreateTask": ("prompt",),
    "ListTasks": (),
    "GetTask": ("id",),
    "UpdateTask": ("id", "prompt"),
    "DeleteTask": ("id",),
}


def _parse(raw: bytes) -> ET.Element:
    if len(raw) > 16_384 or b"\x00" in raw:
        raise ValueError("XML request is outside the bounded profile")
    text = raw.decode("utf-8", errors="strict")
    if "<!" in text or "<?" in text.lstrip().removeprefix('<?xml version="1.0"?>'):
        raise ValueError("XML declarations and processing instructions are unsupported")
    return ET.fromstring(text)


def _fields(root: ET.Element, expected: tuple[str, ...]) -> dict[str, str]:
    if root.attrib or len(root) != len(expected) or (root.text or "").strip():
        raise ValueError("invalid XML operation arguments")
    values: dict[str, str] = {}
    for child in root:
        name = child.tag.removeprefix("{urn:af:v1}")
        if (
            name not in expected
            or name in values
            or len(child)
            or child.attrib
            or (child.tail or "").strip()
        ):
            raise ValueError("invalid XML operation arguments")
        values[name] = child.text or ""
    if set(values) != set(expected):
        raise ValueError("missing XML operation arguments")
    return values


def _populate(parent: ET.Element, payload: Mapping[str, object]) -> None:
    for key, value in payload.items():
        child = ET.SubElement(parent, key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _populate(ET.SubElement(child, "item"), item)
        elif isinstance(value, bool):
            child.text = "true" if value else "false"
        elif value is not None:
            child.text = str(value)


def _xml(payload: Mapping[str, object], *, name: str = "task", status: int = 200) -> Response:
    root = ET.Element(name)
    _populate(root, payload)
    return Response(
        ET.tostring(root, encoding="utf-8"), status_code=status, media_type="application/xml"
    )


def _soap(payload: Mapping[str, object], operation: str, *, fault: bool = False) -> Response:
    envelope = ET.Element("env:Envelope", {"xmlns:env": SOAP, "xmlns:af": "urn:af:v1"})
    body = ET.SubElement(envelope, "env:Body")
    if fault:
        error = ET.SubElement(body, "env:Fault")
        ET.SubElement(ET.SubElement(error, "env:Code"), "env:Value").text = "env:Sender"
        ET.SubElement(
            ET.SubElement(error, "env:Reason"), "env:Text", {"xml:lang": "en"}
        ).text = str(payload["detail"])
    else:
        _populate(ET.SubElement(body, f"af:{operation}Response"), payload)
    return Response(
        ET.tostring(envelope, encoding="utf-8"),
        status_code=400 if fault else 200,
        media_type="application/soap+xml",
    )


def _invoke(
    controller: TaskController, operation: str, arguments: dict[str, str]
) -> dict[str, object]:
    match operation:
        case "Hello":
            return outcome_to_dict(controller.execute("hello"))
        case "Ask":
            return outcome_to_dict(controller.execute(arguments["prompt"]))
        case "CreateTask":
            return task_to_dict(controller.create(arguments["prompt"]))
        case "ListTasks":
            return {"items": [task_to_dict(item) for item in controller.list()]}
        case "GetTask":
            return task_to_dict(controller.get(arguments["id"]))
        case "UpdateTask":
            return task_to_dict(controller.update(arguments["id"], arguments["prompt"]))
        case "DeleteTask":
            controller.delete(arguments["id"])
            return {"id": arguments["id"], "deleted": True}
        case _:
            raise ValueError("unsupported SOAP operation")


def create_xml_router(controller: TaskController) -> APIRouter:
    router = APIRouter(tags=["xml"])

    @router.post("/soap")
    async def soap(request: Request) -> Response:
        try:
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip()
                != "application/soap+xml"
            ):
                raise ValueError("SOAP 1.2 requires application/soap+xml")
            envelope = _parse(await request.body())
            if envelope.tag != f"{{{SOAP}}}Envelope" or envelope.attrib or len(envelope) != 1:
                raise ValueError("expected one SOAP 1.2 envelope and body")
            body = envelope[0]
            if body.tag != f"{{{SOAP}}}Body" or body.attrib or len(body) != 1:
                raise ValueError("expected exactly one SOAP operation")
            node = body[0]
            if not node.tag.startswith("{urn:af:v1}"):
                raise ValueError("unsupported SOAP operation namespace")
            operation = node.tag.removeprefix("{urn:af:v1}")
            if operation not in _FIELDS:
                raise ValueError("unsupported SOAP operation")
            fields = _fields(node, _FIELDS[operation])
            return _soap(await run_in_threadpool(_invoke, controller, operation, fields), operation)
        except (ValueError, UnicodeError, ET.ParseError):
            return _soap({"detail": "invalid SOAP request"}, "", fault=True)
        except AFError as error:
            return _soap({"detail": str(error)}, "", fault=True)

    @router.get("/api/v1/xml/tasks")
    def tasks() -> Response:
        return _xml(_invoke(controller, "ListTasks", {}), name="tasks")

    @router.get("/api/v1/xml/tasks/{task_id}")
    def get(task_id: str) -> Response:
        try:
            return _xml(task_to_dict(controller.get(task_id)))
        except TaskNotFound:
            return _xml({"detail": "task not found"}, name="error", status=404)

    @router.post("/api/v1/xml/tasks")
    async def create(request: Request) -> Response:
        try:
            fields = _fields(_parse(await request.body()), ("prompt",))
            task = await run_in_threadpool(controller.create, fields["prompt"])
            return _xml(task_to_dict(task), status=201)
        except (ValueError, UnicodeError, ET.ParseError, AFError):
            return _xml({"detail": "invalid task request"}, name="error", status=400)

    @router.put("/api/v1/xml/tasks/{task_id}")
    async def update(task_id: str, request: Request) -> Response:
        try:
            fields = _fields(_parse(await request.body()), ("prompt",))
            task = await run_in_threadpool(controller.update, task_id, fields["prompt"])
            return _xml(task_to_dict(task))
        except TaskNotFound:
            return _xml({"detail": "task not found"}, name="error", status=404)
        except (ValueError, UnicodeError, ET.ParseError, AFError):
            return _xml({"detail": "invalid task request"}, name="error", status=400)

    @router.delete("/api/v1/xml/tasks/{task_id}")
    def delete(task_id: str) -> Response:
        try:
            controller.delete(task_id)
            return _xml({"id": task_id, "deleted": True})
        except TaskNotFound:
            return _xml({"detail": "task not found"}, name="error", status=404)

    return router
