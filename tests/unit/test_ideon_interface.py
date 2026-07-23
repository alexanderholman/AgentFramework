from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from af.infrastructure.ideon_reference import create_reference_ideon_runtime
from af.interfaces.ideons import create_ideon_router


def client() -> TestClient:
    app = FastAPI()
    app.include_router(create_ideon_router(create_reference_ideon_runtime().controller))
    return TestClient(app)


def test_rest_interpret_returns_the_canonical_open_record() -> None:
    with client() as http:
        response = http.post(
            "/api/v1/ideons/interpret",
            json={
                "surface": "sat on the bank",
                "source_id": "fixture:rest-open",
                "rights": "internal-test-fixture",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["record_id"]) == 64
    assert payload["decision"]["decision"] == "ask"
    assert payload["openings"] == ["who:actor", "where:location-sense"]
    assert isinstance(payload["ideon_key"], str)
    assert set(payload["questions"]) == {
        "who",
        "what",
        "where",
        "when",
        "whern",
        "why",
        "how",
        "is",
        "could",
        "would",
        "should",
    }


def test_rest_context_closes_bank_and_identity_fields_are_validated() -> None:
    with client() as http:
        resolved = http.post(
            "/api/v1/ideons/interpret",
            json={
                "surface": "sat on the bank",
                "source_id": "fixture:rest-river",
                "rights": "internal-test-fixture",
                "context": {"actor": "alice", "domain": "river"},
                "identity_fields": ["actor", "domain"],
            },
        )
        invalid = http.post(
            "/api/v1/ideons/interpret",
            json={
                "surface": "sat",
                "source_id": "fixture:invalid",
                "rights": "internal-test-fixture",
                "identity_fields": ["missing"],
            },
        )

    assert resolved.status_code == 200
    assert resolved.json()["decision"]["decision"] == "answer"
    assert resolved.json()["selected_senses"][-1] == "bank.river-edge"
    assert invalid.status_code == 422


def test_rest_rejects_blank_or_oversized_controlled_input() -> None:
    with client() as http:
        blank = http.post(
            "/api/v1/ideons/interpret",
            json={"surface": "   ", "source_id": "fixture:blank", "rights": "fixture"},
        )
        extra = http.post(
            "/api/v1/ideons/interpret",
            json={
                "surface": "sat",
                "source_id": "fixture:extra",
                "rights": "fixture",
                "unexpected": True,
            },
        )

    assert blank.status_code == 422
    assert extra.status_code == 422


def test_rest_serializes_a_large_injective_key_without_an_internal_error() -> None:
    surface = " ".join(f"unknown{index}" for index in range(20))

    with client() as http:
        response = http.post(
            "/api/v1/ideons/interpret",
            json={
                "surface": surface,
                "source_id": "fixture:rest-large-key",
                "rights": "internal-test-fixture",
            },
        )

    assert response.status_code == 200
    assert len(response.json()["ideon_key"]) > 4_300
