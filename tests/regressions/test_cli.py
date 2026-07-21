"""Tests for the apple-mail-fast-mcp CLI entry point and setup-imap subcommand."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from imapclient.exceptions import LoginError

from apple_mail_fast_mcp.cli import run_setup_imap

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_accounts() -> list[dict[str, Any]]:
    return [
        {
            "id": "UUID-ICLOUD",
            "name": "iCloud",
            "email_addresses": ["alice@icloud.com"],
            "account_type": "imap",
            "enabled": True,
        },
        {
            "id": "UUID-GMAIL",
            "name": "Gmail",
            "email_addresses": ["alice@gmail.com"],
            "account_type": "imap",
            "enabled": True,
        },
    ]


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch) -> None:
    """Point APPLE_MAIL_MCP_HOME at a temp dir so setup-imap's login-override
    writes (#341) never touch the developer's real ~/.apple_mail_mcp.
    """
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))


@pytest.fixture
def mock_connector() -> MagicMock:
    """Stand-in for AppleMailConnector — list_accounts + _resolve_imap_config."""
    m = MagicMock()
    m.list_accounts.return_value = _make_accounts()
    m._resolve_imap_config.return_value = (
        "imap.mail.me.com",
        993,
        "alice@icloud.com",
    )
    return m


@pytest.fixture
def mock_imap_client() -> MagicMock:
    """Stand-in for ImapConnector. Default: search_messages succeeds."""
    m = MagicMock()
    m.search_messages.return_value = []
    return m


# ---------------------------------------------------------------------------
# Account validation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Happy path — setup
# ---------------------------------------------------------------------------


class TestSetupHappyPath:
    def test_cli_email_override_wins_for_keychain_and_login(
        self,
        mock_connector: MagicMock,
        mock_imap_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When --email is supplied, it overrides _resolve_imap_config's
        result for BOTH the keychain key AND the IMAP LOGIN. Pre-#201 the
        login silently switched back to the resolver's value, which is
        what caused the custom-domain Apple ID failures: the user passed
        the right Apple ID but the resolver swapped in an SMTP-only From
        alias the IMAP server rejected. (#201)
        """
        from apple_mail_fast_mcp import cli as cli_mod

        set_calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            cli_mod,
            "set_imap_password",
            lambda a, e, p: set_calls.append((a, e, p)),
        )

        # Resolver returns one value; user explicitly passes another.
        mock_connector._resolve_imap_config.return_value = (
            "imap.mail.me.com",
            993,
            "from-alias@example.com",
        )

        captured: list[tuple[str, int, str, str]] = []

        def factory(h: str, p: int, e: str, pw: str) -> MagicMock:
            captured.append((h, p, e, pw))
            return mock_imap_client

        rc = run_setup_imap(
            account_name="iCloud",
            cli_email="apple-id@example.com",
            uninstall=False,
            connector_factory=lambda: mock_connector,
            getpass_fn=lambda prompt: "pw",
            imap_factory=factory,
        )
        assert rc == 0
        # Keychain key uses the CLI override.
        assert set_calls == [
            ("iCloud", "apple-id@example.com", "pw"),
        ]
        # IMAP LOGIN uses the same CLI override — not the resolver's value.
        assert captured == [
            ("imap.mail.me.com", 993, "apple-id@example.com", "pw"),
        ]


# ---------------------------------------------------------------------------
# Setup — failure paths
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Uninstall path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# argparse dispatch in server.main()
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Login override persistence (#341)
# ---------------------------------------------------------------------------


class TestLoginOverride:
    """setup-imap --email must persist a login override so runtime
    resolution uses the verified login (#341). Override writes are isolated
    to a temp APPLE_MAIL_MCP_HOME by the autouse _isolate_home fixture.
    """

    def test_icloud_login_failure_hints_at_email_flag(
        self,
        mock_connector: MagicMock,
        mock_imap_client: MagicMock,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The #341 shape: iCloud host, non-Apple login, no --email, login
        rejected → the error suggests re-running with --email.
        """
        from apple_mail_fast_mcp import cli as cli_mod

        monkeypatch.setattr(cli_mod, "set_imap_password", lambda a, e, p: None)
        monkeypatch.setattr(cli_mod, "delete_imap_password", lambda a, e: None)
        mock_connector._resolve_imap_config.return_value = (
            "p42-imap.mail.me.com",
            993,
            "someone@gmail.com",
        )
        mock_imap_client.search_messages.side_effect = LoginError("AUTHENTICATIONFAILED")
        rc = run_setup_imap(
            account_name="iCloud",
            cli_email=None,
            uninstall=False,
            connector_factory=lambda: mock_connector,
            getpass_fn=lambda prompt: "pw",
            imap_factory=lambda *a, **k: mock_imap_client,
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "--email" in err and "icloud.com" in err.lower()


# ---------------------------------------------------------------------------
# setup-imap --host / --port server overrides (#405)
# ---------------------------------------------------------------------------


def _ok_imap_capture(captured: dict[str, Any]):
    """imap_factory that records the (host, port) it was constructed with and
    verifies successfully.
    """

    def _factory(h: str, p: int, e: str, pw: str) -> MagicMock:
        captured["host"], captured["port"] = h, p
        m = MagicMock()
        m.search_messages.return_value = []
        return m

    return _factory
