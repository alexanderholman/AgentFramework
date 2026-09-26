import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from af.interfaces.http import create_http_app
from af.interfaces.soap_contract import (
    SOAP_FIELDS,
    SOAP_NAMESPACE,
    WSSE_NAMESPACE,
    SoapContractRegistry,
    SoapSecurityDenied,
)


def test_discoverable_deterministic_wsdl_xsd_match_installed_operations(application):
    registry = SoapContractRegistry()
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        wsdl = client.get("/soap?wsdl")
        assert wsdl.content == client.get("/soap/wsdl").content == registry.wsdl()
        assert wsdl.headers["content-type"].startswith("application/wsdl+xml")
        schema = client.get("/soap/schema.xsd")
        assert schema.content == registry.xsd()
        root = ET.fromstring(schema.content)
        xs = "{http://www.w3.org/2001/XMLSchema}"
        names = {node.attrib["name"] for node in root.findall(xs + "element")}
        assert names == set(SOAP_FIELDS) | {name + "Response" for name in SOAP_FIELDS}
        doc = ET.fromstring(wsdl.content)
        ns = "{http://schemas.xmlsoap.org/wsdl/}"
        assert {node.attrib["name"] for node in doc.findall(f"{ns}portType/{ns}operation")} == set(
            SOAP_FIELDS
        )
        assert b"127.0.0.1" not in wsdl.content


def _envelope(header=True):
    security = (
        '<s:Header><w:Security s:mustUnderstand="true">'
        "<nonce>unique</nonce></w:Security></s:Header>"
        if header
        else ""
    )
    return (
        f'<s:Envelope xmlns:s="{SOAP_NAMESPACE}" xmlns:a="urn:af:v1" xmlns:w="{WSSE_NAMESPACE}">'
        f"{security}<s:Body><a:Hello/></s:Body></s:Envelope>"
    ).encode()


def test_security_hook_receives_exact_envelope_before_dispatch_and_cannot_be_bypassed(application):
    class Verifier:
        def __init__(self):
            self.seen = []

        def verify(self, request):
            assert application.traces.list() == []
            assert request.operation == "Hello"
            assert request.context is not None
            self.seen.append(request.envelope)

    verifier = Verifier()
    with TestClient(
        create_http_app(application, soap_security=verifier), base_url="http://127.0.0.1"
    ) as client:
        missing = client.post(
            "/soap", content=_envelope(False), headers={"content-type": "application/soap+xml"}
        )
        assert missing.status_code == 400
        valid = client.post(
            "/soap", content=_envelope(), headers={"content-type": "application/soap+xml"}
        )
        assert valid.status_code == 200
        assert verifier.seen == [_envelope()]
        assert b"Hello, world!" in valid.content
        # HTTP replay must never skip an installed WS-Security verifier.
        denied = client.post(
            "/soap",
            content=_envelope(),
            headers={"content-type": "application/soap+xml", "idempotency-key": "key"},
        )
        assert denied.status_code == 400


@pytest.mark.parametrize(
    "error", [SoapSecurityDenied("private credential"), RuntimeError("private provider exception")]
)
def test_soap_security_denial_and_internal_errors_are_safe_versioned_faults(application, error):
    class Deny:
        def verify(self, request):
            raise error

    with TestClient(
        create_http_app(application, soap_security=Deny()), base_url="http://127.0.0.1"
    ) as client:
        result = client.post(
            "/soap", content=_envelope(), headers={"content-type": "application/soap+xml"}
        )
        assert result.status_code == (400 if isinstance(error, SoapSecurityDenied) else 500)
        assert b"private" not in result.content
        root = ET.fromstring(result.content)
        assert root.find(f".//{{{SOAP_NAMESPACE}}}Fault") is not None
        assert application.traces.list() == []


def test_xml_and_json_content_negotiation_share_resource_versions_and_errors(application):
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/v1/tasks",
            content="<task><prompt>hello</prompt></task>",
            headers={"content-type": "application/xml", "accept": "application/xml"},
        )
        assert created.status_code == 201
        path = created.headers["location"]
        task = client.get(path).json()
        xml = ET.fromstring(created.content)
        assert xml.findtext("id") == task["id"]
        assert xml.findtext("output") == task["output"]
        assert xml.findtext("verified") == str(task["verified"]).lower()
        assert created.headers["etag"].endswith('-xml"')
        changed = client.patch(
            path,
            json={"prompt": "how many rs in strawberry"},
            headers={"accept": "application/xml", "if-match": created.headers["etag"]},
        )
        assert changed.status_code == 200
        assert ET.fromstring(changed.content).findtext("output") == "3"
        stale = client.delete(
            path, headers={"accept": "application/xml", "if-match": created.headers["etag"]}
        )
        assert stale.status_code == 412
        assert ET.fromstring(stale.content).findtext("detail") == "task precondition failed"
        deleted = client.delete(
            path, headers={"accept": "application/xml", "if-match": changed.headers["etag"]}
        )
        assert deleted.status_code == 204
        assert deleted.content == b""
        missing_json = client.get(path)
        missing_xml = client.get(path, headers={"accept": "application/xml"})
        assert missing_json.status_code == missing_xml.status_code == 404
        assert (
            ET.fromstring(missing_xml.content).findtext("detail") == missing_json.json()["detail"]
        )


def test_negotiation_rejects_unsupported_and_unsafe_requests_before_mutation(application):
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        for accept in (
            "text/plain",
            "application/xml;q=0,application/json;q=0",
            "application/xml;q=9",
            "application/xml; charset=utf-8",
        ):
            assert (
                client.post(
                    "/api/v1/tasks", json={"prompt": "hello"}, headers={"accept": accept}
                ).status_code
                == 406
            )
        assert (
            client.post(
                "/api/v1/tasks", content="hello", headers={"content-type": "text/plain"}
            ).status_code
            == 415
        )
        for raw in (
            "<!DOCTYPE task><task><prompt>hello</prompt></task>",
            "<evil><prompt>hello</prompt></evil>",
            "<task><prompt>hello</prompt><shell>bad</shell></task>",
        ):
            result = client.post(
                "/api/v1/tasks",
                content=raw,
                headers={"content-type": "application/xml", "accept": "application/xml"},
            )
            assert result.status_code == 400
            assert ET.fromstring(result.content).findtext("detail") == "invalid_xml_request"
        assert application.tasks.list() == []
        listing = client.get(
            "/api/v1/tasks", headers={"accept": "application/*;q=0.5,application/xml;q=0.8"}
        )
        assert listing.headers["content-type"].startswith("application/xml")
