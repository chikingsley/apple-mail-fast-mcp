"""Regressions for narrow provider-native permanent deletion adapters."""

import json
from pathlib import Path

from apple_mail_fast_mcp.junk_providers import GmailPurger, MicrosoftPurger


class RecordingRunner:
    """Record provider argv while returning a fixed sequence of JSON responses."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    def __call__(self, arguments, *, environment=None) -> str:
        self.calls.append((tuple(arguments), environment))
        return next(self.responses)


def test_junk_regression_gmail_resolves_inside_spam_before_permanent_delete() -> None:
    """Regression: Gmail deletion must resolve an RFC id inside Spam before mutation."""
    runner = RecordingRunner([json.dumps({"messages": [{"id": "gmail-1"}]}), "{}"])
    purger = GmailPurger(account="mail@gmail.com", gog_path="/usr/bin/gog", runner=runner)

    assert purger.permanently_delete("rfc@example.test") == 1

    list_call, delete_call = (call[0] for call in runner.calls)
    list_params = json.loads(list_call[list_call.index("--params") + 1])
    assert list_params["q"] == "in:spam rfc822msgid:rfc@example.test"
    assert "--results-only" not in list_call
    assert "users.messages.delete" in delete_call
    assert "--allow-write" in delete_call
    assert "gmail-1" in delete_call[delete_call.index("--params") + 1]


def test_junk_regression_gmail_resolution_is_read_only() -> None:
    """Regression: provider capability checks can resolve Spam without issuing deletion."""
    runner = RecordingRunner([json.dumps({"messages": [{"id": "gmail-1"}]})])
    purger = GmailPurger(account="mail@gmail.com", gog_path="/usr/bin/gog", runner=runner)

    assert purger.resolve_message_ids("rfc@example.test") == ["gmail-1"]
    assert len(runner.calls) == 1
    assert "users.messages.list" in runner.calls[0][0]


def test_junk_regression_gmail_health_check_reads_expected_profile() -> None:
    """Regression: Gmail credential expiry must surface before a deletion is attempted."""
    runner = RecordingRunner([json.dumps({"emailAddress": "mail@gmail.com"})])
    purger = GmailPurger(account="mail@gmail.com", gog_path="/usr/bin/gog", runner=runner)

    purger.check_health()

    assert "users.getProfile" in runner.calls[0][0]
    assert "--no-input" in runner.calls[0][0]


def test_junk_regression_microsoft_restores_active_named_connection(tmp_path: Path) -> None:
    """Regression: a scheduled delete must restore the user's active M365 connection."""
    connections = [
        {"name": "personal", "active": False, "accessTokens": {}},
        {"name": "work", "active": True, "accessTokens": {}},
    ]
    runner = RecordingRunner(
        [json.dumps(connections), "", json.dumps({"value": [{"id": "graph-1"}]}), "", ""]
    )
    purger = MicrosoftPurger(
        connection_name="personal",
        m365_path="/usr/bin/m365",
        source_home=tmp_path,
        runner=runner,
    )

    assert purger.permanently_delete("rfc@example.test") == 1

    inventory_call, select_call, list_call, delete_call, restore_call = runner.calls
    assert inventory_call[0][1:3] == ("connection", "list")
    assert select_call[0][select_call[0].index("--name") + 1] == "personal"
    assert "mailFolders/junkemail/messages" in list_call[0][3]
    assert "graph-1/permanentDelete" in delete_call[0][3]
    assert restore_call[0][restore_call[0].index("--name") + 1] == "work"


def test_junk_regression_microsoft_health_check_uses_named_connection(tmp_path: Path) -> None:
    """Regression: Microsoft token expiry must surface before a deletion is attempted."""
    connections = [
        {"name": "personal", "active": False},
        {"name": "work", "active": True},
    ]
    profile = {"userPrincipalName": "mail@outlook.com", "mail": "mail@outlook.com"}
    runner = RecordingRunner([json.dumps(connections), "", json.dumps(profile), ""])
    purger = MicrosoftPurger(
        connection_name="personal",
        m365_path="/usr/bin/m365",
        source_home=tmp_path,
        runner=runner,
    )

    purger.check_health(expected_email="mail@outlook.com")

    _, select_call, profile_call, restore_call = runner.calls
    assert select_call[0][select_call[0].index("--name") + 1] == "personal"
    assert profile_call[0][3].endswith("?$select=userPrincipalName,mail")
    assert restore_call[0][restore_call[0].index("--name") + 1] == "work"
