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


@pytest.mark.allow_real_io
@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="AppleScript regression")
def test_regression_attachment_property_error_preserves_message():
    """Regression: Cica reported that a failing MIME property must preserve content and other attachment fields."""
    import json

    from apple_mail_fast_mcp.mail_connector import (
        _attachment_metadata_clause,
        _wrap_as_json_script,
    )

    clause = _attachment_metadata_clause().replace("mail attachments of msg", "{1}")
    for source, value in [
        ("name of att", '"report.docx"'),
        ("MIME type of att", "missing value"),
        ("file size of att", "107471"),
        ("downloaded of att", "true"),
    ]:
        clause = clause.replace(source, value)
    script = _wrap_as_json_script(
        clause
        + '\nset resultData to {|content|:"forwarded chain", |attachments|:attList, |attachment_errors|:attachmentErrors, |attachments_complete|:attachmentsComplete}',
        timeout=10,
    )
    connector = AppleMailConnector(timeout=10)
    connector._applescript_socket = None
    result = json.loads(connector._run_applescript(script))
    assert result["content"] == "forwarded chain"
    assert result["attachments"][0]["name"] == "report.docx"
    assert result["attachments"][0]["size"] == 107471
    assert "mime_type" not in result["attachments"][0]
    assert result["attachment_errors"][0]["field"] == "mime_type"
    assert result["attachments_complete"] is False


def test_regression_batch_reports_missing_ids():
    """Regression: Cica partial retrieval must explicitly account for every missing requested ID."""
    from apple_mail_fast_mcp import server
    from apple_mail_fast_mcp.exceptions import MailMessageNotFoundError

    with patch.object(
        server.mail,
        "get_message",
        side_effect=[
            {"id": "42", "rfc_message_id": "found@example.test", "content": "body"},
            MailMessageNotFoundError("Can't get message: not found"),
        ],
    ):
        result = server.get_messages(["found@example.test", "43"])
    assert result["count"] == 1
    assert result["partial"] is True
    assert result["missing_message_ids"] == ["43"]
