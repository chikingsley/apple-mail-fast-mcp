"""Smoke tests for the MCP server over the real stdio transport.

These tests spawn the server as a subprocess and connect via the MCP client
SDK. They catch startup and protocol bugs without mocking the transport:

- Startup errors (import failures, missing env, FastMCP banner interfering
  with stdout framing).
- JSON-RPC framing issues over pipes.
- Stream lifecycle bugs (handshake timeout, stream closure, premature EOF).

Keep scope narrow: a handshake + list_tools round-trip is enough to cover
the transport layer. Real tool behavior belongs in tests/live.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.regression

EXPECTED_TOOLS = {
    # Discovery
    "list_accounts",
    "list_mailboxes",
    "list_rules",
    "search_messages",
    "get_messages",
    "get_thread",
    "get_statistics",
    # Drafts lifecycle (#134)
    "create_draft",
    "update_draft",
    "delete_draft",
    # Mutations
    "update_message",
    "save_attachments",
    "get_attachment_content",
    "create_mailbox",
    "update_mailbox",
    "delete_mailbox",
    "delete_messages",
    # Rule CRUD (#63)
    "create_rule",
    "update_rule",
    "delete_rule",
    # Templates (#30)
    "list_templates",
    "get_template",
    "save_template",
    "delete_template",
    "render_template",
}

# Per #50 acceptance: test must complete within 15 seconds.
HANDSHAKE_TIMEOUT_SECONDS = 15.0


async def _list_tools_over_stdio() -> set[str]:
    """Spawn the server, complete the MCP handshake, and return the tool names."""
    params = StdioServerParameters(
        command="uv",
        args=["run", "--locked", "apple-mail-fast-mcp"],
        env=None,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.list_tools()
        return {t.name for t in result.tools}


async def test_stdio_subprocess_lists_all_tools() -> None:
    """Regression #50: the real stdio handshake surfaces every MCP tool."""
    names = await asyncio.wait_for(_list_tools_over_stdio(), timeout=HANDSHAKE_TIMEOUT_SECONDS)
    assert names == EXPECTED_TOOLS
