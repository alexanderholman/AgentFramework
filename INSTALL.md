# Installation

AgentFramework requires Python 3.12 or newer, Git, and
[uv](https://docs.astral.sh/uv/).

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

Validate the source and wheel packaging path:

```bash
uv run --no-config --no-env-file --offline --locked --no-sources --extra dev python scripts/release_gates.py --action package
uv run --no-config --no-env-file --offline --locked --no-sources --extra dev python scripts/release_gates.py --action package --execute
```

The executed package action snapshots the clean Git commit into temporary directories, builds
the wheel from that source distribution with uv resolution offline and configuration disabled,
installs it outside the checkout without dependencies, and runs deterministic smoke checks
using the locked development environment. It is not an operating-system network sandbox or a
clean-host production installation test.

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

## Update

```bash
git pull --ff-only
uv sync --locked --extra dev
python3 scripts/af_dev.py --action check --execute
```

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
uv run --no-config --no-env-file --offline --locked --no-sources --extra dev python scripts/release_gates.py --action validate
uv run --no-config --no-env-file --offline --locked --no-sources --extra dev python scripts/release_gates.py --action report
uv run --no-config --no-env-file --offline --locked --no-sources --extra dev python scripts/release_gates.py --action check
```

The final command intentionally exits `1` while any blocking declaration remains unmet or
pending. Exit `0` would mean the manifest declares every policy gate complete, not that the
evidence was independently verified or deployment authorized; candidate-bound evidence and
maintainer approval remain external gates.
