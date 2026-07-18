# ChatGPT and MCP

AF exposes its application boundary through the official Python MCP SDK. MCP supplies the
executable capabilities; the repository skill at `.agents/skills/af-work/` supplies concise
guidance for choosing them. The skill contains no copy of AF's business logic.

## MCP catalogue

`af.interfaces.mcp.create_mcp_server()` registers these tools against the same
`AFApplication.tasks` controller used by the other transports:

| Tool | Purpose | Effect |
| --- | --- | --- |
| `af_hello` | Execute the tested static Hello World route | Appends an execution trace |
| `af_ask` | Classify and execute a supported prompt | Appends an execution trace |
| `af_count_characters` | Count one character through the deterministic AF route | Appends an execution trace |
| `af_list_tasks` | List persisted Tasks | Read-only |
| `af_get_task` | Read a Task by exact ID | Read-only |
| `af_create_task` | Execute a prompt and persist its verified Task | Additive write |
| `af_update_task` | Replace a Task prompt and re-execute it | Destructive write |
| `af_delete_task` | Delete a Task by exact ID | Destructive write |

The `af://manifest` resource describes AF's lifecycle, registered routes, and tier
vocabulary. Tool results are typed structured data and include the route, tier,
verification state, reuse state, and trace ID where applicable.

Executing `af_hello`, `af_ask`, or `af_count_characters` is computationally pure, but the
AF pipeline appends a trace and may promote a regression-tested result into the in-process
reuse store. Their tool annotations must reflect that runtime write. Tool annotations
describe effects; they are not an authorization mechanism.

## Local stdio

Stdio is the default transport and is the simplest option for a local Codex client or
another MCP host that can start AF as a subprocess.

From the repository root:

```bash
uv sync
uv run af-mcp
```

The equivalent environment setting is:

```bash
AF_MCP_TRANSPORT=stdio uv run af-mcp
```

A project-local Codex configuration can start the same command:

```toml
[mcp_servers.af]
command = "uv"
args = ["run", "af-mcp"]
cwd = "/absolute/path/to/AgentFramework"
default_tools_approval_mode = "writes"
```

Replace the local filesystem path with the real checkout path. Stdio is a local process
transport; it is not an endpoint ChatGPT web can call.

## Loopback Streamable HTTP

For local HTTP development, select Streamable HTTP explicitly:

```bash
AF_MCP_TRANSPORT=streamable-http \
AF_HOST=127.0.0.1 \
AF_PORT=8000 \
uv run af-mcp
```

The MCP endpoint is then:

```text
http://127.0.0.1:8000/mcp
```

The current server uses stateless MCP sessions with JSON responses. Its default host is
loopback, which keeps the unauthenticated development server off the LAN. The standalone
MCP HTTP process and `af serve` both default to port 8000, so run them on different ports
if both are needed at once.

This endpoint is for local development only. Do not bind the current unauthenticated Task
CRUD server to a public interface.

## ChatGPT web deployment boundary

ChatGPT web cannot reach `127.0.0.1`, cannot launch the stdio command on the user's
machine, and does not read local Codex MCP configuration. It needs a real, reachable HTTPS
Streamable HTTP endpoint.

Before connecting AF to ChatGPT web, a deployment must add and verify:

- TLS termination and a stable public `/mcp` endpoint;
- authentication and authorization for every Task operation;
- per-user or per-workspace data isolation;
- request-size limits, rate limits, timeouts, and audit-safe logging;
- secret management and production database lifecycle controls; and
- the intended proxy and host-header policy.

The current reference server does not implement that production authentication boundary,
so local execution must not be described as an already connected ChatGPT app. Once an
authenticated deployment exists, use its actual HTTPS URL when creating a developer-mode
ChatGPT app or declaring an MCP dependency. Do not add a guessed or placeholder remote URL
to skill metadata.

For current protocol and product guidance, consult the
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x) and
[OpenAI Apps SDK MCP server guide](https://developers.openai.com/apps-sdk/build/mcp-server).

## Skill behaviour and fallback

When the `af-work` skill is active, it should use the narrowest matching MCP tool and treat
the structured result as authoritative. In a local Codex environment without MCP, it may
fall back to the matching CLI command:

```bash
uv run af hello
uv run af ask "how many rs in strawberry"
uv run af tasks list
uv run af tasks create "how many rs in strawberry"
uv run af tasks show TASK_ID
uv run af tasks update TASK_ID "how many bs in blueberry"
uv run af tasks delete TASK_ID
```

ChatGPT web has no local-shell fallback. If the remote MCP app is not installed and
connected, the skill should say that AF is unavailable rather than emulate AF's execution
or fabricate trace metadata.
