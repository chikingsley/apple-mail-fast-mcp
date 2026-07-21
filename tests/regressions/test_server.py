"""
Unit tests for the FastMCP server layer in apple_mail_fast_mcp.server.

These tests exercise each @mcp.tool() function directly as a regular Python
callable with a mocked AppleMailConnector. They cover server-layer concerns
that the connector tests cannot: input validation, confirmation flows,
exception-to-error_type mapping, structured response shape, and
operation_logger calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastmcp.server.elicitation import (
    AcceptedElicitation,
    DeclinedElicitation,
)

from apple_mail_fast_mcp.server import (
    _elicit_confirmation,
    create_rule,
    delete_messages,
    delete_rule,
    get_messages,
    save_attachments,
    search_messages,
)


@pytest.fixture
def mock_mail() -> Any:
    with patch("apple_mail_fast_mcp.server.mail") as m:
        yield m


@pytest.fixture
def mock_logger() -> Any:
    with patch("apple_mail_fast_mcp.server.operation_logger") as m:
        yield m


@pytest.fixture
def mock_ctx_accept() -> MagicMock:
    """Mock MCP Context that accepts elicitation with an affirmative
    ``True`` (the confirm checkbox is set). Under the bool confirmation
    pattern (#282) only an explicit ``True`` proceeds.
    """
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=AcceptedElicitation(data=True))
    return ctx


@pytest.fixture
def mock_ctx_accept_false() -> MagicMock:
    """Mock MCP Context that accepts the elicitation but with ``False``
    (the user submitted the form without confirming). This must block,
    same as a decline (#282).
    """
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=AcceptedElicitation(data=False))
    return ctx


@pytest.fixture
def mock_ctx_decline() -> MagicMock:
    """Mock MCP Context that declines elicitation."""
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=DeclinedElicitation())
    return ctx


@pytest.fixture
def mock_ctx_raise() -> MagicMock:
    """Mock MCP Context whose elicit() raises (simulates a client that
    doesn't implement the elicitation capability — #226).
    """
    ctx = MagicMock()
    ctx.elicit = AsyncMock(side_effect=RuntimeError("not supported"))
    return ctx


# ---------------------------------------------------------------------------
# _elicit_confirmation gate-integrity tests (#226)
#
# Pre-#226 the helper silent-passed on `ctx is None` and on
# `ctx.elicit(...)` raising, which let every downstream gated tool
# (delete_*, send_now, rule mutations) be invoked without confirmation
# from any MCP client that didn't implement elicitation. These tests
# lock the fail-closed contract.
# ---------------------------------------------------------------------------


class TestElicitConfirmationFailsClosed:
    """Regression tests for #226: the gate must fail closed when it
    can't actually elicit user confirmation.
    """

    async def test_returns_cancelled_when_accepted_but_false(
        self,
        mock_ctx_accept_false: MagicMock,
    ) -> None:
        """#282: under the bool confirmation pattern, an elicitation that
        is *accepted* but carries ``False`` must still block — only an
        explicit affirmative proceeds. Fail-closed by construction.
        """
        result = await _elicit_confirmation(
            ctx=mock_ctx_accept_false,
            summary="Do X?",
            operation="op",
            params={"k": "v"},
        )
        assert result is not None
        assert result["success"] is False
        assert result["error_type"] == "cancelled"

    async def test_elicit_called_with_non_none_response_type(
        self,
        mock_ctx_accept: MagicMock,
    ) -> None:
        """#282: the gate must pass an explicit, non-``None`` response_type
        to ``ctx.elicit`` — passing ``None`` triggers FastMCPDeprecationWarning
        and renders a broken empty form in some clients. Unit tests mock
        ``ctx.elicit`` so they can't observe the warning directly; this
        asserts the call shape that avoids it.
        """
        await _elicit_confirmation(
            ctx=mock_ctx_accept,
            summary="Do X?",
            operation="op",
            params={"k": "v"},
        )
        mock_ctx_accept.elicit.assert_awaited_once()
        call = mock_ctx_accept.elicit.await_args
        response_type = call.kwargs.get("response_type")
        if response_type is None and len(call.args) > 1:
            response_type = call.args[1]
        assert response_type is bool


# ---------------------------------------------------------------------------
# atexit pool-close hook (#127)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 0. list_accounts
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 0b. list_rules
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 0c. Rule mutations: delete_rule, create_rule, update_rule
# ---------------------------------------------------------------------------


class TestDeleteRule:
    async def test_accepted_but_false_blocks_delete(
        self, mock_mail: MagicMock, mock_ctx_accept_false: MagicMock
    ) -> None:
        """#282: accepting the confirmation form with ``False`` (confirm
        unchecked) blocks the destructive op, end to end.
        """
        mock_mail.list_rules.return_value = [
            {"index": 1, "name": "Junk filter", "enabled": True},
        ]
        result = await delete_rule(rule_index=1, ctx=mock_ctx_accept_false)
        assert result["success"] is False
        assert result["error_type"] == "cancelled"
        mock_mail.delete_rule.assert_not_called()

    async def test_missing_ctx_blocks_delete_with_confirmation_required(
        self,
        mock_mail: MagicMock,
    ) -> None:
        """#226 integration test: a direct-call-site tool must surface
        the helper's confirmation_required error rather than completing
        the delete when no ctx is supplied.
        """
        mock_mail.list_rules.return_value = [
            {"index": 1, "name": "Junk filter", "enabled": True},
        ]
        result = await delete_rule(rule_index=1, ctx=None)
        assert result["success"] is False
        assert result["error_type"] == "confirmation_required"
        mock_mail.delete_rule.assert_not_called()


_COND = [{"field": "subject", "operator": "contains", "value": "X"}]

# (label, actions-dict) for each action that can move/disclose/delete mail.
_DANGEROUS_RULE_ACTION_CASES = [
    ("delete", {"delete": True}),
    ("forward_to", {"forward_to": ["a@example.com"]}),
    ("move_to", {"move_to": {"account": "Gmail", "mailbox": "X"}}),
    ("copy_to", {"copy_to": {"account": "Gmail", "mailbox": "X"}}),
]


class TestCreateRule:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("label,actions", _DANGEROUS_RULE_ACTION_CASES)
    async def test_dangerous_action_prompts_then_creates(
        self,
        label: str,
        actions: dict[str, Any],
        mock_mail: MagicMock,
        mock_ctx_accept: MagicMock,
    ) -> None:
        """Delete / forward_to / move_to / copy_to require confirmation (#222)."""
        mock_mail.create_rule.return_value = 3
        result = await create_rule(
            name=f"rule-{label}",
            conditions=_COND,
            actions=actions,
            ctx=mock_ctx_accept,
        )
        assert result["success"] is True
        mock_ctx_accept.elicit.assert_awaited_once()
        mock_mail.create_rule.assert_called_once()


# ---------------------------------------------------------------------------
# 1. list_mailboxes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2. search_messages
# ---------------------------------------------------------------------------


class TestSearchMessages:
    def test_advanced_filters_propagate_to_connector(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """New in #28: is_flagged, date_from, date_to, has_attachment must
        pass through to the connector and appear in the audit log.
        """
        mock_mail.search_messages.return_value = []

        result = search_messages(
            "Gmail",
            mailbox="INBOX",
            is_flagged=True,
            date_from="2026-04-01",
            date_to="2026-04-15",
            has_attachment=True,
            limit=25,
        )

        assert result["success"] is True
        mock_mail.search_messages.assert_called_once_with(
            account="Gmail",
            mailbox="INBOX",
            sender_contains=None,
            subject_contains=None,
            read_status=None,
            is_flagged=True,
            date_from="2026-04-01",
            date_to="2026-04-15",
            received_within_hours=None,
            has_attachment=True,
            limit=25,
            include_attachments=False,
            body_contains=None,
            text_contains=None,
            on_warning=ANY,
        )
        logged_params = mock_logger.log_operation.call_args.args[1]
        assert logged_params["filters"] == {
            "sender": None,
            "subject": None,
            "read_status": None,
            "is_flagged": True,
            "date_from": "2026-04-01",
            "date_to": "2026-04-15",
            "has_attachment": True,
            "body_contains": None,
            "text_contains": None,
        }

    # ---- received_within_hours (#230) ----------------------------------

    # ---- source="selected" (folded-in get_selected_messages, #131) -------

    # ---- source=None default (search the mailbox) -----------------------

    def test_no_source_with_account_unchanged(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """Regression: existing positional callers still work."""
        mock_mail.search_messages.return_value = [{"id": "1"}]

        result = search_messages("Gmail")

        assert result["success"] is True
        assert result["account"] == "Gmail"
        mock_mail.search_messages.assert_called_once()
        mock_mail.get_selected_messages.assert_not_called()
        mock_mail.get_message.assert_not_called()

    # ---- source=["SELECTED"] sentinel -----------------------------------

    # No validation_error even though account is None.

    def test_source_selected_post_filters_by_other_params(self, mock_mail: MagicMock) -> None:
        """Filters compose with source=[ids] (unlike pre-#144 source='selected')."""
        mock_mail.get_selected_messages.return_value = [
            {
                "id": "1",
                "subject": "alpha",
                "sender": "alice@example.com",
                "date_received": "2026-04-01",
                "read_status": True,
                "flagged": False,
            },
            {
                "id": "2",
                "subject": "beta",
                "sender": "bob@example.com",
                "date_received": "2026-04-02",
                "read_status": False,
                "flagged": False,
            },
        ]

        result = search_messages(source=["SELECTED"], read_status=False)

        assert [m["id"] for m in result["messages"]] == ["2"]

    # ---- source=[explicit ids] -----------------------------------------

    # ---- include_attachments (#133 + #142) -------------------------------

    # ---- body_contains / text_contains (#145) ---------------------------

    # ---- warnings field (#146) ------------------------------------------


# ---------------------------------------------------------------------------
# 3. get_messages
# ---------------------------------------------------------------------------


class TestGetMessages:
    def test_imap_hint_params_pass_through_per_id(self, mock_mail: MagicMock) -> None:
        """Issue #72: account+mailbox activate the IMAP fast path."""
        mock_mail.get_message.return_value = {"id": "abc@x", "subject": "Hi"}

        result = get_messages(["abc@x"], account="iCloud", mailbox="INBOX", headers_only=True)

        assert result["success"] is True
        mock_mail.get_message.assert_called_once_with(
            "abc@x",
            include_content=True,
            headers_only=True,
            account="iCloud",
            mailbox="INBOX",
            include_attachments=True,
            body_format="text",
        )

    # ---- include_attachments (#133 + #142) -------------------------------


# ---------------------------------------------------------------------------
# 5. update_message — patch tool replacing mark_as_read + move_messages + flag_message (#135)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 7b. get_thread
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 8b. get_attachment_content (#250)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 8. save_attachments
# ---------------------------------------------------------------------------


class TestSaveAttachments:
    def test_surfaces_rejected_attachments(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        """Byte-cap rejections (#236) are passed through to the tool payload."""
        rejected = [{"name": "huge.bin", "size": 9_999_999_999, "reason": "per_attachment_cap"}]
        mock_mail.save_attachments.return_value = {"saved": 1, "rejected": rejected}

        result = save_attachments("1", str(tmp_path))

        assert result["success"] is True
        assert result["saved"] == 1
        assert result["rejected"] == rejected


# ---------------------------------------------------------------------------
# 11. create_mailbox
# ---------------------------------------------------------------------------


class TestUpdateMailboxTool:
    """Tests for the update_mailbox MCP tool (rename only — #102)."""

    def test_move_only_success(self, mock_mail: MagicMock, mock_logger: MagicMock) -> None:
        """#163: new_parent set, new_name None — pure move via IMAP."""
        from apple_mail_fast_mcp.server import update_mailbox

        mock_mail.update_mailbox.return_value = True
        result = update_mailbox(account="Gmail", name="A/B", new_parent="C")
        assert result["success"] is True
        mock_mail.update_mailbox.assert_called_once_with(
            account="Gmail",
            name="A/B",
            new_name=None,
            new_parent="C",
        )

    def test_gmail_system_label_maps_to_typed_error(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """#164: source path under ``[Gmail]/`` returns
        ``error_type: "unsupported_gmail_system_label"``.
        """
        from apple_mail_fast_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        from apple_mail_fast_mcp.server import update_mailbox

        mock_mail.update_mailbox.side_effect = MailUnsupportedGmailSystemLabelError(
            "cannot update Gmail system label '[Gmail]/Drafts'"
        )
        result = update_mailbox(
            account="Gmail",
            name="[Gmail]/Drafts",
            new_name="MyDrafts",
        )
        assert result["success"] is False
        assert result["error_type"] == "unsupported_gmail_system_label"
        assert "Gmail" in result["error"]

    def test_gmail_system_label_destination_maps_to_typed_error(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """#164: destination under ``[Gmail]/`` (via new_parent) maps too."""
        from apple_mail_fast_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        from apple_mail_fast_mcp.server import update_mailbox

        mock_mail.update_mailbox.side_effect = MailUnsupportedGmailSystemLabelError(
            "destination would land in Gmail's system-label namespace"
        )
        result = update_mailbox(
            account="Gmail",
            name="Archive",
            new_parent="[Gmail]/Backup",
        )
        assert result["error_type"] == "unsupported_gmail_system_label"


class TestDeleteMailboxTool:
    """Tests for the delete_mailbox MCP tool (#162, IMAP-dispatched)."""

    @pytest.mark.asyncio
    async def test_gmail_system_label_maps_to_typed_error(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_ctx_accept: MagicMock,
    ) -> None:
        """#164: deleting a ``[Gmail]/`` path returns
        ``error_type: "unsupported_gmail_system_label"``.
        """
        from apple_mail_fast_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        from apple_mail_fast_mcp.server import delete_mailbox

        mock_mail.delete_mailbox.side_effect = MailUnsupportedGmailSystemLabelError(
            "cannot delete Gmail system label '[Gmail]/Trash'"
        )
        result = await delete_mailbox(
            account="Gmail",
            name="[Gmail]/Trash",
            ctx=mock_ctx_accept,
        )
        assert result["success"] is False
        assert result["error_type"] == "unsupported_gmail_system_label"
        assert "Gmail" in result["error"]


# ---------------------------------------------------------------------------
# 12. delete_messages
# ---------------------------------------------------------------------------


class TestDeleteMessages:
    @pytest.mark.asyncio
    async def test_permanent_true_threads_through_to_connector(
        self, mock_mail: MagicMock, mock_ctx_accept: MagicMock
    ) -> None:
        """Issue #111: the connector emits a DeprecationWarning when
        permanent=True; the server's job is just to forward the flag
        unchanged so the warning fires from the user's call frame.
        """
        mock_mail.delete_messages.return_value = 1
        result = await delete_messages(["1"], permanent=True, ctx=mock_ctx_accept)
        assert result["success"] is True
        # Server still echoes the (now-meaningless) flag in its response
        # for backwards compatibility with existing callers.
        assert result["permanent"] is True
        mock_mail.delete_messages.assert_called_once_with(
            message_ids=["1"],
            permanent=True,
            skip_bulk_check=False,
            account=None,
            source_mailbox=None,
        )


# ---------------------------------------------------------------------------
# Rate limiting integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def tight_limits() -> Any:
    """Monkeypatch TIER_LIMITS down to 2 calls/60s so we can trip them easily."""
    import apple_mail_fast_mcp.security as sec

    original = sec.TIER_LIMITS.copy()
    sec.TIER_LIMITS.update(
        {
            "cheap_reads": (2, 60.0),
            "expensive_ops": (2, 60.0),
            "sends": (2, 60.0),
        }
    )
    yield
    sec.TIER_LIMITS.update(original)


# ---------------------------------------------------------------------------
# Email templates (#30)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_templates(tmp_path: Any, monkeypatch: Any) -> Any:
    """Redirect template storage to a tmp dir for the duration of the test."""
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    return tmp_path / "templates"


# ---------------------------------------------------------------------------
# create_draft / update_draft / delete_draft (drafts lifecycle, #134)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_drafts(monkeypatch: Any, tmp_path: Any) -> Any:
    """Point ~/.apple_mail_mcp/drafts/ at a tmp dir for the test."""
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    return tmp_path


class TestCreateDraftTool:
    @pytest.fixture(autouse=True)
    def stub_security(self, monkeypatch: Any) -> None:
        # Default: safety + rate-limit pass; recipient-validation passes.
        monkeypatch.setattr(
            "apple_mail_fast_mcp.server.check_test_mode_safety",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "apple_mail_fast_mcp.server.check_rate_limit",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "apple_mail_fast_mcp.server.validate_send_operation",
            lambda *a, **kw: (True, None),
        )

    @pytest.mark.asyncio
    async def test_body_html_threads_to_connector(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        """#251: body_html is passed through to the connector for a fresh
        save-as-draft.
        """
        from apple_mail_fast_mcp.server import create_draft

        mock_mail.create_draft.return_value = {"draft_id": "161099", "sent_message_id": ""}
        result = await create_draft(
            to=["a@example.com"],
            subject="hi",
            body="plain",
            body_html="<p>rich</p>",
        )
        assert result["success"] is True
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["body_html"] == "<p>rich</p>"

    @pytest.mark.asyncio
    async def test_body_html_with_send_now_rejected(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        """#251: no HTML send path — body_html + send_now is a
        validation_error and never reaches the connector.
        """
        from apple_mail_fast_mcp.server import create_draft

        result = await create_draft(
            to=["a@example.com"],
            subject="hi",
            body_html="<p>rich</p>",
            send_now=True,
        )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.create_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_body_html_with_reply_to_rejected(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        """#251: HTML reply/forward is out of scope — rejected as a
        validation_error before the connector.
        """
        from apple_mail_fast_mcp.server import create_draft

        result = await create_draft(
            reply_to="160989",
            body_html="<p>rich</p>",
        )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.create_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_body_html_unavailable_maps_to_html_requires_imap(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        """#251: the connector's fail-loud exception surfaces as
        error_type 'html_requires_imap'.
        """
        from apple_mail_fast_mcp.exceptions import MailDraftHtmlUnavailableError
        from apple_mail_fast_mcp.server import create_draft

        mock_mail.create_draft.side_effect = MailDraftHtmlUnavailableError(
            "HTML drafts require IMAP credentials"
        )
        result = await create_draft(
            to=["a@example.com"],
            subject="hi",
            body_html="<p>rich</p>",
        )
        assert result["success"] is False
        assert result["error_type"] == "html_requires_imap"

    @pytest.mark.asyncio
    async def test_connector_safety_error_maps_to_safety_violation(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_ctx_accept: MagicMock,
    ) -> None:
        """#322/#175: the SMTP send path's transport-boundary guard raises
        MailSafetyError for a derived non-reserved recipient the server-layer
        gate never saw; the server surfaces it as a safety_violation, not a
        generic ``unknown`` error.
        """
        from apple_mail_fast_mcp.exceptions import MailSafetyError
        from apple_mail_fast_mcp.server import create_draft

        mock_mail.create_draft.side_effect = MailSafetyError(
            "Test mode: recipients must use RFC 2606 reserved domains"
        )
        result = await create_draft(
            to=["ok@example.com"],
            subject="hi",
            body="x",
            send_now=True,
            ctx=mock_ctx_accept,
        )
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"

    @pytest.mark.asyncio
    async def test_send_now_missing_ctx_blocks_with_confirmation_required(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        """#226 integration test: an indirect call site (through
        _run_send_now_gates) must surface the helper's
        confirmation_required error rather than completing the send
        when no ctx is supplied.
        """
        from apple_mail_fast_mcp.server import create_draft

        result = await create_draft(
            to=["a@example.com"],
            subject="hi",
            body="x",
            send_now=True,
            ctx=None,
        )
        assert result["success"] is False
        assert result["error_type"] == "confirmation_required"
        mock_mail.create_draft.assert_not_called()


class TestUpdateDraftTool:
    @pytest.fixture(autouse=True)
    def stub_security(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            "apple_mail_fast_mcp.server.check_test_mode_safety",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "apple_mail_fast_mcp.server.check_rate_limit",
            lambda *a, **kw: None,
        )

    @pytest.mark.asyncio
    async def test_update_body_html_threads_for_fresh_seed(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        """#251: body_html threads to the recreated draft when the seed is a
        fresh draft.
        """
        from apple_mail_fast_mcp.server import update_draft

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991",
            "to": ["alice@example.com"],
            "cc": [],
            "bcc": [],
            "subject": "hi",
            "body": "old",
            "in_reply_to": "",
            "references": "",
            "attachment_names": [],
        }
        mock_mail.delete_draft.return_value = True
        mock_mail.create_draft.return_value = {"draft_id": "161000", "sent_message_id": ""}
        result = await update_draft(draft_id="160991", body_html="<p>rich</p>")
        assert result["success"] is True
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["seed"] == "new"
        assert kwargs["body_html"] == "<p>rich</p>"

    @pytest.mark.asyncio
    async def test_update_body_html_rejected_for_reply_seed(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        """#251: HTML reply/forward drafts are out of scope — reject and
        leave the existing draft untouched (no delete/recreate).
        """
        from apple_mail_fast_mcp.drafts import DraftStateStore, SeedRecord
        from apple_mail_fast_mcp.server import update_draft

        store = DraftStateStore()
        store.set_seed(
            "160991",
            SeedRecord(seed_kind="reply", seed_id="160000", reply_all=False),
        )
        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991",
            "to": ["alice@example.com"],
            "cc": [],
            "bcc": [],
            "subject": "Re: hi",
            "body": "old",
            "in_reply_to": "<orig@x>",
            "references": "<orig@x>",
            "attachment_names": [],
        }
        result = await update_draft(draft_id="160991", body_html="<p>rich</p>")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.delete_draft.assert_not_called()
        mock_mail.create_draft.assert_not_called()


class TestDraftToolErrorPaths:
    """Coverage for the error-handling branches of all three draft tools.

    These are tedious but each one corresponds to a real
    response-shape contract that callers depend on for branching
    (account_not_found vs file_not_found vs unknown, etc.).
    """

    @pytest.fixture(autouse=True)
    def stub_security(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            "apple_mail_fast_mcp.server.check_test_mode_safety",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "apple_mail_fast_mcp.server.check_rate_limit",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "apple_mail_fast_mcp.server.validate_send_operation",
            lambda *a, **kw: (True, None),
        )

    # ------------------------------------------------------------------
    # create_draft
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_create_draft_send_now_implicit_reply_blocked_in_test_mode(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        monkeypatch: Any,
        mock_ctx_accept: MagicMock,
    ) -> None:
        """#175: implicit-reply send_now (no explicit to/cc/bcc) in test
        mode is now blocked. Without the fix, the server would skip
        check_test_mode_safety entirely (recipients list was empty);
        the new server-side guard removal + security-side empty-recipients
        reject combine to close the gap.
        """
        from apple_mail_fast_mcp.security import (
            check_test_mode_safety as real_check,
        )
        from apple_mail_fast_mcp.server import create_draft

        # Restore the real check_test_mode_safety (the class-level
        # autouse `stub_security` fixture replaced it with a no-op).
        monkeypatch.setattr("apple_mail_fast_mcp.server.check_test_mode_safety", real_check)
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        # No to / cc / bcc — Mail.app would derive from reply_to at
        # send time, potentially targeting a real address.
        result = await create_draft(
            reply_to="some-msg-id",
            body="x",
            send_now=True,
            ctx=mock_ctx_accept,
        )
        assert result["error_type"] == "safety_violation"
        assert "explicit recipients" in result["error"]
        mock_mail.create_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_draft_send_now_implicit_reply_blocked_in_test_mode(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        monkeypatch: Any,
        mock_ctx_accept: MagicMock,
    ) -> None:
        """#175: same gap on update_draft's send path — closed by the
        same fix.
        """
        from apple_mail_fast_mcp.security import (
            check_test_mode_safety as real_check,
        )
        from apple_mail_fast_mcp.server import update_draft

        # Restore the real check_test_mode_safety (the class-level
        # autouse `stub_security` fixture replaced it with a no-op).
        monkeypatch.setattr("apple_mail_fast_mcp.server.check_test_mode_safety", real_check)
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        # No to / cc / bcc and the existing draft has none either —
        # implicit-reply send path.
        mock_mail.get_draft_state.return_value = {
            "id": "draft-1",
            "to": [],
            "cc": [],
            "bcc": [],
            "subject": "Re: hi",
            "body": "stub",
            "attachments": [],
            "seed_kind": "reply",
        }
        result = await update_draft(
            draft_id="draft-1",
            body="x",
            send_now=True,
            ctx=mock_ctx_accept,
        )
        assert result["error_type"] == "safety_violation"
        assert "explicit recipients" in result["error"]
        mock_mail.update_draft.assert_not_called()

    # ------------------------------------------------------------------
    # update_draft
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # delete_draft
    # ------------------------------------------------------------------
