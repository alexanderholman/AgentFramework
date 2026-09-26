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
from af.domain.tasks import TaskConflict
from af.framework.context import maybe_current_context
from af.framework.errors import AFError, TaskNotFound
from af.interfaces.http_operations import expected_version, task_etag
from af.interfaces.presenters import outcome_to_dict, task_to_dict
from af.interfaces.soap_contract import (
    SOAP_FIELDS,
    WSSE_NAMESPACE,
    SoapContractRegistry,
    SoapSecurityDenied,
    SoapSecurityRequest,
    SoapSecurityVerifier,
)

SOAP = "http://www.w3.org/2003/05/soap-envelope"
_FIELDS = SOAP_FIELDS


def _parse(raw: bytes) -> ET.Element:
    if len(raw) > 16_384 or b"\x00" in raw:
        raise ValueError("XML request is outside the bounded profile")
    text = raw.decode("utf-8", errors="strict")
    if "<!" in text or "<?" in text.lstrip().removeprefix('<?xml version="1.0"?>'):
        raise ValueError("XML declarations and processing instructions are unsupported")
    return ET.fromstring(text)


def _fields(root: ET.Element, expected: tuple[str, ...]) -> dict[str, str]:
    if (
        root.attrib
        or len(root) != len(expected)
        or (root.text or "").strip()
        or (root.tail or "").strip()
    ):
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


def _soap(
    payload: Mapping[str, object], operation: str, *, fault: bool = False, receiver: bool = False
) -> Response:
    envelope = ET.Element("env:Envelope", {"xmlns:env": SOAP, "xmlns:af": "urn:af:v1"})
    body = ET.SubElement(envelope, "env:Body")
    if fault:
        error = ET.SubElement(body, "env:Fault")
        ET.SubElement(ET.SubElement(error, "env:Code"), "env:Value").text = (
            "env:Receiver" if receiver else "env:Sender"
        )
        ET.SubElement(
            ET.SubElement(error, "env:Reason"), "env:Text", {"xml:lang": "en"}
        ).text = str(payload["detail"])
    else:
        _populate(ET.SubElement(body, f"af:{operation}Response"), payload)
    return Response(
        ET.tostring(envelope, encoding="utf-8"),
        status_code=(500 if receiver else 400) if fault else 200,
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


def create_xml_router(
    controller: TaskController, *, security: SoapSecurityVerifier | None = None
) -> APIRouter:
    router = APIRouter(tags=["xml"])

    registry = SoapContractRegistry()

    @router.get("/soap/wsdl")
    @router.get("/soap")
    def wsdl() -> Response:
        return Response(registry.wsdl(), media_type="application/wsdl+xml")

    @router.get("/soap/schema.xsd")
    def xsd() -> Response:
        return Response(registry.xsd(), media_type="application/xml")

    @router.post("/soap")
    async def soap(request: Request) -> Response:
        try:
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip()
                != "application/soap+xml"
            ):
                raise ValueError("SOAP 1.2 requires application/soap+xml")
            raw = await request.body()
            envelope = _parse(raw)
            if (
                envelope.tag != f"{{{SOAP}}}Envelope"
                or envelope.attrib
                or (envelope.text or "").strip()
                or (envelope.tail or "").strip()
            ):
                raise ValueError("expected one SOAP 1.2 envelope and body")
            if security is None:
                if len(envelope) != 1:
                    raise ValueError("SOAP headers require a configured verifier")
            else:
                if len(envelope) != 2:
                    raise SoapSecurityDenied()
                header = envelope[0]
                if (
                    header.tag != f"{{{SOAP}}}Header"
                    or header.attrib
                    or len(header) != 1
                    or header[0].tag != f"{{{WSSE_NAMESPACE}}}Security"
                    or (header.text or "").strip()
                    or (header.tail or "").strip()
                ):
                    raise SoapSecurityDenied()
            body = envelope[-1]
            if (
                body.tag != f"{{{SOAP}}}Body"
                or body.attrib
                or len(body) != 1
                or (body.text or "").strip()
                or (body.tail or "").strip()
            ):
                raise ValueError("expected exactly one SOAP operation")
            node = body[0]
            if not node.tag.startswith("{urn:af:v1}"):
                raise ValueError("unsupported SOAP operation namespace")
            operation = node.tag.removeprefix("{urn:af:v1}")
            if operation not in _FIELDS:
                raise ValueError("unsupported SOAP operation")
            fields = _fields(node, _FIELDS[operation])
            if security is not None:
                await run_in_threadpool(
                    security.verify, SoapSecurityRequest(raw, operation, maybe_current_context())
                )
            return _soap(await run_in_threadpool(_invoke, controller, operation, fields), operation)
        except (ValueError, UnicodeError, ET.ParseError):
            return _soap({"detail": "invalid SOAP request"}, "", fault=True)
        except SoapSecurityDenied:
            return _soap({"detail": "SOAP security verification failed"}, "", fault=True)
        except TaskNotFound:
            return _soap({"detail": "task not found"}, "", fault=True)
        except AFError:
            return _soap({"detail": "operation rejected"}, "", fault=True)
        except Exception:
            return _soap({"detail": "SOAP operation failed"}, "", fault=True, receiver=True)

    @router.get("/api/v1/xml/tasks")
    def tasks() -> Response:
        return _xml(_invoke(controller, "ListTasks", {}), name="tasks")

    @router.get("/api/v1/xml/tasks/{task_id}")
    def get(task_id: str) -> Response:
        try:
            task = controller.get(task_id)
            response = _xml(task_to_dict(task))
            response.headers["ETag"] = task_etag(task, "xml")
            return response
        except TaskNotFound:
            return _xml({"detail": "task not found"}, name="error", status=404)

    @router.post("/api/v1/xml/tasks")
    async def create(request: Request) -> Response:
        try:
            fields = _fields(_parse(await request.body()), ("prompt",))
            task = await run_in_threadpool(controller.create, fields["prompt"])
            response = _xml(task_to_dict(task), status=201)
            response.headers["ETag"] = task_etag(task, "xml")
            response.headers["Location"] = f"/api/v1/xml/tasks/{task.id}"
            return response
        except (ValueError, UnicodeError, ET.ParseError, AFError):
            return _xml({"detail": "invalid task request"}, name="error", status=400)

    @router.put("/api/v1/xml/tasks/{task_id}")
    async def update(task_id: str, request: Request) -> Response:
        try:
            fields = _fields(_parse(await request.body()), ("prompt",))
            task = await run_in_threadpool(
                controller.update,
                task_id,
                fields["prompt"],
                expected_version=expected_version(request, "xml"),
            )
            response = _xml(task_to_dict(task))
            response.headers["ETag"] = task_etag(task, "xml")
            return response
        except TaskConflict:
            return _xml({"detail": "task precondition failed"}, name="error", status=412)
        except TaskNotFound:
            return _xml({"detail": "task not found"}, name="error", status=404)
        except (ValueError, UnicodeError, ET.ParseError, AFError):
            return _xml({"detail": "invalid task request"}, name="error", status=400)

    @router.delete("/api/v1/xml/tasks/{task_id}")
    def delete(task_id: str, request: Request) -> Response:
        try:
            controller.delete(task_id, expected_version=expected_version(request, "xml"))
            return _xml({"id": task_id, "deleted": True})
        except TaskConflict:
            return _xml({"detail": "task precondition failed"}, name="error", status=412)
        except TaskNotFound:
            return _xml({"detail": "task not found"}, name="error", status=404)

    return router
