# Application-owned starter kits

`af new DESTINATION --template TEMPLATE --package PACKAGE` writes a new application
using AF's public composition and controller APIs. The destination must not exist;
existing files and symlinks are never overwritten. Python package names are bounded
identifiers and cannot shadow the framework, standard library or core dependencies.

```sh
af new my-project --template full-stack --package my_project
cd my-project
python -m pip install -e '.[dev]'
python -m pytest tests
python -m my_project
```

Install the approved matching AF wheel/distribution before installing the generated
application when that version is not available from your configured package index.
The generated `pyproject.toml` pins the AF version used for generation. No external
service, model provider or credential is needed to boot these starters.

| Template | Primary entry point | Application-owned source |
| --- | --- | --- |
| `basic-web` | HTTP factory and `/app` | Minimal HTML/CSS/JavaScript and shared task API |
| `pretty-web` | HTTP factory and `/app` | Styled responsive frontend plus AF's Pretty Web profile |
| `rest-api` | `/api/v1/tasks` | Public HTTP composition, without a generated frontend |
| `worker` | `python -m PACKAGE enqueue "hello"`, then `once` | Durable SQLite queue and a typed WorkerKernel handler |
| `tui` | `python -m PACKAGE` | Typed widgets, app provider, terminal and headless drivers |
| `full-stack` | HTTP factory and `/app` | Frontend, API, durable worker and shared CLI |

Every starter includes `application.py`, the shared console entry point, a pinned
package manifest, local-state ignore rules, primary-entry tests, a CI workflow and
an `af-starter.json` file listing content hashes. These are editable files in the
new application; there is no hidden source-generation service or remote loader.
The existing `af make procedure` scaffold remains separately available for one
procedure/provider contribution.

Applications keep their SQLite state in `var/` and close owned application/queue
resources. Web servers bind loopback port 8000 by default. Their public HTTP
composition retains AF's normal request contexts, controllers, lifecycle and
security middleware; the REST starter does not maintain a divergent REST kernel.
Inherited framework routes are still available. Configure authentication and
trusted ingress in application-owned composition before exposing a deployment.
The frontend inserts result text with `textContent` and serves scripts locally.

The worker starter rejects unknown job schemas and runs task work through a
bounded deterministic acceptance scope. Enqueue and consume use separate queue
connections, so jobs survive application restart. Delivery is at least once;
adding external non-idempotent effects requires corresponding business-level
idempotency. The starter does not advertise exactly-once execution.

The TUI starter contributes an application-owned `StarterTui` through
`bind_tui_application`. It resolves `TaskController` from each kernel-provided
turn scope, and uses the same state machine with a terminal or scripted driver:

```sh
python -m PACKAGE --once 'how many rs in strawberry?'
python -m PACKAGE --plain
```

The first command is deterministic headless execution; the second permits a plain
snapshot when no terminal is available. Focus, resizing, input handling and terminal
restoration belong to the shared TUI kernel and drivers.

`tests/feature/test_starter_scaffolds.py` generates all six variants in isolated
folders, verifies the manifest hashes and runs each generated project's tests in
a fresh Python process. The web tests create/read a verified task across application
restart, worker tests dispatch/settle a real local durable queue delivery, TUI tests
run typed headless events, and every variant boots its application-owned CLI.
This evidence covers starter composition and local primary entry points; it does
not certify a production deployment, external infrastructure or every terminal.
