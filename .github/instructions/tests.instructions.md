---
applyTo: "tests/**/*.py,tests/*.py"
---

# Test requirements

- Use pytest and assert observable behavior rather than private implementation.
- Keep tests deterministic, isolated, order-independent, and free of external
  network calls. Use temporary SQLite databases or injected repositories.
- Put pure behavior in `tests/unit`, real persistence boundaries in
  `tests/integration`, and complete CLI/HTTP/GraphQL/MCP behavior in
  `tests/feature`.
- Reuse the isolated application fixture in `tests/conftest.py` when several
  transports need the same composition; keep one-off fixtures near their test.
- Cover success and failure paths, including blank/invalid prompts, unsupported
  work, missing task IDs, verification failures, and CRUD lifecycle behavior.
- Cross-transport tests should prove that all adapters share application state
  and semantics rather than duplicating expected implementation details.
- Never loosen an assertion merely to accommodate a regression.

Run the narrow test subtree while iterating, followed by:

```bash
python scripts/af_dev.py --action check --execute
```

