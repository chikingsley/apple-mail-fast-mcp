"""Regressions from the September shared communications audit."""

from unittest.mock import patch

import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.client.elicitation import ElicitResult
from fastmcp.server import create_proxy
from mcp.types import InputRequiredResult

from apple_mail_mcp.code_mode import communications_code_mode
from apple_mail_mcp.mail_connector import AppleMailConnector
from apple_mail_mcp.server import _elicit_confirmation


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
@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.parametrize("approved", [False, True])
async def test_regression_code_mode_preserves_confirmation_and_catalog(mode, approved):
    """Regression: smaller discovery must retain schemas and a declined mutation gate."""
    app = FastMCP("regression", transforms=[communications_code_mode()])
    writes = []

    @app.tool
    async def guarded(ctx: Context) -> dict[str, object] | InputRequiredResult:
        error = await _elicit_confirmation(ctx, "Confirm simulated write", "delete_messages", {})
        if error:
            return error
        writes.append(True)
        return {"success": True}

    async def decline(*args):  # ruff: ignore[unused-async] - async callback required by MCP
        return (
            ElicitResult(action="accept", content={"confirm": True, "value": True})
            if approved
            else ElicitResult(action="decline")
        )

    async with Client(create_proxy(app), elicitation_handler=decline, mode=mode) as client:
        assert {t.name for t in await client.list_tools()} == {"search", "get_schema", "execute"}
        schema = await client.call_tool("get_schema", {"tools": ["guarded"]})
        assert "guarded" in schema.content[0].text
        result = await client.call_tool("execute", {"tool_name": "guarded", "arguments": {}})
        assert result.data["success"] is approved
        if not approved:
            assert result.data["error_type"] == "cancelled"
    assert writes == ([True] if approved else [])


@pytest.mark.allow_real_io
@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="AppleScript regression")
def test_regression_attachment_property_error_preserves_message():
    """Regression: Cica reported that a failing MIME property must preserve content and other attachment fields."""
    import json

    from apple_mail_mcp.mail_connector import (
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
    from apple_mail_mcp import server
    from apple_mail_mcp.exceptions import MailMessageNotFoundError

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


def test_regression_beeper_watchdog_does_not_reopen_running_app():
    """Regression: checking Beeper instead of Beeper Desktop stole focus every minute."""
    import plistlib
    import runpy
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "deploy/keep-beeper-running.py"
    with (
        patch.object(
            Path,
            "read_bytes",
            return_value=plistlib.dumps({"CFBundleExecutable": "Beeper Desktop"}),
        ),
        patch(
            "subprocess.check_output",
            return_value="/Applications/Beeper Desktop.app/Contents/MacOS/Beeper Desktop\n",
        ),
        patch("subprocess.run") as launch,
    ):
        runpy.run_path(str(script))
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_regression_mutating_code_is_rejected_before_execution():
    """Regression: confirmation retries must never replay an earlier write in a batch."""
    app = FastMCP("no-replay", transforms=[communications_code_mode()])
    writes = []

    @app.tool(annotations={"readOnlyHint": False})
    def mutate():
        writes.append(True)
        return {"success": True}

    async with Client(app) as client:
        result = await client.call_tool(
            "execute", {"code": 'return await call_tool("mutate", {})'}, raise_on_error=False
        )
        assert result.is_error
    assert writes == []
