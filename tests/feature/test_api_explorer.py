"""Installed documentation remains usable under AF's same-origin CSP."""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from af.interfaces.http import create_http_app


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_explorers_use_local_assets_and_named_controls_with_no_inline_code(application):
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        for path in ("/docs", "/graphql/explorer"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert "default-src 'self'" in response.headers["content-security-policy"]
            page = Page()
            page.feed(response.text)
            assert sum(tag == "main" for tag, _ in page.tags) == 1
            labels = {attrs["for"] for tag, attrs in page.tags if tag == "label"}
            for tag, attrs in page.tags:
                assert not any(key.startswith("on") for key in attrs)
                if tag in {"input", "select", "textarea"}:
                    assert attrs["id"] in labels
                if tag == "script":
                    assert attrs["src"].startswith("/assets/")
                if tag == "link" or tag == "script":
                    asset = attrs.get("src", attrs.get("href"))
                    assert asset.startswith("/assets/")
                    assert client.get(asset).status_code == 200
            assert '<button id="send-request" type="submit" disabled>' in response.text
            assert "cdn.jsdelivr" not in response.text
        assert client.get("/redoc", follow_redirects=False).headers["location"] == "/docs"
        assert "type Query" in client.get("/graphql/schema.graphql").text
        assert "/api/v1/tasks" in client.get("/openapi.json").json()["paths"]


def test_explorer_request_shapes_create_and_read_the_same_task(application):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the browser request-building code")
    asset = Path(__file__).resolve().parents[2] / "src/af/views/assets/explorer.js"
    program = """
const explorer = require(process.argv[1]);
const create = explorer.buildRestRequest('POST /api/v1/tasks', {}, '{}', '{"prompt":"hello"}');
const query = 'query { tasks(limit: 10) { id output verified } }';
const graphql = explorer.buildGraphQLRequest(query, '{}', '');
console.log(JSON.stringify([create, graphql]));
"""
    completed = subprocess.run(
        [node, "-e", program, str(asset)], capture_output=True, text=True, check=True, timeout=10
    )
    create, graphql = json.loads(completed.stdout)
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        created = client.request(
            create["method"],
            create["path"],
            content=create["body"],
            headers={"content-type": "application/json"},
        )
        assert created.status_code == 201
        assert created.json()["output"] == "Hello, world!"
        response = client.request(
            graphql["method"],
            graphql["path"],
            content=graphql["body"],
            headers={"content-type": "application/json"},
        )
        assert response.json()["data"]["tasks"][0]["id"] == created.json()["id"]
        assert response.json()["data"]["tasks"][0]["verified"] is True


def test_browser_request_guards_and_explicit_submit_behavior():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute explorer browser behavior tests")
    script = Path(__file__).resolve().parents[1] / "js/test_explorer.cjs"
    completed = subprocess.run(
        [node, "--test", str(script)], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_documentation_escapes_schema_text_and_excludes_non_json_operations():
    from fastapi import FastAPI

    from af.interfaces.api_explorer import create_api_explorer_router

    api = FastAPI(docs_url=None, redoc_url=None)
    api.include_router(
        create_api_explorer_router(
            lambda: {
                "paths": {
                    "/api/v1/tasks": {
                        "parameters": [],
                        "get": {"summary": "<script>injected()</script>"},
                        "post": {"requestBody": {"content": {"application/xml": {}}}},
                    },
                    "/operator-only": {"get": {}},
                }
            },
            lambda: "type Query { value: String! }",
        )
    )
    with TestClient(api) as client:
        page = client.get("/docs").text
        assert "&lt;script&gt;injected()&lt;/script&gt;" in page
        assert "<script>injected()" not in page
        assert "POST /api/v1/tasks" not in page
        assert "/operator-only" not in page
