"""Regressions from the September shared communications audit."""

from unittest.mock import patch

import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.client.elicitation import ElicitResult

from apple_mail_fast_mcp.code_mode import communications_code_mode
from apple_mail_fast_mcp.mail_connector import AppleMailConnector
from apple_mail_fast_mcp.server import _elicit_confirmation


def test_regression_numeric_body_read_retains_scope():
    """Regression: an indexed ID must not trigger IMAP login or scan every account."""
    connector = AppleMailConnector()
    with (
        patch.object(connector, "_imap_get_message") as imap,
        patch.object(connector, "_run_applescript", return_value='{"id":"42"}') as run,
    ):
        assert (
            connector.get_message("42", account="Gmail", mailbox="[Gmail]/All Mail")["id"] == "42"
        )
    imap.assert_not_called()
    script = run.call_args.args[0]
    assert "repeat with acc in accounts" not in script
    assert "repeat with mb in mailboxes of acc" not in script
    assert 'resolveMailbox(acc, "[Gmail]/All Mail")' in script


@pytest.mark.asyncio
async def test_regression_code_mode_preserves_confirmation_and_catalog():
    """Regression: smaller discovery must retain schemas and a declined mutation gate."""
    app = FastMCP("regression", transforms=[communications_code_mode()])
    writes = []

    @app.tool
    async def guarded(ctx: Context) -> dict[str, object]:
        error = await _elicit_confirmation(ctx, "Confirm simulated write", "delete_messages", {})
        if error:
            return error
        writes.append(True)
        return {"success": True}

    async def decline(*args):  # ruff: ignore[unused-async] - async callback required by MCP
        return ElicitResult(action="decline")

    async with Client(app, elicitation_handler=decline, mode="legacy") as client:
        assert {t.name for t in await client.list_tools()} == {"search", "get_schema", "execute"}
        schema = await client.call_tool("get_schema", {"tools": ["guarded"]})
        assert "guarded" in schema.content[0].text
        result = await client.call_tool(
            "execute", {"code": 'return await call_tool("guarded", {})'}
        )
        assert result.data["success"] is False
        assert result.data["error_type"] == "cancelled"
    assert writes == []
