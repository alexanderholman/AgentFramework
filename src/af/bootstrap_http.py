"""Composition for the finite SQLite HTTP request ledger."""

from pathlib import Path

from sqlalchemy.engine import make_url

from af.application.http_operations import HttpOperations
from af.bootstrap import AFApplication
from af.domain.tasks import TaskRepository
from af.infrastructure.http_operations import SQLiteHttpOperations
from af.infrastructure.repositories import SqlAlchemyTaskRepository


def create_http_operations(application: AFApplication) -> HttpOperations:
    repository = application.container.resolve_token(TaskRepository)
    url = make_url(application.settings.database_url)
    if (
        isinstance(repository, SqlAlchemyTaskRepository)
        and url.get_backend_name() == "sqlite"
        and url.database not in {None, "", ":memory:"}
    ):
        path = Path(str(url.database)).expanduser()
        return SQLiteHttpOperations(path.with_name(path.name + ".http.sqlite3"))
    # A non-persistent repository has a non-persistent ledger. Other database
    # compositions must explicitly inject a durable HttpOperations adapter.
    return SQLiteHttpOperations(":memory:")
