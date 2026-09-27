# AF Architecture

AF is a classification-heavy, inference-light Python framework. Its reference application
implements one execution pipeline and one Task application boundary, then adapts them to
CLI, HTML, REST, GraphQL, and MCP interfaces.

## Runtime flow

```mermaid
flowchart TD
    I["CLI, HTML, REST, GraphQL, MCP"] --> C["TaskController"]
    C --> S["TaskService"]
    S --> A["PipelineAgent"]
    A --> E["Classifier, router, procedure, verifier"]
    A --> T["TraceRepository"]
    A --> G["Regression gate and reuse store"]
    S --> Q["TaskRepository"]
```

Every presentation adapter delegates to `TaskController`. Transport code translates input,
output, and expected errors; it does not implement classification, counting, verification,
reuse, or persistence rules.

## Composition root

`af.bootstrap.create_application()` is the composition root. It creates an explicit
`Container`, applies explicit service providers, and returns an `AFApplication` containing the
resolved settings and public runtime services.

The core providers include:

1. `_PersistenceProvider`, which installs a `Database`, `TaskRepository`, and
   `TraceRepository`, or accepts injected repository test doubles.
2. `_FrameworkProvider`, which registers the built-in procedures, classifier chain,
   verifier, router, trace repository, regression gate, reuse store, and `PipelineAgent`.
3. `_ApplicationProvider`, which binds a singleton `TaskService` and transient
   `TaskController`.

Composition also installs the finite science/catalogue, Ideon, search and queue/worker
providers. Optional extensions are explicit; introspection reports the actual provider graph.

The container uses explicit factories rather than constructor inspection or implicit
autowiring. `create_application()` also accepts replacement Task and trace repositories,
reuse storage, settings, and an optional selector. A selector may only propose an
installed typed procedure. Optional configured local selector adapters and separately
consented decision adapters remain infrastructure behind typed ports; they do not supply
final-answer execution or silently enable a remote fallback. See [decision providers](decision-providers.md).

### Container contracts and lifetimes

Container declarations are explicit and become immutable when the first application scope
is created. A binding may be unqualified or use a bounded `Qualifier`; aliases resolve to
the target identity, contextual bindings apply only to the immediate consumer, and tags
resolve an ordered tuple of declared service keys. `with_test_overrides()` forks declarations
with empty runtime caches; an explicit override also takes precedence over contextual
production bindings for that canonical service key. Declared instance values and factory
closure state remain shared unless explicitly overridden, so mutable instances are not
deep-cloned by a test fork.

The four lifetimes have distinct cache owners:

| Lifetime | Cache and ownership boundary |
| --- | --- |
| Singleton | Root container and all descendants |
| Application | One application scope |
| Invocation | One typed request, message, command, job, event, schedule, notification, or interaction scope (`InvocationKind`) |
| Transient | New value for each resolution, owned by the requesting scope |

Factories remain explicit and may be synchronous or asynchronous. Cached construction is
single-flight for concurrent callers, failures can be retried, and dependency or alias
cycles report their complete service-key chain. Synchronous resolution never starts an event
loop for an asynchronous factory. `resolve_async()` executes a synchronous factory on its
calling event-loop thread rather than silently changing the construction domain; waiting for
an already-running synchronous single flight remains asynchronous.

Scopes own factory-created closeable resources. `close()` and `aclose()` unwind child scopes
and resources exactly once in reverse successful-construction order; asynchronous-only
cleanup requires `aclose()`. Cleanup continues after ordinary failures and reports sanitized
aggregate evidence. Every hierarchy has one configurable total shutdown budget (30 seconds by
default). Work that ignores cancellation is abandoned after that budget with typed
`ShutdownTimeout` evidence, while scope/context authority is revoked and remaining cleanup is
attempted in reverse order. Synchronous disposers run in bounded worker threads and therefore
must be thread-safe; event-loop- or thread-affine resources must expose `aclose()` instead.
A synchronous factory that outlives a timed-out shutdown and later transfers a closeable
resource records a pending cleanup handoff; a subsequent `aclose()` drains that handoff on an
explicit event loop rather than starting hidden or untracked cleanup.
A closing or disposed scope and all descendants reject new resolution. Invocation scopes can
own the existing immutable `ExecutionContext`; resolution, context access, and owned-task
creation require the exact active invocation lease, and its ambient binding is removed when
the invocation exits. Invocation child work must use `Container.create_task()` so it is joined
or cancelled before cleanup. A raw `asyncio.create_task()` does not inherit scoped resolution
or context authority; detached work must cross an explicit typed boundary.

Factories, providers, and disposers are trusted in-process Python code. Resolver leases,
registry freezing, and context masking prevent accidental authority widening; they are not a
sandbox. Hostile extensions require a separate process boundary.

## Execution lifecycle

`TaskService.execute()` normalizes the prompt and passes a `WorkRequest` to
`PipelineAgent.handle()`. The agent then performs the AF lifecycle:

1. **Classify.** `ClassifierChain` tries `HelloClassifier`,
   `CharacterCountClassifier`, and finally `UnknownClassifier` in order. Deterministic
   matches stop here and never invoke a selector.
2. **Route.** `LadderRouter` selects a compatible procedure from `ProcedureRegistry` using
   its tier and the active routing policy. The default policy optimizes for cheapest and
   fastest.
