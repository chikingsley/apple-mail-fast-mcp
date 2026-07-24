"""Regression coverage for the #376 local metadata accelerator."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_fast_mcp.exceptions import MailKeychainEntryNotFoundError
from apple_mail_fast_mcp.local_db_connector import (
    LocalDbConnector,
    LocalDbUnsupportedQueryError,
)
from apple_mail_fast_mcp.mail_connector import AppleMailConnector


def _create_envelope_index(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
        CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE message_global_data (message_id INTEGER, message_id_header TEXT);
        CREATE TABLE messages (
            ROWID INTEGER PRIMARY KEY,
            message_id INTEGER,
            subject INTEGER,
            sender INTEGER,
            mailbox INTEGER,
            date_received INTEGER,
            read INTEGER,
            flagged INTEGER,
            deleted INTEGER
        );
        INSERT INTO subjects VALUES (1, 'Build completed'), (2, 'Older receipt');
        INSERT INTO addresses VALUES
            (1, 'no_reply@email.apple.com', 'App Store Connect'),
            (2, 'billing@example.com', 'Billing');
        INSERT INTO mailboxes VALUES
            (1, 'imap://ACCOUNT-UUID/INBOX'),
            (2, 'imap://ACCOUNT-UUID/%5BGmail%5D/Spam'),
            (3, 'imap://OTHER-UUID/INBOX');
        INSERT INTO message_global_data VALUES
            (101, '<build@example.com>'),
            (102, '<receipt@example.com>'),
            (103, '<other@example.com>');
        """
    )
    recent = int(datetime(2026, 7, 21, 12, tzinfo=UTC).timestamp())
    older = int(datetime(2026, 6, 1, 12, tzinfo=UTC).timestamp())
    connection.executemany(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 101, 1, 1, 1, recent, 0, 1, 0),
            (2, 102, 2, 2, 2, older, 1, 0, 0),
            (3, 103, 1, 1, 3, recent, 0, 0, 0),
        ],
    )
    connection.commit()
    connection.close()


def _search(connector: LocalDbConnector, **overrides: object) -> list[dict[str, object]]:
    arguments: dict[str, object] = {
        "account_id": "ACCOUNT-UUID",
        "mailbox": "INBOX",
        "sender_contains": None,
        "subject_contains": None,
        "read_status": None,
        "is_flagged": None,
        "date_from": None,
        "date_to": None,
        "received_within_hours": None,
        "has_attachment": None,
        "limit": 50,
        "include_attachments": False,
        "body_contains": None,
        "text_contains": None,
    }
    arguments.update(overrides)
    return connector.search_messages(**arguments)  # type: ignore[arg-type]


def test_issue_376_local_db_filters_account_mailbox_and_metadata(tmp_path: Path) -> None:
    """#376: metadata search must preserve account and mailbox boundaries."""
    index = tmp_path / "Envelope Index"
    _create_envelope_index(index)

    rows = _search(
        LocalDbConnector(index),
        sender_contains="EMAIL.APPLE",
        subject_contains="completed",
        read_status=False,
        is_flagged=True,
        date_from="2026-07-01",
        date_to="2026-07-21",
    )

    assert rows == [
        {
            "id": "101",
            "rfc_message_id": "build@example.com",
            "subject": "Build completed",
            "sender": "App Store Connect <no_reply@email.apple.com>",
            "date_received": "2026-07-21T12:00:00+00:00",
            "read_status": False,
            "flagged": True,
        }
    ]


def test_issue_376_local_db_matches_encoded_nested_mailbox(tmp_path: Path) -> None:
    """#376: Gmail's encoded nested mailbox URLs must match public paths."""
    index = tmp_path / "Envelope Index"
    _create_envelope_index(index)

    rows = _search(LocalDbConnector(index), mailbox="[Gmail]/Spam")

    assert [row["id"] for row in rows] == ["102"]


def test_issue_376_local_db_defers_content_queries(tmp_path: Path) -> None:
    """#376: content-dependent searches must fall through to IMAP or AppleScript."""
    index = tmp_path / "Envelope Index"
    _create_envelope_index(index)

    with pytest.raises(LocalDbUnsupportedQueryError):
        _search(LocalDbConnector(index), body_contains="approval")


def test_issue_376_mail_connector_uses_local_db_before_applescript() -> None:
    """#376: a missing IMAP credential routes metadata search through the local index."""
    local_db = MagicMock(spec=LocalDbConnector)
    local_db.search_messages.return_value = [{"id": "42", "subject": "Fast"}]
    connector = AppleMailConnector(local_db=local_db)

    with (
        patch.object(
            connector,
            "_imap_search",
            side_effect=MailKeychainEntryNotFoundError("missing"),
        ),
        patch.object(
            connector,
            "list_accounts",
            return_value=[{"name": "Gmail", "id": "account-uuid"}],
        ),
        patch.object(connector, "_search_messages_applescript") as applescript_search,
    ):
        result = connector.search_messages(
            "Gmail",
            subject_contains="Fast",
            read_status=False,
            limit=10,
        )

    assert result == [{"id": "42", "subject": "Fast"}]
    local_db.search_messages.assert_called_once_with(
        account_id="account-uuid",
        mailbox="INBOX",
        sender_contains=None,
        subject_contains="Fast",
        read_status=False,
        is_flagged=None,
        date_from=None,
        date_to=None,
        received_within_hours=None,
        has_attachment=None,
        limit=10,
        include_attachments=False,
        body_contains=None,
        text_contains=None,
    )
    applescript_search.assert_not_called()
