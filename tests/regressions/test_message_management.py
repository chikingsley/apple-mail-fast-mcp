"""Unit tests for message management functionality."""

from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector


class TestDeleteMessages:
    """Tests for deleting messages."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_permanent_delete_uses_scoped_imap_expunge(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Issue #111: permanent=True must perform a real scoped expunge."""
        mock_run.return_value = '["rfc@example.test"]'

        with patch.object(
            connector, "_permanently_delete_imap_messages", return_value=1
        ) as permanent_delete:
            result = connector.delete_messages(
                message_ids=["12345"],
                permanent=True,
                account="Gmail",
                source_mailbox="[Gmail]/Trash",
            )

        assert result == 1
        permanent_delete.assert_called_once_with(
            account="Gmail",
            mailbox="[Gmail]/Trash",
            rfc_message_ids=["rfc@example.test"],
        )
