"""Provider installation executes through the same verified AF pipeline."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from af.bootstrap import create_application
from af.config import Settings
from af.domain.catalogue_procedures import (
    CatalogueClassifier,
    CatalogueProcedure,
    CatalogueVerifier,
)
from af.framework.types import Classification, ProcedureResult, WorkRequest
from af.interfaces.cli import create_cli
from af.interfaces.http import create_http_app


def test_provider_executes_and_reuses_algorithm_and_problem_through_cli_rest():
    application = create_application(settings=Settings())
    try:
        prompt = 'problem order-and-locate {"values":[9,2,9,-1],"target":9}'
        cli = CliRunner().invoke(create_cli(application), ["ask", prompt])
        assert cli.exit_code == 0, cli.stdout
        payload = json.loads(cli.stdout)
        assert payload["value"] == {"ordered": [-1, 2, 9, 9], "index": 2}
        with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
            result = client.post("/api/v1/tasks", json={"prompt": prompt})
            assert result.status_code == 201, result.text
            assert json.loads(result.json()["output"]) == payload
            assert result.json()["verified"] is True
            assert application.traces.get(result.json()["trace_id"]).reused is True
            algorithm = client.post(
                "/api/v1/tasks", json={"prompt": 'algorithm sum-integers {"values":[1,2,3]}'}
            )
            assert algorithm.status_code == 201
            assert json.loads(algorithm.json()["output"])["value"] == 6
    finally:
        application.shutdown()


def test_verifier_rejects_tampered_output_evidence_and_unrelated_routes():
    classifier, procedure, verifier = (
        CatalogueClassifier(),
        CatalogueProcedure(),
        CatalogueVerifier(),
    )
    request = WorkRequest('algorithm insertion-sort {"values":[2,1]}')
    classification = classifier.classify(request)
    assert classification is not None
    result = procedure.execute(classification)
    assert verifier.verify(request, classification, procedure, result).passed
    forged = json.loads(result.output)
    forged["value"] = [2, 1]
    assert not verifier.verify(
        request, classification, procedure, ProcedureResult(json.dumps(forged), forged)
    ).passed
    assert not verifier.verify(
        request, classification, procedure, ProcedureResult(result.output, {})
    ).passed
    assert not verifier.verify(
        request, classification, procedure, ProcedureResult("no JSON")
    ).passed
    unknown = Classification("unknown", {}, 0, "test")
    assert verifier.verify(request, unknown, procedure, result) is None
    assert classifier.classify(WorkRequest("please run arbitrary code")) is None
    with pytest.raises(ValueError):
        procedure.validate(unknown)
    with pytest.raises(ValueError):
        procedure.validate(
            Classification("catalogue.execute", {"request": "algorithm absent {}"}, 1, "test")
        )
