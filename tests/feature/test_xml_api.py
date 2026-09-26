from __future__ import annotations

import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from af.bootstrap import AFApplication
from af.interfaces.http import create_http_app

SOAP = "http://www.w3.org/2003/05/soap-envelope"


def test_soap_task_crud_shares_rest_state(application: AFApplication) -> None:
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        response = client.post(
            "/soap",
            content=f'<s:Envelope xmlns:s="{SOAP}" xmlns:a="urn:af:v1">'
            "<s:Body><a:CreateTask><prompt>how many rs in strawberry</prompt>"
            "</a:CreateTask></s:Body></s:Envelope>",
            headers={"content-type": "application/soap+xml"},
        )
        assert response.status_code == 200, response.text
        root = ET.fromstring(response.content)
        task_id = root.findtext(".//id")
        assert task_id
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        assert task["output"] == "3"
        xml = client.get(f"/api/v1/xml/tasks/{task_id}")
        assert xml.status_code == 200
        assert ET.fromstring(xml.content).findtext("output") == task["output"]


def test_xml_external_entities_and_extra_arguments_are_rejected_before_execution(
    application: AFApplication,
) -> None:
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        before = application.traces.list()
        for content in (
            '<!DOCTYPE foo [<!ENTITY x SYSTEM "file:///etc/passwd">]><foo>&x;</foo>',
            f'<s:Envelope xmlns:s="{SOAP}" xmlns:a="urn:af:v1">'
            "<s:Body><a:Ask><prompt>hello</prompt><shell>bad</shell></a:Ask></s:Body></s:Envelope>",
        ):
            response = client.post(
                "/soap", content=content, headers={"content-type": "application/soap+xml"}
            )
            assert response.status_code == 400
            assert "Fault" in response.text
        assert application.traces.list() == before


def _soap_call(client, operation, arguments=None):
    envelope = ET.Element(f"{{{SOAP}}}Envelope")
    body = ET.SubElement(envelope, f"{{{SOAP}}}Body")
    node = ET.SubElement(body, f"{{urn:af:v1}}{operation}")
    for name, value in (arguments or {}).items():
        ET.SubElement(node, name).text = value
    return client.post(
        "/soap",
        content=ET.tostring(envelope),
        headers={"content-type": "application/soap+xml; charset=utf-8"},
    )


def test_all_soap_operations_preserve_the_shared_task_crud_contract(application):
    import json

    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        hello = _soap_call(client, "Hello")
        assert hello.status_code == 200
        assert ET.fromstring(hello.content).findtext(".//output") == "Hello, world!"
        science = _soap_call(client, "Ask", {"prompt": "math divide a=1 b=3"})
        assert science.status_code == 200
        result = ET.fromstring(science.content)
        assert result.findtext(".//verified") == "true"
        assert json.loads(result.findtext(".//output"))["quantity"]["exact"] == "1/3"
        created = _soap_call(client, "CreateTask", {"prompt": "hello"})
        assert created.status_code == 200
        task_id = ET.fromstring(created.content).findtext(".//id")
        assert task_id
        listing = _soap_call(client, "ListTasks")
        assert listing.status_code == 200
        assert ET.fromstring(listing.content).findtext(".//items/item/id") == task_id
        fetched = _soap_call(client, "GetTask", {"id": task_id})
        assert ET.fromstring(fetched.content).findtext(".//route") == "hello"
        updated = _soap_call(
            client, "UpdateTask", {"id": task_id, "prompt": "how many bs in blueberry"}
        )
        assert updated.status_code == 200
        assert ET.fromstring(updated.content).findtext(".//output") == "2"
        assert client.get(f"/api/v1/tasks/{task_id}").json()["output"] == "2"
        deleted = _soap_call(client, "DeleteTask", {"id": task_id})
        assert deleted.status_code == 200
        assert ET.fromstring(deleted.content).findtext(".//deleted") == "true"
        for operation, fields in [
            ("GetTask", {"id": task_id}),
            ("DeleteTask", {"id": task_id}),
            ("UpdateTask", {"id": task_id, "prompt": "hello"}),
        ]:
            missing = _soap_call(client, operation, fields)
            assert missing.status_code == 400
            assert ET.fromstring(missing.content).find(f".//{{{SOAP}}}Fault") is not None
        unsupported = _soap_call(client, "Ask", {"prompt": "unsupported science request"})
        assert unsupported.status_code == 400


