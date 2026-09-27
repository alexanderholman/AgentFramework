"""Deterministic document/literal SOAP 1.2 contract and explicit security extension."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Protocol

from af.framework.context import ExecutionContext

SOAP_NAMESPACE = "http://www.w3.org/2003/05/soap-envelope"
WSSE_NAMESPACE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
SOAP_FIELDS = {
    "Hello": (),
    "Ask": ("prompt",),
    "CreateTask": ("prompt",),
    "ListTasks": (),
    "GetTask": ("id",),
    "UpdateTask": ("id", "prompt"),
    "DeleteTask": ("id",),
}


class SoapSecurityDenied(Exception):
    """A configured verifier did not authenticate this exact SOAP envelope."""


@dataclass(frozen=True, slots=True)
class SoapSecurityRequest:
    envelope: bytes
    operation: str
    context: ExecutionContext | None


class SoapSecurityVerifier(Protocol):
    def verify(self, request: SoapSecurityRequest) -> None:
        """Verify signature/body coverage, freshness and nonce replay; raise on denial.

        Installation of this hook requires one WS-Security header for every SOAP
        operation. The hook cannot replace or widen the HTTP execution principal.
        AF does not implement XML Signature, encryption or UsernameToken itself.
        """
        ...


class SoapContractRegistry:
    """The closed installed v1 operation registry; no arbitrary remote schema import."""

    def xsd(self) -> bytes:
        root = ET.Element(
            "xs:schema",
            {
                "xmlns:xs": "http://www.w3.org/2001/XMLSchema",
                "xmlns:af": "urn:af:v1",
                "targetNamespace": "urn:af:v1",
                "elementFormDefault": "unqualified",
                "version": "1.0.0",
            },
        )
        task_fields = (
            "id",
            "prompt",
            "output",
            "intent",
            "route",
            "tier",
            "status",
            "verified",
            "reused",
            "trace_id",
            "created_at",
            "updated_at",
        )
        outcome_fields = (
            "prompt",
            "output",
            "intent",
            "route",
            "tier",
            "status",
            "verified",
            "reused",
            "trace_id",
        )
        for operation, fields in SOAP_FIELDS.items():
            self._element(root, operation, fields)
            if operation == "ListTasks":
                sequence = self._element(root, operation + "Response", ())
                items = ET.SubElement(sequence, "xs:element", {"name": "items"})
                collection = ET.SubElement(ET.SubElement(items, "xs:complexType"), "xs:sequence")
                item = ET.SubElement(
                    collection,
                    "xs:element",
                    {"name": "item", "minOccurs": "0", "maxOccurs": "unbounded"},
                )
                seq = ET.SubElement(ET.SubElement(item, "xs:complexType"), "xs:sequence")
                self._fields(seq, task_fields)
            else:
                result = (
                    ("id", "deleted")
                    if operation == "DeleteTask"
                    else outcome_fields
                    if operation in {"Hello", "Ask"}
                    else task_fields
                )
                self._element(root, operation + "Response", result)
        return bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))

    @staticmethod
    def _fields(sequence: ET.Element, fields: tuple[str, ...]) -> None:
        for field in fields:
            kind = (
                "boolean"
                if field in {"verified", "reused", "deleted"}
                else "dateTime"
                if field in {"created_at", "updated_at"}
                else "string"
            )
            ET.SubElement(sequence, "xs:element", {"name": field, "type": "xs:" + kind})

    def _element(self, root: ET.Element, name: str, fields: tuple[str, ...]) -> ET.Element:
        element = ET.SubElement(root, "xs:element", {"name": name})
        sequence = ET.SubElement(ET.SubElement(element, "xs:complexType"), "xs:sequence")
        self._fields(sequence, fields)
        return sequence

    def wsdl(self) -> bytes:
        root = ET.Element(
            "wsdl:definitions",
            {
                "xmlns:wsdl": "http://schemas.xmlsoap.org/wsdl/",
                "xmlns:soap12": "http://schemas.xmlsoap.org/wsdl/soap12/",
                "xmlns:xs": "http://www.w3.org/2001/XMLSchema",
                "xmlns:af": "urn:af:v1",
                "targetNamespace": "urn:af:v1",
                "name": "AgentFrameworkV1",
            },
        )
        schema = ET.SubElement(ET.SubElement(root, "wsdl:types"), "xs:schema")
        ET.SubElement(
            schema, "xs:import", {"namespace": "urn:af:v1", "schemaLocation": "/soap/schema.xsd"}
        )
        for operation in SOAP_FIELDS:
            for suffix in ("", "Response"):
                message = ET.SubElement(root, "wsdl:message", {"name": operation + suffix})
                ET.SubElement(
                    message,
                    "wsdl:part",
                    {"name": "parameters", "element": "af:" + operation + suffix},
                )
        port = ET.SubElement(root, "wsdl:portType", {"name": "TasksV1"})
        binding = ET.SubElement(
            root, "wsdl:binding", {"name": "TasksV1Soap12", "type": "af:TasksV1"}
        )
        ET.SubElement(
            binding,
            "soap12:binding",
            {"style": "document", "transport": "http://schemas.xmlsoap.org/soap/http"},
        )
        for operation in SOAP_FIELDS:
            op = ET.SubElement(port, "wsdl:operation", {"name": operation})
            bound = ET.SubElement(binding, "wsdl:operation", {"name": operation})
            ET.SubElement(bound, "soap12:operation", {"soapAction": "urn:af:v1/" + operation})
            for direction, suffix in (("input", ""), ("output", "Response")):
                ET.SubElement(op, "wsdl:" + direction, {"message": "af:" + operation + suffix})
                ET.SubElement(
                    ET.SubElement(bound, "wsdl:" + direction), "soap12:body", {"use": "literal"}
                )
        service = ET.SubElement(root, "wsdl:service", {"name": "AgentFrameworkV1"})
        port = ET.SubElement(
            service, "wsdl:port", {"name": "TasksV1", "binding": "af:TasksV1Soap12"}
        )
        ET.SubElement(port, "soap12:address", {"location": "/soap"})
        return bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
