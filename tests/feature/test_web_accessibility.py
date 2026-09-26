"""Rendered landmarks and hostile form input at the installed HTML boundary."""

from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from af.interfaces.http import create_http_app


class Landmarks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.main = []

    def handle_starttag(self, tag, attrs):
        if tag == "main":
            self.main.append(dict(attrs))


def test_installed_html_has_one_reachable_main_landmark(application):
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        for path in ("/", "/tasks", "/operations?language=ar&territory=EG"):
            response = client.get(path)
            assert response.status_code == 200
            landmarks = Landmarks()
            landmarks.feed(response.text)
            assert len(landmarks.main) == 1, path
            assert landmarks.main[0]["id"] == "main-content"
            assert 'href="#main-content"' in response.text
        assert 'lang="en" dir="ltr" aria-labelledby="operations-title"' in response.text


@pytest.mark.parametrize(
    ("token", "origin"),
    [("nonce.é", None), (None, "http://["), (None, "http://127.0.0.1/")],
)
def test_malformed_csrf_and_origin_are_denied_without_mutation(application, token, origin):
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        client.get("/")
        response = client.post(
            "/tasks",
            data={"prompt": "hello", "csrf_token": token or client.cookies["af-csrf"]},
            headers={"origin": origin} if origin else {},
        )
        assert response.status_code == 403
        assert application.tasks.list() == []


def test_nonascii_session_signature_is_discarded_without_server_error(application):
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        response = client.get("/", headers=[(b"cookie", b'af-session="e30=.\\351"')])
        assert response.status_code == 200
        assert "Max-Age=0" in response.headers["set-cookie"]


def test_invalid_update_preserves_the_empty_field_and_associated_error(application):
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        task = client.post("/api/v1/tasks", json={"prompt": "hello"}).json()
        client.get(f"/tasks/{task['id']}")
        response = client.post(
            f"/tasks/{task['id']}",
            data={"prompt": "", "csrf_token": client.cookies["af-csrf"]},
        )
        assert response.status_code == 422
        assert 'name="prompt" value=""' in response.text
        assert 'aria-invalid="true" aria-describedby="prompt-error"' in response.text
        assert client.get(f"/api/v1/tasks/{task['id']}").json()["prompt"] == "hello"
