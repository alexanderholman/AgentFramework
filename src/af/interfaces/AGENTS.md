# AF interface agent guidance

This directory contains Typer, FastAPI HTML/REST, Strawberry GraphQL, presenter,
and FastMCP adapters. Interface code may depend on application/domain output
types, but it must not implement AF business rules or access SQLAlchemy directly.

- Build adapters with injected `AFApplication` or `TaskController` factories.
- Keep `hello`, natural-language execution, and persisted Task CRUD behavior
  equivalent across every surface.
- Validate external input and translate AF exceptions into stable transport
  errors without exposing traces or internals unintentionally.
- Preserve REST status semantics, non-blocking GraphQL resolvers, escaped Jinja
  output, machine-readable CLI CRUD, and truthful MCP tool annotations.
- Add or update `tests/feature` coverage for any externally visible change.

Run the relevant feature test first, then
`python scripts/af_dev.py --action check --execute`.

