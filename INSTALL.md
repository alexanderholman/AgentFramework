# Installation

AgentFramework requires Python 3.12 or newer, Git, and
[uv](https://docs.astral.sh/uv/).

## Clone and install

```bash
git clone https://github.com/alexanderholman/AgentFramework.git
cd AgentFramework
uv sync --locked --extra dev
```

The repository is private initially, so cloning requires a GitHub identity with access.

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
