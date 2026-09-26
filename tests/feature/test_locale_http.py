"""Real HTML and REST locale selection never changes shared domain decisions."""

import json
import re
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from af.interfaces.http import create_http_app


class Elements(HTMLParser):
    def __init__(self, document: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.feed(document)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def tagged(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for name, attrs in self.tags if name == tag]


@pytest.mark.parametrize(
    "language,territory,resolved,direction",
    [
        ("en", "GB", "en", "ltr"),
        ("fr", "CA", "fr", "ltr"),
        ("ar", "EG", "ar", "rtl"),
        ("en-XA", None, "en", "ltr"),
        ("ar-XB", None, "ar", "rtl"),
        ("qaa-ZZ", None, "en", "ltr"),
    ],
)
def test_html_and_rest_locale_selection_keep_one_canonical_science_result(
    application, language, territory, resolved, direction
):
    task = application.tasks.create("physics kinetic_energy mass=2 kg speed=3 m/s")
    query = {"language": language}
    if territory is not None:
        query["territory"] = territory
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        canonical = client.get(f"/api/v1/tasks/{task.id}").json()
        localized = client.get(f"/api/v1/tasks/{task.id}", params=query)
        assert localized.status_code == 200
        payload = localized.json()
        presentation = payload.pop("presentation")
        assert payload == canonical
        assert presentation["resolved_language"] == resolved
        assert presentation["direction"] == direction
        assert localized.headers["content-language"] == resolved
        assert json.loads(payload["output"])["reason_code"] == "science_verified"
        page = client.get(f"/tasks/{task.id}", params=query)
        assert page.status_code == 200
        elements = Elements(page.text)
        assert elements.tagged("html")[0] == {"lang": resolved, "dir": direction}
        assert any(attrs.get("id") == "main-content" for attrs in elements.tagged("main"))
        assert any(attrs.get("href") == "#main-content" for attrs in elements.tagged("a"))
        assert {"language", "territory"} <= {
            attrs.get("name") for attrs in elements.tagged("input")
        }
        assert page.headers["content-language"] == resolved
        if resolved in {"fr", "ar"}:
            assert 'id="translation-notice"' in page.text
        if language in {"en-XA", "ar-XB"}:
            assert "···" in page.text
        assert task.output in page.text.replace("&#34;", '"')
        assert application.tasks.get(task.id) == task


def test_html_locale_survives_create_update_delete_and_has_usable_external_styles(application):
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        page = client.get("/?language=fr&territory=CA")
        assert "Langue" in page.text and "Territoire" in page.text
        elements = Elements(page.text)
        form = next(item for item in elements.tagged("form") if item.get("method") == "post")
        csrf = next(
            item["value"] for item in elements.tagged("input") if item.get("name") == "csrf_token"
        )
        created = client.post(form["action"], data={"prompt": "hello", "csrf_token": csrf})
        assert created.status_code == 200
        assert "language=fr" in str(created.url) and "territory=CA" in str(created.url)
        task = application.tasks.list()[0]
        assert task.output == "Hello, world!"
        elements = Elements(created.text)
        update, delete = [item for item in elements.tagged("form") if item.get("method") == "post"]
        changed = client.post(
            update["action"], data={"prompt": "math add a=1 b=2", "csrf_token": csrf}
        )
        assert changed.status_code == 200
        assert json.loads(application.tasks.get(task.id).output)["quantity"]["exact"] == "3"
        deleted = client.post(delete["action"], data={"csrf_token": csrf})
        assert deleted.status_code == 200 and "language=fr" in str(deleted.url)
        assert application.tasks.list() == []
        stylesheet = client.get("/assets/af.css")
        assert stylesheet.status_code == 200
        assert stylesheet.headers["content-type"].startswith("text/css")
        assert ":focus-visible" in stylesheet.text and "prefers-reduced-motion" in stylesheet.text
        assert "style-src 'unsafe-inline'" not in page.headers["content-security-policy"]


@pytest.mark.parametrize(
    "query", ["language=bad_tag", "territory=toolong", "language=fr&language=ar", "language="]
)
def test_invalid_locale_is_denied_before_html_or_rest_writes(application, query):
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        index = client.get("/")
        token = re.search(r'name="csrf_token" value="([^"]+)"', index.text).group(1)
        html = client.post(f"/tasks?{query}", data={"prompt": "hello", "csrf_token": token})
        rest = client.post(f"/api/v1/tasks?{query}", json={"prompt": "hello"})
        assert html.status_code == rest.status_code == 400
        assert application.tasks.list() == []


def test_rest_create_update_list_and_hello_offer_optional_presentation(application):
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        created = client.post("/api/v1/tasks?language=fr&territory=GB", json={"prompt": "hello"})
        assert created.status_code == 201
        task = created.json()
        assert task["presentation"]["territory"] == "GB"
        assert task["presentation"]["resolved_language"] == "fr"
        changed = client.patch(
            f"/api/v1/tasks/{task['id']}?language=ar", json={"prompt": "math add a=1 b=2"}
        )
        assert changed.json()["presentation"]["direction"] == "rtl"
        listed = client.get("/api/v1/tasks?language=en-XA").json()
        assert listed["total"] == 1
        assert listed["items"][0]["presentation"]["text"].startswith("[")
        plain = client.get("/api/v1/tasks").json()
        assert "presentation" not in plain["items"][0]
        hello = client.post("/api/v1/hello?language=fr").json()
        assert hello["output"] == "Hello, world!" and hello["verified"] is True
        assert hello["presentation"]["resolved_language"] == "fr"
