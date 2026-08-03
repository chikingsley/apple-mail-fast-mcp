"""Regressions for all-account Apple Mail junk discovery."""

from unittest.mock import MagicMock

from apple_mail_fast_mcp.junk_cleaner import JunkMailbox, junk_mailboxes
from apple_mail_fast_mcp.mail_connector import AppleMailConnector


def test_junk_regression_discovers_each_enabled_accounts_junk_mailbox() -> None:
    """Regression: scheduled cleanup must cover every account instead of one hard-coded inbox."""
    connector = MagicMock(spec=AppleMailConnector)
    connector.list_accounts.return_value = [
        {"id": "UUID-GMAIL", "name": "first@gmail.com", "enabled": True},
        {"id": "UUID-OUTLOOK", "name": "second@outlook.com", "enabled": True},
        {"id": "UUID-DISABLED", "name": "disabled@example.com", "enabled": False},
    ]
    connector.list_mailboxes.side_effect = lambda account: {
        "UUID-GMAIL": [
            {"name": "INBOX", "path": "INBOX"},
            {"name": "Spam", "path": "[Gmail]/Spam"},
        ],
        "UUID-OUTLOOK": [{"name": "Junk Email", "path": "Junk Email"}],
    }[account]

    assert junk_mailboxes(connector) == [
        JunkMailbox("UUID-GMAIL", "first@gmail.com", "[Gmail]/Spam"),
        JunkMailbox("UUID-OUTLOOK", "second@outlook.com", "Junk Email"),
    ]
