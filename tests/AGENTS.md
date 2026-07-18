# AF test agent guidance

Tests are executable specifications for AF's shared way of working.

- `unit/` isolates framework, domain, application, and container behavior.
- `integration/` exercises real infrastructure such as SQLAlchemy and SQLite.
- `feature/` exercises public CLI, HTML, REST, GraphQL, and MCP behavior.

Tests must be deterministic, order-independent, and network-free. Use the
isolated application fixture in `tests/conftest.py` for cross-transport behavior
and temporary storage for persistence. Prefer behavior and contract assertions
over private attributes or implementation call counts. Cover failure paths and
add a regression test before fixing a bug.

Run a narrow pytest path while iterating and the complete check before finishing:

```bash
python scripts/af_dev.py --action check --execute
```

