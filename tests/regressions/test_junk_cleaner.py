"""Regressions for all-account Apple Mail junk discovery and cleanup."""

from pathlib import Path
from unittest.mock import MagicMock

from apple_mail_fast_mcp.junk_campaigns import JunkCampaignStore
from apple_mail_fast_mcp.junk_cleaner import (
    JunkCleanerConfig,
    JunkMailbox,
    _eligible_messages,
    clean_junk,
    junk_mailboxes,
)
from apple_mail_fast_mcp.junk_providers import ProviderAccount
from apple_mail_fast_mcp.mail_connector import AppleMailConnector


def test_junk_regression_discovers_each_enabled_accounts_junk_mailbox() -> None:
    """Regression: scheduled cleanup must cover every account instead of one hard-coded inbox."""
    connector = MagicMock(spec=AppleMailConnector)
    connector.list_accounts.return_value = [
        {"id": "UUID-GMAIL", "name": "first@gmail.com", "enabled": True},
        {"id": "UUID-OUTLOOK", "name": "second@outlook.com", "enabled": True},
        {"id": "UUID-IMAP", "name": "third@example.com", "enabled": True},
        {"id": "UUID-DISABLED", "name": "disabled@example.com", "enabled": False},
    ]
    connector.list_mailboxes.side_effect = lambda account: {
        "UUID-GMAIL": [
            {"name": "INBOX", "path": "INBOX"},
            {"name": "Spam", "path": "[Gmail]/Spam"},
        ],
        "UUID-OUTLOOK": [{"name": "Junk Email", "path": "Junk Email"}],
        "UUID-IMAP": [{"name": "Junk Mail", "path": "Junk Mail"}],
    }[account]

    assert junk_mailboxes(connector) == [
        JunkMailbox("UUID-GMAIL", "first@gmail.com", "[Gmail]/Spam"),
        JunkMailbox("UUID-OUTLOOK", "second@outlook.com", "Junk Email"),
        JunkMailbox("UUID-IMAP", "third@example.com", "Junk Mail"),
    ]


def test_junk_regression_flagged_message_qualifies_for_immediate_cleanup(tmp_path: Path) -> None:
    """Regression: sender-supplied Junk flags must enter the permanent-delete set."""
    store = JunkCampaignStore(tmp_path / "junk.sqlite3")
    [message] = store.record_messages(
        account="mail@example.com",
        mailbox="Junk Email",
        messages=[
            {
                "id": "flagged-1",
                "rfc_message_id": "flagged-1@example.test",
                "sender": "ordinary@campaign.example",
                "flagged": True,
            }
        ],
    )
    config = JunkCleanerConfig(
        mode="delete",
        minimum_domains=3,
        minimum_messages=3,
        observation_window_days=30,
        maximum_deletions_per_run=25,
        providers={},
        notification_webhook_file=None,
    )

    assert _eligible_messages(
        store,
        [message],
        config,
        flagged_connector_ids={"flagged-1"},
    ) == [message]


def test_junk_regression_clears_flags_before_provider_health(
    tmp_path: Path, monkeypatch: MagicMock
) -> None:
    """Regression: a provider-health failure must occur after visible flags are cleared."""
    connector = MagicMock(spec=AppleMailConnector)
    connector.list_accounts.return_value = [
        {"id": "UUID-OUTLOOK", "name": "mail@example.com", "enabled": True}
    ]
    connector.list_mailboxes.return_value = [{"name": "Junk Email", "path": "Junk Email"}]
    connector.search_messages.side_effect = [
        [
            {
                "id": "flagged-1",
                "rfc_message_id": "flagged-1@example.test",
                "sender": "ordinary@campaign.example",
                "flagged": True,
            }
        ],
        [
            {
                "id": "flagged-1",
                "rfc_message_id": "flagged-1@example.test",
                "sender": "ordinary@campaign.example",
                "flagged": True,
            }
        ],
        [],
    ]
    connector.update_message.return_value = 1

    def fail_after_flag_clear(**_kwargs: object) -> list[dict[str, object]]:
        connector.update_message.assert_called_once()
        raise RuntimeError("health probe crashed")

    monkeypatch.setattr(
        "apple_mail_fast_mcp.junk_cleaner.check_provider_health", fail_after_flag_clear
    )
    config = JunkCleanerConfig(
        mode="delete",
        minimum_domains=3,
        minimum_messages=3,
        observation_window_days=30,
        maximum_deletions_per_run=25,
        providers={},
        notification_webhook_file=None,
    )

    try:
        clean_junk(
            connector,
            config=config,
            database_path=tmp_path / "junk.sqlite3",
            source_home=tmp_path,
        )
    except RuntimeError as exc:
        assert str(exc) == "health probe crashed"
    else:
        raise AssertionError("provider health failure should propagate")


def test_junk_regression_unhealthy_provider_defers_delete_after_unflag(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: one provider outage must defer deletion after completing flag cleanup."""
    connector = MagicMock(spec=AppleMailConnector)
    connector.list_accounts.return_value = [
        {"id": "UUID-OUTLOOK", "name": "mail@outlook.com", "enabled": True}
    ]
    connector.list_mailboxes.return_value = [{"name": "Junk Email", "path": "Junk Email"}]
    flagged = {
        "id": "flagged-1",
        "rfc_message_id": "flagged-1@example.test",
        "sender": "ordinary@campaign.example",
        "flagged": True,
    }
    connector.search_messages.side_effect = [[flagged], [flagged], []]
    connector.update_message.return_value = 1
    monkeypatch.setattr(
        "apple_mail_fast_mcp.junk_cleaner.check_provider_health",
        lambda **_kwargs: [
            {
                "account": "mail@outlook.com",
                "healthy": False,
                "detail": "authentication required",
                "transition": "failed",
            }
        ],
    )
    build_purger = MagicMock()
    monkeypatch.setattr("apple_mail_fast_mcp.junk_cleaner.build_purger", build_purger)
    config = JunkCleanerConfig(
        mode="delete",
        minimum_domains=3,
        minimum_messages=3,
        observation_window_days=30,
        maximum_deletions_per_run=25,
        providers={"mail@outlook.com": ProviderAccount(kind="microsoft", credential="personal")},
        notification_webhook_file=None,
    )

    result = clean_junk(
        connector,
        config=config,
        database_path=tmp_path / "junk.sqlite3",
        source_home=tmp_path,
    )

    connector.update_message.assert_called_once()
    build_purger.assert_not_called()
    assert result["mailboxes"][0]["flags_cleared"] == 1
    assert result["mailboxes"][0]["deferred"] == 1
