---
applyTo: "src/af/interfaces/**/*.py,src/af/interfaces/*.py,src/af/views/templates/**/*.html"
---

# Interface and view requirements

- Interfaces are thin adapters. Inject or resolve `AFApplication` once, then
  delegate execution and persisted `Task` CRUD to `TaskController`.
- Do not implement classification, counting, verification, reuse, or repository
  queries inside CLI commands, HTTP routes, GraphQL resolvers, MCP tools, or
  templates.
- Keep transport schemas and presenters at this boundary and preserve equivalent
  outputs and errors across CLI, HTML, REST, GraphQL, and MCP.
- REST uses `/api/v1`, meaningful status codes, validated Pydantic payloads, and
  an empty body for successful `204` deletes.
- GraphQL async resolvers must not block the event loop when invoking synchronous
  application services; preserve structured GraphQL error information.
- MCP tools use structured output and accurate read-only, destructive,
  idempotent, and open-world annotations. Stdio is the safe default; an
  unauthenticated HTTP transport must bind to loopback by default.
- HTML forms use semantic labels, escaped content, clear validation feedback,
  and post/redirect/get for mutations.
- CLI commands return stable machine-readable JSON for Task CRUD and concise
  plain text for `hello` and `ask`.

Run the matching feature tests and then the complete AF check.

