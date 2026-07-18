from __future__ import annotations

import pytest

from af.bootstrap import AFApplication, create_application
from af.framework.reuse import InMemoryReuseStore
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.tracing import InMemoryTraceRepository


@pytest.fixture
def application() -> AFApplication:
    return create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        reuse_store=InMemoryReuseStore(),
    )
