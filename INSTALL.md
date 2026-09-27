# Installation

AgentFramework requires Python 3.12 or newer. The source-development workflow also uses
Git and [uv](https://docs.astral.sh/uv/). An approved runtime wheel does not require Git,
the development extra, or a repository checkout to run deterministic procedures.

## Development clone and install

```bash
git clone https://github.com/alexanderholman/AgentFramework.git
cd AgentFramework
uv sync --locked --extra dev
```

The repository is private initially, so cloning requires a GitHub identity with access.
This editable development installation is not an immutable production deployment.

## Verify the installation

Run the complete, repeatable quality gate:

```bash
python3 scripts/af_dev.py --action check --execute
```

Then exercise the deterministic examples:

```bash
uv run af hello
uv run af ask "how many rs in strawberry"
```

The expected outputs are `Hello, world!` and `3`.

For focused development checks, `uv run af test tests/unit/test_pipeline.py -q`
forwards arguments to the installed pytest and preserves its exit status. It runs
trusted project test code in the current directory without initializing AF state.
The runtime-only wheel does not include pytest; install the development extra first.
This focused command does not replace the canonical full gate above.

Validate the source and wheel packaging path:

```bash
python3 scripts/af_dev.py --action package
python3 scripts/af_dev.py --action package --execute
```

The executed package action snapshots the clean Git commit into temporary directories and
builds the wheel from its source distribution. It exports only runtime requirements from
`uv.lock` as PEP 751 `pylock.toml`, preserves artifact URLs, verifies their hashes,
and installs them into a temporary target with source builds
disabled and uv offline. Prime the uv cache with the locked development install first; a missing
runtime wheel fails the gate instead of downloading or borrowing it from the development environment.
An explicit `UV_CACHE_DIR` is retained.

Smoke checks run outside the checkout with Python `-I -S`, loading only the temporary runtime
target. This proves a runtime-only install on the current host. It does not establish a clean
operating-system installation, Windows/macOS compatibility, an OS network sandbox, deployed
authentication, or production approval. Distribution remains private and alpha; no public package
registry or approved signed release is implied by a locally built wheel.

## Optional local setup and starter applications

```bash
uv run af init --plan --profile scheduler
# Apply the reviewed local plan explicitly:
uv run af init --non-interactive --apply --yes --profile scheduler
uv run af status
uv run af doctor --json
uv run af new my-app --template full-stack --package my_app
```

The default CLI and MCP factories detect `storage/machine.json`. Set `AF_MACHINE_ROOT` to
select another managed directory consistently for setup and subsequent commands. Managed setup
uses its own local declaration, with provider routes disabled. The basic examples also work
without setup using ordinary settings. See [machine setup](docs/machine-setup.md).

The generated application pins the matching AF version and owns its composition root, source,
tests and CI. Install the approved matching AF wheel/distribution before installing the generated
project; it does not promise that this private version exists on a public package index.
See [all six starters](docs/starter-kits.md) and [TUI profiles](docs/tui.md).

## Run the interfaces

```bash
# HTML MVC, REST, OpenAPI, and GraphQL
uv run af serve

# Local stdio MCP server
uv run af-mcp

# Local Streamable HTTP MCP server
AF_MCP_TRANSPORT=streamable-http uv run af-mcp
```

See [README.md](README.md) for endpoints and examples, and
[docs/chatgpt-mcp.md](docs/chatgpt-mcp.md) for ChatGPT integration constraints.
The HTTP documentation at `/docs` and GraphQL explorer at `/graphql/explorer` use only
local assets. Network listeners bind loopback by default. Requiring bearer tokens preserves
headless authorization; a host must explicitly provide any browser login/credential integration.

## Update

```bash
git pull --ff-only
uv sync --locked --extra dev
python3 scripts/af_dev.py --action check --execute
```

Back up retained state and keys before updating an existing installation; follow the
[data lifecycle](docs/data-lifecycle.md) restore instructions and verify the restored copy.
Runtime state is stored under `storage/` by default and is intentionally ignored by Git.
Removing the checkout does not remove any separately deployed database or MCP service.

## Production status

AgentFramework is not yet production-ready. Do not operate a service from an editable
checkout. The first candidate profile permits one SQLite database, one dedicated-account
MCP-stdio process, and quiesced offline CLI maintenance; every HTTP/MCP network listener is
excluded. The target,
release blockers, artifact requirements, backup/restore evidence, and rollback expectations
are defined in [Private production MVP](docs/production-mvp.md).

```bash
python3 scripts/af_dev.py --action release-validate --execute
python3 scripts/af_dev.py --action release-report --execute
python3 scripts/af_dev.py --action release-check --execute
```

The final command intentionally exits `1` while any blocking declaration remains unmet or
pending. Exit `0` would mean the manifest declares every policy gate complete, not that the
evidence was independently verified or deployment authorized; candidate-bound evidence and
maintainer approval remain external gates.
