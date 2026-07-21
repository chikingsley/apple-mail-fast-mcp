"""Unit tests for mail connector."""

import contextlib
import smtplib
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_fast_mcp.exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailDraftNotFoundError,
    MailKeychainEntryNotFoundError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
    MailSafetyError,
)
from apple_mail_fast_mcp.mail_connector import (
    AppleMailConnector,
)


class TestAppleMailConnector:
    """Tests for AppleMailConnector."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @pytest.mark.allow_real_io
    def test_run_applescript_uses_resident_native_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Regression: AppleScript requests use the configured resident helper."""
        socket_path = tmp_path / "helper.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.listen(1)
        received: list[str] = []

        def serve_once() -> None:
            with listener, listener.accept()[0] as connection:
                request_size = struct.unpack("!I", connection.recv(4))[0]
                received.append(connection.recv(request_size).decode("utf-8"))
                payload = b"result"
                connection.sendall(struct.pack("!BI", 0, len(payload)) + payload)

        server = threading.Thread(target=serve_once)
        server.start()
        monkeypatch.setenv("APPLE_MAIL_MCP_APPLESCRIPT_SOCKET", str(socket_path))

        connector = AppleMailConnector(timeout=30)

        assert connector._run_applescript("test script") == "result"
        server.join(timeout=5)
        assert not server.is_alive()
        assert received == ["test script"]

    def test_configured_native_helper_socket_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Regression: a configured but invalid helper socket must fail closed."""
        missing = tmp_path / "missing-helper.sock"
        monkeypatch.setenv("APPLE_MAIL_MCP_APPLESCRIPT_SOCKET", str(missing))
        connector = AppleMailConnector(timeout=30)

        with pytest.raises(MailAppleScriptError, match="must name a Unix socket"):
            connector._run_applescript("test script")

    @patch("subprocess.run")
    def test_run_applescript_curly_apostrophe_still_maps_to_typed_error(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Real macOS stderr uses curly apostrophes — must still dispatch typed errors.

        Regression guard for a bug where `Can\u2019t get account "X"` (curly
        apostrophe, as emitted by Mail.app) bypassed the typed-exception
        mapping and surfaced as a generic MailAppleScriptError, defeating the
        server-layer not-found routing.
        """
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr='Can\u2019t get account "NonExistent"',
        )
        with pytest.raises(MailAccountNotFoundError):
            connector._run_applescript("test script")

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr='Can\u2019t get mailbox "NonExistent"',
        )
        with pytest.raises(MailMailboxNotFoundError):
            connector._run_applescript("test script")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_script_includes_type_and_enabled(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Generated AppleScript must extract account_type (as text), enabled,
        and the full_name (#158) used for the Display Name <email> sender.
        """
        mock_run.return_value = "[]"
        connector.list_accounts()
        script = mock_run.call_args[0][0]
        assert "|account_type|:((account type of acc) as text)" in script
        assert "|enabled|:(enabled of acc)" in script
        assert "|id|:(id of acc as text)" in script
        # #158: full_name read with missing-value coercion.
        assert "full name of acc" in script
        assert "|full_name|:accFullName" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_script_emits_one_based_index(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Per #63, list_rules' return shape must include a 1-based index
        matching Mail.app's AppleScript ``rule N`` reference.
        """
        mock_run.return_value = "[]"
        connector.list_rules()
        script = mock_run.call_args[0][0]
        # Iterates by index, not by reference, so the loop variable is the index.
        assert "repeat with i from 1 to ruleCount" in script
        assert "|index|:i" in script

    # --- set_rule_enabled ------------------------------------------------

    # --- delete_rule -----------------------------------------------------

    # --- _check_supported_actions ---------------------------------------

    # --- create_rule -----------------------------------------------------

    # --- update_rule -----------------------------------------------------

    @staticmethod
    def _supported_actions_clean_response() -> str:
        """Mock _check_supported_actions JSON for a rule with no
        unsupported actions set.
        """
        return (
            '{"run_script_set":false,"play_sound_set":false,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":false,"highlight_text":false,'
            '"color_message":"none"}'
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_script_quotes_name_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The AppleScript must use |name| (quoted) so NSJSONSerialization keeps it.

        Unquoted `name:` in the record literal causes the key to be silently
        dropped during ASObjC -> NSDictionary conversion because `name` collides
        with NSObject's `name` property. Regression guard for real Mail.app bug.
        """
        mock_run.return_value = "[]"
        connector.list_accounts()
        script = mock_run.call_args[0][0]
        assert "|name|:(name of acc)" in script
        assert "{name:(name of acc)" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_script_quotes_name_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The AppleScript must use |name| so NSJSONSerialization preserves it.

        Post-#247: the record construction now lives in the
        `collectMailboxesWithPaths` handler (which also emits the new
        `path` field).
        """
        mock_run.return_value = "[]"
        connector.list_mailboxes("Gmail")
        script = mock_run.call_args[0][0]
        # The handler emits records with |name|, |path|, and |unread_count|.
        assert "|name|:mbName" in script
        assert "|path|:mbPath" in script
        assert "|unread_count|:mbUnread" in script
        # Caller invokes the handler with the resolved account.
        assert "my collectMailboxesWithPaths(accountRef)" in script

    # --- _resolve_imap_config --------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_prefers_user_name_for_login(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Primary path: user_name (Mail.app's IMAP LOGIN credential) wins
        over email_addresses[0] (the SMTP From list). They overlap for
        most accounts but diverge for iCloud accounts on a custom-domain
        Apple ID — there email_addresses[0] is an SMTP-only From alias
        the IMAP server rejects with AUTHENTICATIONFAILED. (#201)
        """
        mock_run.return_value = (
            '{"host":"imap.mail.me.com",'
            '"port":993,'
            '"user_name":"apple-id@example.com",'
            '"email_addresses":["from-alias@example.com","apple-id@example.com"]}'
        )
        result = connector._resolve_imap_config("iCloud")
        assert result == ("imap.mail.me.com", 993, "apple-id@example.com")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_falls_back_to_email_addresses_when_user_name_empty(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Fallback path: empty user_name → use email_addresses[0]. (#201)"""
        mock_run.return_value = (
            '{"host":"imap.gmail.com","port":993,"user_name":"","email_addresses":["me@gmail.com"]}'
        )
        result = connector._resolve_imap_config("Gmail")
        assert result == ("imap.gmail.com", 993, "me@gmail.com")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_icloud_third_party_apple_id_uses_icloud_alias(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#299: iCloud account whose Apple ID (`user name`) is a third-party
        email (gmail) — the *.mail.me.com server rejects that, so resolve the
        login to the account's @icloud.com address instead.
        """
        mock_run.return_value = (
            '{"host":"p42-imap.mail.me.com",'
            '"port":993,'
            '"user_name":"someone@gmail.com",'
            '"email_addresses":["someone@icloud.com","someone@me.com"]}'
        )
        result = connector._resolve_imap_config("iCloud")
        assert result == ("p42-imap.mail.me.com", 993, "someone@icloud.com")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_icloud_falls_back_to_me_com_alias(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#299: when only an @me.com Apple-hosted alias is present, use it."""
        mock_run.return_value = (
            '{"host":"p42-imap.mail.me.com",'
            '"port":993,'
            '"user_name":"someone@gmail.com",'
            '"email_addresses":["someone@me.com"]}'
        )
        result = connector._resolve_imap_config("iCloud")
        assert result == ("p42-imap.mail.me.com", 993, "someone@me.com")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_icloud_apple_user_name_unchanged(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#299: when `user name` is already Apple-hosted, keep it (the
        normal iCloud case) — don't second-guess it.
        """
        mock_run.return_value = (
            '{"host":"imap.mail.me.com",'
            '"port":993,'
            '"user_name":"primary@icloud.com",'
            '"email_addresses":["alias@icloud.com","primary@icloud.com"]}'
        )
        result = connector._resolve_imap_config("iCloud")
        assert result == ("imap.mail.me.com", 993, "primary@icloud.com")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_icloud_no_apple_alias_keeps_user_name(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#299/#201: me.com host + non-Apple `user name` + NO Apple-hosted
        alias (the pure custom-domain shape) → fall back to `user name`,
        preserving #201.
        """
        mock_run.return_value = (
            '{"host":"imap.mail.me.com",'
            '"port":993,'
            '"user_name":"apple-id@example.com",'
            '"email_addresses":["from-alias@example.com","apple-id@example.com"]}'
        )
        result = connector._resolve_imap_config("iCloud")
        assert result == ("imap.mail.me.com", 993, "apple-id@example.com")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_login_override_wins(
        self,
        mock_run: MagicMock,
        connector: AppleMailConnector,
        tmp_path,
        monkeypatch,
    ) -> None:
        """#341: a persisted login override (setup-imap --email) wins over the
        Mail.app-derived login — the fix for an iCloud account with a
        third-party Apple ID and an empty `email addresses` list, where #299's
        apple-alias rule has nothing to choose from.
        """
        from apple_mail_fast_mcp import imap_overrides

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        imap_overrides.set_login_override("iCloud", "s.morgan@icloud.com")
        # The unresolvable shape: me.com host, gmail user_name, no aliases.
        mock_run.return_value = (
            '{"host":"p42-imap.mail.me.com",'
            '"port":993,'
            '"user_name":"s.morgan@gmail.com",'
            '"email_addresses":[]}'
        )
        result = connector._resolve_imap_config("iCloud")
        assert result == ("p42-imap.mail.me.com", 993, "s.morgan@icloud.com")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_port_override_wins(
        self,
        mock_run: MagicMock,
        connector: AppleMailConnector,
        tmp_path,
        monkeypatch,
    ) -> None:
        """#405: a persisted port override (setup-imap --port) wins over the
        port Mail.app reports — the fix for accounts whose Mail.app scriptably
        misreports the IMAP port (e.g. Zimbra returning 143 for a 993/implicit-
        TLS account).
        """
        from apple_mail_fast_mcp import imap_overrides

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        imap_overrides.set_server_override("Work", host=None, port=993)
        mock_run.return_value = (
            '{"host":"imap.corp.example",'
            '"port":143,'  # Mail.app misreports the port
            '"user_name":"me@corp.example",'
            '"email_addresses":["me@corp.example"]}'
        )
        result = connector._resolve_imap_config("Work")
        assert result == ("imap.corp.example", 993, "me@corp.example")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_host_override_wins(
        self,
        mock_run: MagicMock,
        connector: AppleMailConnector,
        tmp_path,
        monkeypatch,
    ) -> None:
        """#405: a persisted host override (setup-imap --host) wins over the
        server name Mail.app reports.
        """
        from apple_mail_fast_mcp import imap_overrides

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        imap_overrides.set_server_override("Work", host="imap.real.example", port=None)
        mock_run.return_value = (
            '{"host":"imap.wrong.example",'
            '"port":993,'
            '"user_name":"me@corp.example",'
            '"email_addresses":["me@corp.example"]}'
        )
        host, _port, _email = connector._resolve_imap_config("Work")
        assert host == "imap.real.example"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_host_override_applied_before_icloud_alias(
        self,
        mock_run: MagicMock,
        connector: AppleMailConnector,
        tmp_path,
        monkeypatch,
    ) -> None:
        """#405 + #299: the host override is applied *before* the iCloud
        apple-alias logic, so overriding an account onto a non-iCloud host
        stops that logic from rewriting the login. Mail.app reports an me.com
        host + third-party user_name (which alone would swap in the @icloud
        alias); with the host overridden to a plain host, the login stays the
        reported user_name.
        """
        from apple_mail_fast_mcp import imap_overrides

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        imap_overrides.set_server_override("iCloud", host="imap.corp.example", port=None)
        mock_run.return_value = (
            '{"host":"p42-imap.mail.me.com",'
            '"port":993,'
            '"user_name":"me@gmail.com",'
            '"email_addresses":["me@gmail.com","alias@icloud.com"]}'
        )
        host, _port, email = connector._resolve_imap_config("iCloud")
        assert host == "imap.corp.example"
        assert email == "me@gmail.com"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_non_icloud_host_not_overridden(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#299: the Apple-alias preference is scoped to iCloud IMAP hosts.
        A non-me.com host keeps `user name` even if an icloud address happens
        to be in the From list.
        """
        mock_run.return_value = (
            '{"host":"imap.gmail.com",'
            '"port":993,'
            '"user_name":"me@gmail.com",'
            '"email_addresses":["me@gmail.com","old@icloud.com"]}'
        )
        result = connector._resolve_imap_config("Gmail")
        assert result == ("imap.gmail.com", 993, "me@gmail.com")

    # --- _imap_failures state + _log_imap_fallback -----------------------

    # --- Issue #118: per-account circuit breaker --------------------------

    def test_breaker_does_not_open_for_message_not_found(
        self, connector: AppleMailConnector
    ) -> None:
        """#350: a reply/forward seed not in the guessed seed_mailbox raises
        MailMessageNotFoundError — a benign folder-guess miss (AppleScript
        resolves across all folders), not a credential/network failure. It
        must NOT open the breaker, or a normal reply-to-filed-mail would
        poison every IMAP read for the account for 30s.
        """
        connector._log_imap_fallback("iCloud", MailMessageNotFoundError("not in INBOX"))
        assert "iCloud" not in connector._imap_failure_until
        assert connector._imap_breaker_open("iCloud") is False

    # --- _imap_search helper ---------------------------------------------

    # --- _imap_get_thread helper -----------------------------------------

    # --- get_thread delegation -------------------------------------------

    # --- _imap_move_messages helper (#149) -------------------------------

    # --- update_message move delegation (#149) ---------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_skips_imap_for_combined_patch(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Move + read_status combined: stays on AppleScript until
        sibling issues #150 / #151 / #152 land.
        """
        mock_run_as.return_value = "2"
        connector.update_message(
            ["a@x", "b@x"],
            destination_mailbox="Archive",
            read_status=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    # --- _imap_delete_messages helper (#150) -----------------------------

    # --- delete_messages delegation (#150) -------------------------------

    # --- _imap_set_read_status helper (#151) -----------------------------

    # --- update_message read-only delegation (#151) ----------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_skips_imap_for_combined_read_and_move(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Read + move in one call: stays on AppleScript pending #152."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            read_status=True,
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_skips_imap_for_combined_read_and_flag(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Read + flag in one call: stays on AppleScript pending #152."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            read_status=True,
            flagged=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    # --- _imap_set_flagged_status helper (#152) --------------------------

    # --- update_message flag-only delegation (#152) ----------------------

    # --- search_messages delegation --------------------------------------

    # Note: validates the Python-side JSON parse. Real end-to-end correctness
    # (AppleScript actually emitting valid JSON when the data contains '|')
    # is proven by integration tests.
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_handles_pipe_in_subject(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Subject containing '|' must not break parsing (the bug this refactor fixes)."""
        mock_run.return_value = (
            '[{"id":"abc","subject":"Q3 Report | Draft",'
            '"sender":"boss@example.com","date_received":"Wed Feb 5 2025",'
            '"read_status":true}]'
        )
        result = connector._search_messages_applescript("Gmail", "INBOX")
        assert len(result) == 1
        assert result[0]["subject"] == "Q3 Report | Draft"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_propagates_account_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """If _run_applescript raises MailAccountNotFoundError, search_messages must not swallow it.

        Regression guard: a previous version wrapped the tell-block in try/on error,
        which downgraded MailAccountNotFoundError to MailAppleScriptError.
        """
        mock_run.side_effect = MailAccountNotFoundError('Can\'t get account "NoSuch".')
        with pytest.raises(MailAccountNotFoundError):
            connector._search_messages_applescript("NoSuch", "INBOX")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_propagates_mailbox_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Similar regression guard for MailMailboxNotFoundError."""
        mock_run.side_effect = MailMailboxNotFoundError('Can\'t get mailbox "NoSuch".')
        with pytest.raises(MailMailboxNotFoundError):
            connector._search_messages_applescript("Gmail", "NoSuch")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_with_filters(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test message search with filters.

        Per #32, filters are now applied as per-message IF expressions
        instead of a `whose` clause — `whose` is unusably slow against
        large IMAP mailboxes (>120s timeout on 8000+ messages). The
        pattern iterates messages newest-first (Mail.app exposes
        `item 1 of msgs` as the newest, per #242) and checks each filter
        against the message; the script short-circuits when matchCount
        reaches the limit.
        """
        mock_run.return_value = "[]"

        connector._search_messages_applescript(
            "Gmail",
            "INBOX",
            sender_contains="john@example.com",
            subject_contains="meeting",
            read_status=False,
            limit=10,
        )

        # Filter conditions appear as IF clauses, not in a `whose` clause.
        call_args = mock_run.call_args[0][0]
        assert (
            'if (sender of msg) does not contain "john@example.com" '
            "then set includeThis to false" in call_args
        )
        assert (
            'if (subject of msg) does not contain "meeting" '
            "then set includeThis to false" in call_args
        )
        assert "if (read status of msg) is not false then set includeThis to false" in call_args
        # Limit is enforced by accumulating matches and exiting the repeat
        # when matchCount reaches the bound.
        assert "if matchCount >= 10 then exit repeat" in call_args
        # Newest-first iteration: item 1 of msgs is the newest (per #242).
        assert "repeat with i from 1 to total" in call_args
        # Guard against regression to the old reverse-index pattern.
        assert "repeat with i from total to 1 by -1" not in call_args
        # No `whose` clause anywhere.
        assert "whose" not in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_without_filters_omits_whose_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """AppleScript rejects `whose true` — no-filter searches must drop `whose`.

        Regression guard for a bug where `search_messages("X", "INBOX")` with no
        filters emitted `messages of mailboxRef whose true`, which Mail.app
        rejects with `Illegal comparison or logical (-1726)`.
        """
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX")
        script = mock_run.call_args[0][0]
        assert "whose true" not in script
        # With NO filters, the generated source must reference `mailboxRef`
        # without a `whose` clause.
        assert "messages of mailboxRef\n" in script or "messages of mailboxRef " in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_date_range_filter(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """date_from/date_to are constructed as AppleScript date objects via
        property setters in a preamble above the loop, then referenced as
        variables in IF expressions inside the loop. (#242)

        The `date "YYYY-MM-DD"` literal form does NOT work in AppleScript —
        it parses 2026-05-28 as arithmetic and yields year-12196, silently
        filtering out every real-world message. The construction pattern is
        locale-independent and gives exactly midnight local time on the
        target date.
        """
        mock_run.return_value = "[]"
        connector._search_messages_applescript(
            "Gmail", "INBOX", date_from="2026-04-01", date_to="2026-04-15"
        )
        script = mock_run.call_args[0][0]
        # Preamble: construct dateFromVar via property setters.
        assert "set dateFromVar to current date" in script
        assert "set year of dateFromVar to 2026" in script
        assert "set month of dateFromVar to 4" in script
        assert "set day of dateFromVar to 1" in script
        # Preamble: construct dateToExclVar at the day AFTER date_to (exclusive
        # upper bound so the full date_to day is inclusive).
        assert "set dateToExclVar to current date" in script
        assert "set year of dateToExclVar to 2026" in script
        assert "set month of dateToExclVar to 4" in script
        assert "set day of dateToExclVar to 16" in script
        # In-loop clauses reference the variables.
        assert "if (date received of msg) < dateFromVar then set includeThis to false" in script
        assert "if (date received of msg) >= dateToExclVar then set includeThis to false" in script
        # Guard against regression to the broken date literal form.
        assert 'date "2026-04-01"' not in script
        assert 'date "2026-04-16"' not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_result_includes_flagged(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """New in #28: result rows include the flagged status."""
        mock_run.return_value = (
            '[{"id":"1","subject":"s","sender":"a@b.c",'
            '"date_received":"Mon","read_status":false,"flagged":true}]'
        )
        result = connector._search_messages_applescript("Gmail", "INBOX")
        assert result[0]["flagged"] is True

    # --- Issue #72 dispatcher behavior -----------------------------------

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_with_full_name_emits_display_form(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: account with full_name -> 'Display Name <email>' form."""
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "Alice Smith",
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert connector._resolve_account_to_sender("iCloud") == "Alice Smith <alice@icloud.com>"

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_without_full_name_falls_back_to_bare_email(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: account without full_name -> bare email (graceful fallback)."""
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": None,
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert connector._resolve_account_to_sender("iCloud") == "alice@icloud.com"

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_whitespace_only_full_name_falls_back(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: whitespace-only full_name treated as not-configured."""
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "   ",
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert connector._resolve_account_to_sender("iCloud") == "alice@icloud.com"

    # ---- get_thread ----

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_drops_threading_internals_from_output(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Response rows must NOT leak in_reply_to / references_raw /
        references_parsed (threading-internal scratch fields). They
        DO carry rfc_message_id alongside id (dual-emit from #148).
        """
        mock_run.side_effect = [
            (
                '{"account":"Gmail","rfc_message_id":"<anchor@x>",'
                '"subject":"Q3","in_reply_to":"","references_raw":""}'
            ),
            (
                '[{"id":"100","rfc_message_id":"<anchor@x>","in_reply_to":"",'
                '"references_raw":"","subject":"Q3","sender":"a@x",'
                '"date_received":"Mon","read_status":false,"flagged":false}]'
            ),
        ]
        result = connector._get_thread_applescript("100")
        for m in result:
            assert "rfc_message_id" in m
            assert "in_reply_to" not in m
            assert "references_raw" not in m
            assert "references_parsed" not in m


class TestBulkOpsSourceMailbox:
    """Regression guards for #103: bulk-mutation methods accept paired
    `account` + `source_mailbox` parameters that narrow the AppleScript
    scan from O(N × accounts × mailboxes) to O(N).

    Both params must be provided together (a mailbox name without an
    account is ambiguous because the same name can exist across accounts).
    Either alone raises ValueError.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    # ------ mark_as_read ------

    # ------ move_messages ------
    # Note: move_messages already has `account` for the DESTINATION account.
    # `source_mailbox` is independent — it narrows where we LOOK for the
    # source messages. Both branches (gmail_mode True/False) need the
    # narrow path.

    # ------ flag_message ------

    # ------ delete_messages ------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_messages_permanent_emits_deprecation_warning(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Issue #111: Mail.app exposes no AppleScript path to bypass Trash.
        `permanent=True` is a no-op; warn so callers don't silently rely on
        absent behavior.
        """
        # Skip the #150 IMAP fast path so the AppleScript narrow path runs.
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run.return_value = "1"
        with pytest.warns(DeprecationWarning, match="#111"):
            connector.delete_messages(
                ["abc"],
                permanent=True,
                account="iCloud",
                source_mailbox="Junk",
            )
        # Script shape unchanged from the non-permanent path: `delete msg`
        # always moves to the account's Trash mailbox today.
        script = mock_run.call_args[0][0]
        assert 'set sourceMb to my resolveMailbox(account "iCloud", "Junk")' in script
        assert "delete msg" in script
        assert "repeat with acc in accounts" not in script


def _raw_with_attachments(atts: list[tuple[str, str, bytes]]) -> bytes:
    """Build raw RFC 822 bytes with the given attachments.

    ``atts`` is a list of ``(filename, subtype, payload_bytes)``.
    """
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"] = "s@example.com"
    m["To"] = "r@example.com"
    m["Subject"] = "with attachments"
    m["Message-ID"] = "<att-test@example.com>"
    m.set_content("body")
    for filename, subtype, data in atts:
        m.add_attachment(data, maintype="application", subtype=subtype, filename=filename)
    return m.as_bytes()


class TestDeleteDraft:
    """Tests for AppleMailConnector.delete_draft."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_resolve_draft_lookup_id", return_value='1"x\\y')
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_draft_escapes_resolved_id(
        self,
        mock_run: MagicMock,
        mock_resolve: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """#294 defense-in-depth: the resolved id is escaped at the
        interpolation site, so a (hypothetical) quote/backslash-bearing id
        can't break out of the AppleScript string even if it ever got past
        validation/resolution.
        """
        from apple_mail_fast_mcp.utils import (
            escape_applescript_string,
            sanitize_input,
        )

        mock_run.return_value = "OK"
        connector.delete_draft("validid")
        script = mock_run.call_args[0][0]
        expected = escape_applescript_string(sanitize_input('1"x\\y'))
        assert f'whose id is "{expected}"' in script


class TestFindMessageByMessageId:
    """Tests for AppleMailConnector.find_message_by_message_id."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_internal_id_for_bare_rfc_input(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Read tools (#148) emit bare RFC ids on the IMAP path. Round-trip
        through ``create_draft(reply_to=...)`` requires this call to return
        Mail's internal id when the input is bare. Unit test asserts API
        surface; an integration test asserts the AppleScript actually
        matches against Mail.app's storage.
        """
        mock_run.return_value = "54957"
        result = connector.find_message_by_message_id(
            "1779175169746.aa805a12-74b6-4330-93ff-72a175ed8679@example.com"
        )
        assert result == "54957"


class TestGetDraftState:
    """Tests for AppleMailConnector.get_draft_state."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_resolve_draft_lookup_id", return_value='1"x\\y')
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_draft_state_escapes_resolved_id(
        self,
        mock_run: MagicMock,
        mock_resolve: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """#294 defense-in-depth: the resolved id is escaped into targetId."""
        from apple_mail_fast_mcp.utils import (
            escape_applescript_string,
            sanitize_input,
        )

        mock_run.return_value = '{"found":false}'
        with contextlib.suppress(MailDraftNotFoundError):
            connector.get_draft_state("validid")
        script = mock_run.call_args[0][0]
        expected = escape_applescript_string(sanitize_input('1"x\\y'))
        assert f'set targetId to "{expected}"' in script


class TestCreateDraft:
    """Tests for AppleMailConnector.create_draft."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Fresh seed (`seed='new'`)
    # ------------------------------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_new_with_from_account_sets_display_name_sender(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: when the resolver returns a Display Name <email> string,
        the AppleScript embeds it verbatim (escaped) on the sender line.
        """
        mock_run.return_value = "1"
        with patch.object(
            connector,
            "_resolve_account_to_sender",
            return_value="Alice Smith <me@x.com>",
        ):
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                from_account="Gmail",
                # #245: seed="new" save-as-draft now routes via IMAP APPEND;
                # send_now=True keeps exercising the AppleScript sender line.
                send_now=True,
            )
        script = mock_run.call_args[0][0]
        assert 'set sender of theMessage to "Alice Smith <me@x.com>"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_new_with_from_account_bare_email_passthrough(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: when the resolver returns a bare email (no display name
        configured), the AppleScript embeds the bare form.
        """
        mock_run.return_value = "1"
        with patch.object(connector, "_resolve_account_to_sender", return_value="me@x.com"):
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                from_account="Gmail",
                # #245: seed="new" save-as-draft now routes via IMAP APPEND;
                # send_now=True keeps exercising the AppleScript sender line.
                send_now=True,
            )
        script = mock_run.call_args[0][0]
        assert 'set sender of theMessage to "me@x.com"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_new_with_from_account_sanitizes_sender_null_bytes(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#173: sender string is run through sanitize_input before
        escape_applescript_string, so embedded null bytes (which would
        otherwise truncate or confuse the AppleScript at runtime) are
        stripped per the SECURITY_CHECKLIST two-step convention.
        """
        mock_run.return_value = "1"
        with patch.object(
            connector,
            "_resolve_account_to_sender",
            return_value="Alice\x00Smith <me@x.com>",
        ):
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                from_account="Gmail",
                # #245: seed="new" save-as-draft now routes via IMAP APPEND;
                # send_now=True keeps exercising the AppleScript sender line.
                send_now=True,
            )
        script = mock_run.call_args[0][0]
        assert "\x00" not in script
        assert 'set sender of theMessage to "AliceSmith <me@x.com>"' in script

    # ------------------------------------------------------------------
    # Reply seed
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Forward seed
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Seed lookup error mapping
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # RFC 5322 Message-ID seed_id support (#205)
    # ------------------------------------------------------------------

    @patch.object(AppleMailConnector, "find_message_by_message_id")
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reply_resolves_rfc_message_id_seed(
        self,
        mock_run: MagicMock,
        mock_resolve: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Read tools (#148) emit RFC ids on the IMAP path. create_draft
        must resolve them to Mail's internal id before building the
        `whose id is` AppleScript clause. (#205)
        """
        mock_resolve.return_value = "160989"
        mock_run.return_value = "1"
        connector.create_draft(
            seed="reply",
            seed_id="abc-123@example.com",  # RFC form (contains '@')
            body="thanks",
        )
        mock_resolve.assert_called_once_with("abc-123@example.com")
        script = mock_run.call_args[0][0]
        # AppleScript looks up by Mail's internal id, not the RFC id.
        assert 'whose id is "160989"' in script
        assert "abc-123@example.com" not in script

    @patch.object(AppleMailConnector, "find_message_by_message_id")
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_forward_resolves_rfc_message_id_seed(
        self,
        mock_run: MagicMock,
        mock_resolve: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Same RFC-id resolution applies to the forward branch. (#205)"""
        mock_resolve.return_value = "160989"
        mock_run.return_value = "1"
        connector.create_draft(
            seed="forward",
            seed_id="abc-123@example.com",
            to=["x@example.com"],
            body="fyi",
        )
        mock_resolve.assert_called_once_with("abc-123@example.com")
        script = mock_run.call_args[0][0]
        assert 'whose id is "160989"' in script

    @patch.object(AppleMailConnector, "find_message_by_message_id")
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_internal_numeric_seed_id_skips_resolver(
        self,
        mock_run: MagicMock,
        mock_resolve: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Existing callers passing Mail's internal numeric id (no '@')
        must keep working without a resolver round-trip. (#205)
        """
        mock_run.return_value = "1"
        connector.create_draft(
            seed="reply",
            seed_id="160989",  # internal id form, no '@'
            body="thanks",
        )
        mock_resolve.assert_not_called()
        script = mock_run.call_args[0][0]
        assert 'whose id is "160989"' in script

    # No from_account → create_draft would auto-resolve a sole account
    # (#321) via list_accounts; stub it out so this test isolates the RFC
    # seed-resolution path.
    @patch.object(AppleMailConnector, "_resolve_implicit_account", return_value=None)
    @patch.object(AppleMailConnector, "find_message_by_message_id")
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_unresolvable_rfc_seed_raises_message_not_found(
        self,
        mock_run: MagicMock,
        mock_resolve: MagicMock,
        _mock_implicit: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """When the RFC id doesn't match any message, surface the same
        MailMessageNotFoundError the AppleScript SEED_NOT_FOUND path
        produces — caller can't tell the difference. (#205)
        """
        mock_resolve.return_value = None
        with pytest.raises(MailMessageNotFoundError):
            connector.create_draft(
                seed="reply",
                seed_id="missing@example.com",
                body="x",
            )
        # AppleScript should not run if we can't resolve the seed.
        mock_run.assert_not_called()


class TestExtractDraftAttachments:
    """Tests for AppleMailConnector.extract_draft_attachments."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_resolve_draft_internal_id", return_value="160991")
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_extract_resolves_rfc_message_id(
        self,
        mock_run: MagicMock,
        mock_find: MagicMock,
        connector: AppleMailConnector,
        tmp_path: Any,
    ) -> None:
        """#294: extract_draft_attachments resolves an RFC Message-ID
        draft_id to Mail's internal id (like delete_draft/get_draft_state),
        so update_draft preserves attachments on IMAP-APPEND drafts (#245).
        """
        mock_run.return_value = "0"
        connector.extract_draft_attachments("abc.123@host", ["a.pdf"], tmp_path)
        mock_find.assert_called_once_with("abc.123@host")
        script = mock_run.call_args[0][0]
        assert 'set targetId to "160991"' in script

    @patch.object(AppleMailConnector, "_resolve_draft_lookup_id", return_value='1"x\\y')
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_extract_escapes_resolved_id(
        self,
        mock_run: MagicMock,
        mock_resolve: MagicMock,
        connector: AppleMailConnector,
        tmp_path: Any,
    ) -> None:
        """#294 defense-in-depth: the resolved id is escaped into targetId."""
        from apple_mail_fast_mcp.utils import (
            escape_applescript_string,
            sanitize_input,
        )

        mock_run.return_value = "0"
        connector.extract_draft_attachments("validid", ["a.pdf"], tmp_path)
        script = mock_run.call_args[0][0]
        expected = escape_applescript_string(sanitize_input('1"x\\y'))
        assert f'set targetId to "{expected}"' in script


class TestUpdateMailbox:
    """Tests for AppleMailConnector.update_mailbox (rename only — #102)."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_handles_nested_path(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Slash-separated path passes through to the resolveMailbox handler,
        which walks the container chain to find the nested mailbox (#247).
        """
        mock_run.return_value = "success"
        connector.update_mailbox(account="Gmail", name="Archive/2024", new_name="Archive2024")
        script = mock_run.call_args[0][0]
        assert 'set mb to my resolveMailbox(accountRef, "Archive/2024")' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_gmail_system_label_source_refused_before_applescript(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Pre-flight: source name like ``[Gmail]/Drafts`` raises
        ``MailUnsupportedGmailSystemLabelError`` before any AppleScript
        runs (#164). Renames of Gmail system labels don't stick anyway.
        """
        from apple_mail_fast_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )

        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.update_mailbox(
                account="Gmail",
                name="[Gmail]/Drafts",
                new_name="MyDrafts",
            )
        mock_run.assert_not_called()


class TestUpdateMailboxMove:
    """IMAP-dispatched move path of update_mailbox (#163)."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch("apple_mail_fast_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_fast_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_gmail_system_label_source_refused_before_imap_session(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Move source ``[Gmail]/Sent Mail`` raises before the IMAP
        credential lookup runs (#164).
        """
        from apple_mail_fast_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )

        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.update_mailbox(
                account="Gmail",
                name="[Gmail]/Sent Mail",
                new_parent="Archive",
            )
        # No IMAP session opened, no credentials looked up.
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()

    @patch("apple_mail_fast_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_fast_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_gmail_system_label_destination_parent_refused(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Moving a regular folder INTO ``[Gmail]/Subfolder`` is also
        refused — the resulting destination would land in Gmail's
        system-label namespace (#164).
        """
        from apple_mail_fast_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )

        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.update_mailbox(
                account="Gmail",
                name="Archive",
                new_parent="[Gmail]/Subfolder",
            )
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()

    @patch("apple_mail_fast_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_fast_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_bare_gmail_parent_destination_refused(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """``new_parent="[Gmail]"`` produces a destination of
        ``[Gmail]/<leaf>`` — also a system-label path; refused (#164).
        """
        from apple_mail_fast_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )

        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.update_mailbox(
                account="Gmail",
                name="Archive",
                new_parent="[Gmail]",
            )
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()


class TestDeleteMailbox:
    """delete_mailbox via IMAP (#162)."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch("apple_mail_fast_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_fast_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_gmail_system_label_refused_before_credential_lookup(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Pre-flight: deleting ``[Gmail]/Trash`` raises before the IMAP
        credential lookup runs (#164).
        """
        from apple_mail_fast_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )

        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.delete_mailbox(account="Gmail", name="[Gmail]/Trash")
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()

    @patch("apple_mail_fast_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_fast_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_bare_gmail_parent_refused(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """The bare ``[Gmail]`` parent is also refused (#164)."""
        from apple_mail_fast_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )

        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.delete_mailbox(account="Gmail", name="[Gmail]")
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()


# =============================================================================
# received_within_hours (#230)
# =============================================================================


class TestReceivedWithinHours:
    """Tests for the new `received_within_hours` parameter on search_messages.

    Connector-tier coverage: AppleScript clause emission, IMAP-path
    post-filter, validation, composition with date_from, and the _now()
    monkeypatch hook used for deterministic time math.
    """

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_applescript_emits_relative_hours_short_circuit(self, mock_run: MagicMock) -> None:
        """AS path hoists the cutoff out of the loop AND uses `exit repeat`
        instead of a filter-skip. With newest-first iteration (#242), once a
        message is older than the cutoff, every subsequent iteration would
        also be older — so we exit the loop entirely instead of skipping.
        """
        from apple_mail_fast_mcp.mail_connector import AppleMailConnector

        connector = AppleMailConnector()
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX", received_within_hours=6)
        script = mock_run.call_args[0][0]
        # Hoisted cutoff: computed once before the loop.
        assert "set cutoffDate to (current date) - (6 * hours)" in script
        # Short-circuit: exit the loop on the first message older than cutoff.
        assert ("if (date received of msg) < cutoffDate then exit repeat") in script
        # Guard: the old per-iteration inline form must NOT appear in the
        # filter block — that pattern was the performance bug fixed by #242.
        assert (
            "if (date received of msg) < ((current date) - (6 * hours)) "
            "then set includeThis to false"
        ) not in script


# =============================================================================
# IMAP Keychain dual-form lookup (#243)
# =============================================================================


@pytest.mark.real_account_fallback
class TestKeychainDualFormLookup:
    """Keychain entries are written under whatever string the user typed at
    setup-imap time (typically the account NAME). Callers may legitimately
    pass either the name or the UUID (per the docstring's stability claim).
    The wrapper retries with the alternative form on initial NotFound.

    Opts out of the conftest ``_alternative_account_identifier`` stub via the
    ``real_account_fallback`` marker — these tests exercise the real fallback
    against an instance-mocked ``list_accounts`` (no AppleScript).
    """

    def test_env_var_keyed_on_name_found_when_caller_passes_uuid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#248 + #243 compose: the env var is keyed on the account NAME, but
        the caller passes the UUID. The real get_imap_password is exercised —
        the UUID form misses (env + Keychain), the wrapper resolves UUID→name,
        and the name form hits the env var (no Keychain shell-out for it).
        """
        from apple_mail_fast_mcp.mail_connector import AppleMailConnector

        monkeypatch.setenv("APPLE_MAIL_MCP_IMAP_PASSWORD_GMAIL", "ENV-PW")
        # The UUID form has no env var and its Keychain lookup must report
        # not-found (exit 44) so the wrapper falls back to the name form.
        run_calls: list[list[str]] = []

        def fake_run(cmd, *a, **kw):
            run_calls.append(cmd)
            m = MagicMock()
            m.returncode = 44  # item not found
            m.stdout = ""
            m.stderr = "could not be found in the keychain."
            return m

        monkeypatch.setattr("apple_mail_fast_mcp.keychain.subprocess.run", fake_run)
        c = AppleMailConnector()
        monkeypatch.setattr(
            c,
            "list_accounts",
            lambda: [{"name": "Gmail", "id": "04E9E040-D5C2-4B6B-8FFA-5AAF3DCCAB16"}],
        )
        result = c._get_imap_password_with_fallback(
            "04E9E040-D5C2-4B6B-8FFA-5AAF3DCCAB16", "alice@gmail.com"
        )
        assert result == "ENV-PW"
        # Only the UUID form shelled out to `security`; the name form was
        # satisfied by the env var without a Keychain call. The UUID form
        # probes both prefixes (new, then legacy on the NotFound miss — #337).
        assert len(run_calls) == 2
        services = [cmd[cmd.index("-s") + 1] for cmd in run_calls]
        assert services == [
            "apple-mail-fast-mcp.imap.04E9E040-D5C2-4B6B-8FFA-5AAF3DCCAB16",
            "apple-mail-mcp.imap.04E9E040-D5C2-4B6B-8FFA-5AAF3DCCAB16",
        ]


# =============================================================================
# Mailbox resolver shape (#247)
# =============================================================================


class TestCreateDraftImapAppend:
    """seed='new' drafts are created via IMAP APPEND to avoid Mail.app's
    cite-blockquote wrapper (issue #245).
    """

    def _conn(self):
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch("apple_mail_fast_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_fast_mcp.mail_connector.get_imap_password", return_value="pw")
    @patch.object(
        AppleMailConnector,
        "_resolve_imap_config",
        return_value=("imap.host", 993, "appleid@fmasi.eu"),
    )
    @patch.object(
        AppleMailConnector,
        "_resolve_account_to_sender",
        return_value="Fred <email@fmasi.eu>",
    )
    def test_body_html_appends_multipart_alternative(
        self, _sender, _cfg, _pw, mock_imap_cls, mock_applescript
    ):
        """#251: body_html threads into the IMAP MIME as a
        multipart/alternative (text/plain + text/html).
        """
        import email as _email
        from email import policy as _policy

        conn = self._conn()
        result = conn.create_draft(
            seed="new",
            to=["lazar@hadleigh.co.uk"],
            subject="Q2 numbers",
            body="plain fallback",
            body_html="<p>Revenue <b>up</b></p>",
            from_account="iCloud",
            send_now=False,
        )
        raw = mock_imap_cls.return_value.append_draft.call_args[0][0]
        msg = _email.message_from_bytes(raw, policy=_policy.default)
        assert msg.get_content_type() == "multipart/alternative"
        html_part = msg.get_body(preferencelist=("html",))
        plain_part = msg.get_body(preferencelist=("plain",))
        assert html_part is not None
        assert plain_part is not None
        assert "<b>up</b>" in html_part.get_content()
        assert plain_part.get_content().strip() == "plain fallback"
        assert "@" in result["draft_id"]

    @patch.object(AppleMailConnector, "_run_applescript", return_value="123")
    @patch("apple_mail_fast_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_fast_mcp.mail_connector.get_imap_password", return_value="pw")
    @patch.object(
        AppleMailConnector,
        "_resolve_imap_config",
        return_value=("imap.host", 993, "appleid@fmasi.eu"),
    )
    @patch.object(
        AppleMailConnector,
        "_resolve_account_to_sender",
        return_value="Fred <email@fmasi.eu>",
    )
    def test_body_html_fails_loud_when_imap_unavailable(
        self, _sender, _cfg, _pw, mock_imap_cls, mock_applescript
    ):
        """#251: when body_html is set and the IMAP path can't engage, raise
        MailDraftHtmlUnavailableError — never silently downgrade to a
        plain-text AppleScript draft.
        """
        from apple_mail_fast_mcp.exceptions import (
            MailDraftHtmlUnavailableError,
        )

        mock_imap_cls.return_value.append_draft.side_effect = MailKeychainEntryNotFoundError(
            "no creds"
        )
        conn = self._conn()
        with pytest.raises(MailDraftHtmlUnavailableError):
            conn.create_draft(
                seed="new",
                to=["x@example.invalid"],
                subject="hi",
                body="body",
                body_html="<p>rich</p>",
                from_account="iCloud",
                send_now=False,
            )
        # IMAP was attempted, but NO AppleScript draft-build fallback ran.
        mock_imap_cls.return_value.append_draft.assert_called_once()
        scripts = [c[0][0] for c in mock_applescript.call_args_list]
        assert not any("make new outgoing message" in s for s in scripts)


class TestSmtpSendPath:
    """#322: create_draft(send_now=True) submits a wrapper-free RFC 822
    message over SMTP, bypassing Mail.app's AppleScript
    ``tell theMessage to send`` (whose ``content`` setter applies the
    FB11734014 cite-blockquote to sent mail). The SMTP boundary is
    ``mail_connector.SmtpSender``.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def _configure_smtp_without_sent_stub(
        self,
        connector: AppleMailConnector,
        monkeypatch: pytest.MonkeyPatch,
        *,
        host: str = "smtp.x.test",
        port: int = 587,
        email: str = "me@x.test",
        password: str = "pw",
    ) -> None:
        """Wire the connector so the SMTP path engages without real I/O,
        leaving ``_save_sent_copy`` real so the Sent-copy behavior (#406) can
        be exercised (with ``ImapConnector`` patched at the class boundary).
        """
        monkeypatch.setattr(connector, "_resolve_smtp_config", lambda account: (host, port, email))
        monkeypatch.setattr(
            connector,
            "_resolve_imap_config",
            lambda account: ("imap.x.test", 993, email),
        )
        monkeypatch.setattr(
            connector,
            "_get_imap_password_with_fallback",
            lambda account, e: password,
        )
        monkeypatch.setattr(connector, "_resolve_account_to_sender", lambda account: email)
        monkeypatch.setattr(connector, "_imap_breaker_open", lambda account: False)
        monkeypatch.setattr(connector, "_imap_clear_breaker", lambda account: None)

    def _configure_smtp(
        self,
        connector: AppleMailConnector,
        monkeypatch: pytest.MonkeyPatch,
        *,
        host: str = "smtp.x.test",
        port: int = 587,
        email: str = "me@x.test",
        password: str = "pw",
    ) -> None:
        """Wire the connector so the SMTP path engages without real I/O.

        These transport tests assert on the SMTP submission, not the
        post-send Sent-mailbox copy (#406), which would otherwise attempt
        real IMAP I/O — so ``_save_sent_copy`` is stubbed. The Sent-copy
        behavior has its own dedicated coverage (see the ``#406`` tests,
        which use :meth:`_configure_smtp_without_sent_stub`).
        """
        self._configure_smtp_without_sent_stub(
            connector,
            monkeypatch,
            host=host,
            port=port,
            email=email,
            password=password,
        )
        monkeypatch.setattr(connector, "_save_sent_copy", lambda *a, **k: None)

    # --- the bug regression -------------------------------------------------

    def test_send_now_compose_uses_smtp_not_applescript(
        self, connector: AppleMailConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for #322: with SMTP configured, a fresh send_now is
        submitted over SMTP as a clean (no cite-blockquote) message and the
        AppleScript ``tell theMessage to send`` path is never reached.
        """
        self._configure_smtp(connector, monkeypatch)
        scripts: list[str] = []
        monkeypatch.setattr(connector, "_run_applescript", lambda s: scripts.append(s) or "")
        with patch("apple_mail_fast_mcp.mail_connector.SmtpSender") as sender_cls:
            result = connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="Hi",
                body="Hello there",
                from_account="Gmail",
                send_now=True,
            )

        assert not any("tell theMessage to send" in s for s in scripts)
        sender_cls.assert_called_once()
        sender_cls.return_value.send.assert_called_once()
        raw, recipients = sender_cls.return_value.send.call_args.args
        assert b"Hello there" in raw
        assert b"blockquote" not in raw.lower()  # FB11734014 wrapper absent
        assert recipients == ["a@example.com"]
        assert result == {
            "draft_id": "",
            "sent_message_id": "",
            "from_account": "Gmail",
        }

    # --- graceful fallback --------------------------------------------------

    def test_non_221_quit_after_accept_does_not_double_send(
        self, connector: AppleMailConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR #404: if the server accepts the message (send_message → 250) but
        then returns a non-221 to QUIT, ``SMTP.__exit__`` raises
        ``SMTPResponseException``. That teardown error must NOT propagate into
        an AppleScript fallback — otherwise a second copy is sent. Uses a real
        ``SmtpSender`` over a mocked ``smtplib`` so the whole send path (not
        just a mocked SmtpSender) is exercised.
        """
        self._configure_smtp(connector, monkeypatch)
        scripts: list[str] = []
        monkeypatch.setattr(connector, "_run_applescript", lambda s: scripts.append(s) or "SENT")
        with patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP") as mock_smtp:
            client = mock_smtp.return_value.__enter__.return_value
            # send_message succeeds (message accepted); QUIT on `with` exit
            # returns non-221, which SMTP.__exit__ raises.
            mock_smtp.return_value.__exit__.side_effect = smtplib.SMTPResponseException(
                421, b"4.7.0 try later"
            )
            result = connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="Hi",
                body="Hello there",
                from_account="Gmail",
                send_now=True,
            )

        # Exactly one real SMTP submission, and no AppleScript duplicate.
        client.send_message.assert_called_once()
        assert not any("tell theMessage to send" in s for s in scripts)
        assert result == {
            "draft_id": "",
            "sent_message_id": "",
            "from_account": "Gmail",
        }

    # --- non-engagement -----------------------------------------------------

    # --- reply / forward send ----------------------------------------------

    # --- Sent-mailbox copy after a successful send (#406) -------------------

    def test_compose_send_saves_sent_copy(
        self, connector: AppleMailConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#406: a successful compose send APPENDs a copy to the account's
        Sent folder over IMAP (not marked as a reply).
        """
        self._configure_smtp_without_sent_stub(connector, monkeypatch)
        monkeypatch.setattr(connector, "_run_applescript", lambda s: "")
        with (
            patch("apple_mail_fast_mcp.mail_connector.SmtpSender"),
            patch("apple_mail_fast_mcp.mail_connector.ImapConnector") as imap_cls,
        ):
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="Hi",
                body="Hello there",
                from_account="Gmail",
                send_now=True,
            )
        imap_cls.return_value.append_sent_copy.assert_called_once()
        _args, kwargs = imap_cls.return_value.append_sent_copy.call_args
        assert kwargs.get("answered") is False

    def test_reply_send_saves_sent_copy_marked_answered(
        self, connector: AppleMailConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#406: a reply send saves an \\Answered Sent copy, reusing the
        connector already built to fetch the original.
        """
        self._configure_smtp_without_sent_stub(connector, monkeypatch)
        monkeypatch.setattr(
            connector,
            "_build_reply_forward_mime",
            lambda **kw: ("<m@id>", b"rawreply", ["orig@example.net"]),
        )
        with (
            patch("apple_mail_fast_mcp.mail_connector.SmtpSender"),
            patch("apple_mail_fast_mcp.mail_connector.ImapConnector") as imap_cls,
        ):
            connector._try_smtp_send(
                seed="reply",
                seed_id="orig@id",
                seed_mailbox="INBOX",
                send_now=True,
                from_account="Gmail",
                to=None,
                cc=None,
                bcc=None,
                subject=None,
                body="thanks",
                reply_all=False,
                attachment_paths=None,
            )
        imap_cls.return_value.append_sent_copy.assert_called_once()
        _args, kwargs = imap_cls.return_value.append_sent_copy.call_args
        assert kwargs.get("answered") is True

    def test_gmail_send_skips_sent_copy_because_gmail_auto_saves(
        self, connector: AppleMailConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR #404 re-review: Gmail's SMTP server auto-files submitted mail
        into ``[Gmail]/Sent Mail`` server-side. Appending our own copy would
        create a DUPLICATE in Sent, so for Gmail (and any provider flagged
        ``smtp_saves_sent_copy``) the post-send APPEND is skipped. Verified
        empirically against a live Gmail account during the #404 review.
        """
        self._configure_smtp_without_sent_stub(
            connector, monkeypatch, host="smtp.gmail.com", email="me@gmail.com"
        )
        monkeypatch.setattr(connector, "_run_applescript", lambda s: "")
        with (
            patch("apple_mail_fast_mcp.mail_connector.SmtpSender"),
            patch("apple_mail_fast_mcp.mail_connector.ImapConnector") as imap_cls,
        ):
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="Hi",
                body="Hello there",
                from_account="Gmail",
                send_now=True,
            )
        imap_cls.return_value.append_sent_copy.assert_not_called()

    def test_sent_copy_failure_is_swallowed_and_no_applescript_fallback(
        self, connector: AppleMailConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#406 hard rule: the message is already delivered when the Sent-copy
        APPEND runs, so a failure there must be swallowed (logged, not raised)
        and must NOT trigger the AppleScript ``tell theMessage to send``
        fallback — that would deliver a duplicate (the PR #404 double-send
        class of bug). The send result is still the normal success dict.
        """
        self._configure_smtp_without_sent_stub(connector, monkeypatch)
        scripts: list[str] = []
        monkeypatch.setattr(connector, "_run_applescript", lambda s: scripts.append(s) or "SENT")
        with (
            patch("apple_mail_fast_mcp.mail_connector.SmtpSender"),
            patch("apple_mail_fast_mcp.mail_connector.ImapConnector") as imap_cls,
        ):
            # Sent-copy APPEND blows up hard (no Sent folder, protocol error…).
            imap_cls.return_value.append_sent_copy.side_effect = MailMessageNotFoundError(
                "no Sent folder"
            )
            result = connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="Hi",
                body="Hello there",
                from_account="Gmail",
                send_now=True,
            )
        # The failure was swallowed: normal success dict, single SMTP send,
        # and crucially NO AppleScript fallback send.
        assert result == {
            "draft_id": "",
            "sent_message_id": "",
            "from_account": "Gmail",
        }
        assert not any("tell theMessage to send" in s for s in scripts)

    # --- test-mode transport-boundary safety guard (#322 / #175) -----------

    def test_test_mode_blocks_derived_reply_all_recipient(
        self, connector: AppleMailConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#175/#322: reply_all derives cc from the original — a real derived
        recipient the server-layer gate never saw must be caught at the
        transport boundary, and must NOT silently fall back to AppleScript.
        """
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        self._configure_smtp(connector, monkeypatch)
        monkeypatch.setattr(
            connector,
            "_build_reply_forward_mime",
            lambda **kw: (
                "<m@id>",
                b"raw",
                ["ok@example.com", "boss@real-company.com"],
            ),
        )
        with (
            patch("apple_mail_fast_mcp.mail_connector.SmtpSender") as sender_cls,
            pytest.raises(MailSafetyError),
        ):
            connector._try_smtp_send(
                seed="reply",
                seed_id="orig@id",
                seed_mailbox="INBOX",
                send_now=True,
                from_account="Gmail",
                to=["ok@example.com"],
                cc=None,
                bcc=None,
                subject=None,
                body="hi",
                reply_all=True,
                attachment_paths=None,
            )
        sender_cls.assert_not_called()

    # --- config discovery ---------------------------------------------------