def test_xml_rest_crud_preserves_json_state_and_not_found_semantics(application):
    headers = {"content-type": "application/xml"}
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/v1/xml/tasks", content="<task><prompt>hello</prompt></task>", headers=headers
        )
        assert created.status_code == 201
        root = ET.fromstring(created.content)
        task_id = root.findtext("id")
        assert task_id and root.findtext("verified") == "true"
        listing = client.get("/api/v1/xml/tasks")
        assert listing.status_code == 200
        assert ET.fromstring(listing.content).findtext("items/item/id") == task_id
        changed = client.put(
            f"/api/v1/xml/tasks/{task_id}",
            content="<task><prompt>how many rs in strawberry</prompt></task>",
            headers=headers,
        )
        assert changed.status_code == 200
        assert ET.fromstring(changed.content).findtext("output") == "3"
        assert client.get(f"/api/v1/tasks/{task_id}").json()["route"] == "character-count"
        invalid = client.put(f"/api/v1/xml/tasks/{task_id}", content=b"\xff", headers=headers)
        assert invalid.status_code == 400
        assert client.get(f"/api/v1/tasks/{task_id}").json()["output"] == "3"
        deleted = client.delete(f"/api/v1/xml/tasks/{task_id}")
        assert deleted.status_code == 200
        assert ET.fromstring(deleted.content).findtext("deleted") == "true"
        assert client.get(f"/api/v1/xml/tasks/{task_id}").status_code == 404
        assert client.delete(f"/api/v1/xml/tasks/{task_id}").status_code == 404
        missing_update = client.put(
            f"/api/v1/xml/tasks/{task_id}",
            content="<task><prompt>hello</prompt></task>",
            headers=headers,
        )
        assert missing_update.status_code == 404
        assert ET.fromstring(missing_update.content).findtext("detail") == "task not found"


def test_soap_envelope_namespace_and_scalar_argument_boundaries_do_not_execute(application):
    prefixes = f'xmlns:s="{SOAP}" xmlns:a="urn:af:v1"'
    bodies = [
        "<s:Header/><s:Body><a:Hello/></s:Body>",
        '<s:Body extra="x"><a:Hello/></s:Body>',
        "<s:Body><a:Hello/><a:Hello/></s:Body>",
        "<s:Body><Hello/></s:Body>",
        "<s:Body><a:Unknown/></s:Body>",
        '<s:Body><a:Hello extra="x"/></s:Body>',
        "<s:Body><a:Hello>unexpected</a:Hello></s:Body>",
        "<s:Body><a:Ask/></s:Body>",
        "<s:Body><a:Ask><prompt><nested/></prompt></a:Ask></s:Body>",
        '<s:Body><a:Ask><prompt bad="x">hello</prompt></a:Ask></s:Body>',
        "<s:Body><a:Ask><prompt>hello</prompt>trailing</a:Ask></s:Body>",
        "<s:Body><a:UpdateTask><id>x</id><id>y</id></a:UpdateTask></s:Body>",
    ]
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        before = application.traces.list()
        for body in bodies:
            response = client.post(
                "/soap",
                content=f"<s:Envelope {prefixes}>{body}</s:Envelope>",
                headers={"content-type": "application/soap+xml"},
            )
            assert response.status_code == 400, body
            assert ET.fromstring(response.content).find(f".//{{{SOAP}}}Fault") is not None
        wrong_type = client.post(
            "/soap",
            content=f"<s:Envelope {prefixes}><s:Body><a:Hello/></s:Body></s:Envelope>",
            headers={"content-type": "text/xml"},
        )
        assert wrong_type.status_code == 400
        assert application.traces.list() == before


def test_xml_size_encoding_declaration_and_entity_limits_reject_before_execution(application):
    samples = [
        b"<task>\x00</task>",
        b"\xff",
        b"<task>",
        b"x" * 16385,
        b"<?processing instruction?><task><prompt>hello</prompt></task>",
        b'<!DOCTYPE task [<!ENTITY x "hello">]><task><prompt>&x;</prompt></task>',
        b"<task><prompt>hello</prompt><extra>bad</extra></task>",
    ]
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        before = application.traces.list()
        for content in samples:
            response = client.post(
                "/api/v1/xml/tasks", content=content, headers={"content-type": "application/xml"}
            )
            assert response.status_code == 400
            assert ET.fromstring(response.content).findtext("detail") == "invalid task request"
        assert application.traces.list() == before
        valid_declaration = client.post(
            "/api/v1/xml/tasks",
            content='<?xml version="1.0"?><task><prompt>hello</prompt></task>',
            headers={"content-type": "application/xml"},
        )
        assert valid_declaration.status_code == 201