3. **Select (unknown work only).** An optional selector may propose a registered procedure,
   typed arguments, and confidence. AF validates that proposal against the immutable
   catalogue, rejects unauthorized remote selection, enforces pre-execution policy, and fails closed to
   human handoff on any malformed or unsupported proposal.
4. **Execute.** AF first looks in `ReuseStore`; a miss invokes the chosen procedure.
5. **Verify.** `DeterministicVerifier` independently checks the procedure output.
6. **Record.** The pipeline builds an append-style trace containing registered intent,
   route, verification result, and ordered lifecycle metadata. Prompt, typed arguments,
   result content and free-form verifier detail are minimized before persistence;
   see [trace privacy](trace-privacy.md).
7. **Test.** A pure, replay-safe result is executed again through
   `ReplayRegressionGate` before promotion.
8. **Reuse.** A passing result is promoted to the in-process reuse store. A later hit is
   independently verified again before return.

The completed trace is persisted after the lifecycle events have been assembled. Unknown
or unroutable prompts persist a shorter unsupported trace before raising `UnsupportedWork`.
Failed verification is also explicit and never becomes a successful outcome.

The initial registered capabilities are:

- `HelloProcedure`, a static route returning `Hello, world!`; and
- `CharacterCountProcedure`, a procedural route that counts one character with an explicit
  case-insensitive loop.

Unknown work fails closed unless an embedding host injects a selector that proposes one of
those registered procedures. The selector never supplies the final user answer.

Agent-role recommendation is a separate read-only application boundary. It tokenizes a
normalized request and matches whole-word signals for `architect`, `builder`, `reviewer`,
and `tester`. A single specialist match is routed, no specialist match falls back to
`general`, and multiple specialist matches return an explicit ambiguous handoff without
selecting an agent. This boundary does not execute the selected agent or enter the procedure
pipeline; embedding hosts remain responsible for enforcing the returned decision.

## Task application boundary

`TaskController` is the shared MVC/application-facing controller. It delegates to
`TaskService`, which owns normalization, execution, and Task persistence semantics.

- `execute(prompt)` runs AF and persists a trace, but does not persist a Task.
- `create(prompt)` runs AF, then stores a new Task containing the verified outcome.
- `update(id, prompt)` loads the Task, re-executes the replacement prompt, and stores the
  new outcome while preserving the ID and creation time.
- `get`, `list`, and `delete` delegate to the Task repository.

A Task records its prompt, output, intent, route, tier, status, verification and reuse
flags, trace ID, and timestamps. It is separate from the append-style execution trace: one
Task can be updated to reference the trace of its latest execution.

## Persistence and reuse

The default application uses SQLAlchemy 2 with SQLite:

- `SqlAlchemyTaskRepository` stores Tasks in `af_tasks`.
- `SqlAlchemyTraceRepository` stores traces in `af_run_traces`.
- `Database` creates the schema and configures SQLite for the current process.

In-memory Task and trace repositories are available for isolated tests. Reuse currently
uses `InMemoryReuseStore`, so promoted results survive repeated calls within one application
process but not a process restart. Persistent Tasks and traces do not imply persistent reuse
cache entries.

## Transport adapters

| Adapter | Factory or entry point | Surface |
| --- | --- | --- |
| CLI | `af.interfaces.cli.create_cli()` | `af hello`, `af ask`, and `af tasks` CRUD |
| HTML MVC | `af.interfaces.web.create_web_router()` | Jinja Task pages and form actions |
| REST | `af.interfaces.rest.create_rest_router()` | `/api/v1/hello` and `/api/v1/tasks` CRUD |
| GraphQL | `af.interfaces.graphql.create_graphql_router()` | Task queries and mutations at `/graphql` |
| MCP | `af.interfaces.mcp.create_mcp_server()` | Typed task, workflow, decision and installed extension tools plus `af://manifest` |

`af.interfaces.http.create_http_app()` combines HTML, REST, and GraphQL in one FastAPI
application and places the shared `AFApplication` on `app.state.af`. MCP is currently a
separate FastMCP process started with `af-mcp`; it is not mounted into the combined FastAPI
application. Both factories can receive the same already-composed `AFApplication` in tests
or an embedding host.

Each adapter owns only its transport types:

- CLI uses Typer and JSON presentation helpers.
- HTML uses FastAPI form controllers and Jinja templates.
- REST uses Pydantic request and response models.
- GraphQL uses Strawberry types and an injected controller context.
- MCP uses FastMCP tools, MCP safety annotations, and typed structured results.

This keeps transport behaviour replaceable while the `TaskController` and pipeline remain
the single implementation of AF's way of working.

## Package map

| Package | Responsibility |
| --- | --- |
| `af.framework` | Contracts, container, routing, execution pipeline, reuse, and shared values |
| `af.domain` | Classifiers, procedures, verification, Task model, and repository port |
| `af.application` | Task use cases and shared controller |
| `af.infrastructure` | SQLAlchemy database, Task repositories, and trace repositories |
| `af.interfaces` | CLI, HTTP, HTML, REST, GraphQL, MCP, and presentation mappings |
| `af/views` | Jinja templates and layouts |
| `af.bootstrap` | Providers, dependency composition, and `AFApplication` |
| `af.config` | Environment-backed runtime settings |

To add a deterministic capability, implement and test its classifier, procedure, and
independent verifier behaviour, register the procedure and classifier at the composition
root, and preserve the same application boundary. A new capability should not require each
transport to implement its logic independently.
