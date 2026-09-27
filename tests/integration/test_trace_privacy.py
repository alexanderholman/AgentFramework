from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

import pytest

from af.bootstrap import create_application
from af.config import Settings
from af.framework.errors import UnsupportedWork, VerificationFailed


def test_persistent_execution_trace_contains_no_prompt_arguments_or_output(tmp_path):
    path = tmp_path / "private.sqlite3"
    app = create_application(settings=Settings(database_url=f"sqlite:///{path}"))
    secret = "private-token-ABC123"
    try:
        result = app.tasks.count_characters(secret, "A")
        assert result.output == "2" and result.prompt == secret
        trace = app.traces.get(result.trace_id)
        assert secret not in json.dumps(asdict(trace), default=str)
        assert trace.prompt == "[redacted]" and trace.arguments == {}
        assert trace.output == "[redacted]" and trace.verified
        with pytest.raises(UnsupportedWork) as error:
            app.tasks.execute(secret)
        assert secret not in str(error.value)
        with sqlite3.connect(path) as connection:
            rows = connection.execute("SELECT * FROM af_run_traces").fetchall()
        assert secret not in repr(rows)
    finally:
        app.shutdown()


def test_verifier_error_text_does_not_disclose_provider_detail():
    error = VerificationFailed("trace-id", "private output XYZ987")
    assert "XYZ987" not in str(error)
    assert "trace-id" in str(error)
