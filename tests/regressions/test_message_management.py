"""Unit tests for message management functionality."""

from unittest.mock import MagicMock, patch

import pytest

from apple_mail_fast_mcp.mail_connector import AppleMailConnector


class TestDeleteMessages:
    """Tests for deleting messages."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_permanent_delete_warns_and_still_returns_count(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Issue #111: permanent=True emits a DeprecationWarning since
        Mail.app exposes no AppleScript path that actually bypasses Trash.
        The call still succeeds (messages are moved to Trash like the
        default path) and returns the count.
        """
        mock_run.return_value = "1"

        with pytest.warns(DeprecationWarning, match="#111"):
            result = connector.delete_messages(message_ids=["12345"], permanent=True)

        assert result == 1
