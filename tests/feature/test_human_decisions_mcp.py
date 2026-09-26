from __future__ import annotations

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from af.bootstrap import create_application
from af.config import Settings
from af.framework.context import AcceptanceScope, ExecutionContext, Principal, use_context
from af.interfaces.mcp import create_mcp_server


def test_mcp_human_decision_restart_preserves_authority_consumes_once_and_revokes(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'state.db'}")
    owner = ExecutionContext(
        principal=Principal("owner", "human"),
        tenant_id="a",
        requested_scope=AcceptanceScope.DETERMINISTIC,
        effective_scope=AcceptanceScope.DETERMINISTIC,
    )
    stranger = ExecutionContext(
        principal=Principal("other", "human"),
        tenant_id="a",
        requested_scope=AcceptanceScope.DETERMINISTIC,
        effective_scope=AcceptanceScope.DETERMINISTIC,
    )
    app = create_application(settings=settings)

    async def request():
        with use_context(owner):
            async with create_connected_server_and_client_session(
                create_mcp_server(app)
            ) as session:
                result = await session.call_tool(
                    "af_request_human_decision", {"prompt": "confirm: hello"}
                )
                assert not result.isError
                return result.structuredContent

    try:
        pending = anyio.run(request)
    finally:
        app.shutdown()
    app = create_application(settings=settings)

    async def respond():
        with use_context(stranger):
            async with create_connected_server_and_client_session(
                create_mcp_server(app)
            ) as session:
                denied = await session.call_tool(
                    "af_get_human_decision", {"decision_id": pending["decision_id"]}
                )
                assert denied.isError
        with use_context(owner):
            async with create_connected_server_and_client_session(
                create_mcp_server(app)
            ) as session:
                viewed = await session.call_tool(
                    "af_get_human_decision", {"decision_id": pending["decision_id"]}
                )
                assert not viewed.isError
                assert "response_secret" not in viewed.structuredContent
                args = {
                    "decision_id": pending["decision_id"],
                    "response_secret": pending["response_secret"],
                    "choice_id": "confirm",
                }
                result = await session.call_tool("af_respond_human_decision", args)
                assert not result.isError, result
                assert result.structuredContent["result"]["output"] == "Hello, world!"
                assert (await session.call_tool("af_respond_human_decision", args)).isError
                next_pending = await session.call_tool(
                    "af_request_human_decision", {"prompt": "confirm: hello"}
                )
                revoked = await session.call_tool(
                    "af_revoke_human_decision",
                    {"decision_id": next_pending.structuredContent["decision_id"]},
                )
                assert not revoked.isError
                assert revoked.structuredContent["reason"] == "revoked"

    try:
        anyio.run(respond)
    finally:
        app.shutdown()
