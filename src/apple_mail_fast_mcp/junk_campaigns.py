"""Auditable sender-campaign evidence for messages already classified as junk."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parseaddr
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path


@dataclass(frozen=True)
class SenderFingerprint:
    """Normalized sender identity used to correlate rotating-domain campaigns."""

    local_part: str
    domain: str


@dataclass(frozen=True)
class JunkMessage:
    """The deletion-safe metadata retained from one current Junk/Spam message."""

    account: str
    mailbox: str
    connector_id: str
    rfc_message_id: str
    fingerprint: SenderFingerprint


def fingerprint_sender(sender: str) -> SenderFingerprint | None:
    """Extract a lowercase ``local-part@domain`` fingerprint from ``sender``."""
    _, address = parseaddr(sender)
    local_part, separator, domain = address.rpartition("@")
    local_part = local_part.strip().lower()
    domain = domain.strip().lower().rstrip(".")
    if separator != "@" or not local_part or "." not in domain:
        return None
    return SenderFingerprint(local_part=local_part, domain=domain)


def looks_generated_local_part(local_part: str) -> bool:
    """Recognize machine-generated mailbox names with conservative shape rules."""
    length = len(local_part)
    digits = sum(character.isdigit() for character in local_part)
    separators = sum(not character.isalnum() for character in local_part)
    letters = sum(character.isalpha() for character in local_part)
    return (
        (length >= 8 and digits == length)
        or (length >= 10 and digits >= 4 and separators >= 1)
        or (length >= 16 and digits >= 6 and letters >= 3)
    )


def message_evidence(
    *, account: str, mailbox: str, message: dict[str, object]
) -> JunkMessage | None:
    """Convert a connector row into metadata suitable for permanent deletion."""
    connector_id = message.get("id")
    rfc_message_id = message.get("rfc_message_id")
    sender = message.get("sender")
    if not isinstance(connector_id, str) or not connector_id.strip():
        return None
    if not isinstance(sender, str) or not sender.strip():
        return None
    if not isinstance(rfc_message_id, str) or not rfc_message_id.strip():
        return None
    fingerprint = fingerprint_sender(sender)
    if fingerprint is None:
        return None
    return JunkMessage(
        account=account,
        mailbox=mailbox,
        connector_id=connector_id,
        rfc_message_id=rfc_message_id.strip().strip("<>"),
        fingerprint=fingerprint,
    )


class JunkCampaignStore:
    """SQLite evidence and action ledger for the scheduled junk cleaner."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            self._create_schema(connection)
            self._migrate_observations(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS junk_campaign_observation (
                account TEXT NOT NULL,
                mailbox TEXT NOT NULL,
                message_id TEXT NOT NULL,
                local_part TEXT NOT NULL,
                domain TEXT NOT NULL,
                rfc_message_id TEXT,
                was_flagged INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (account, mailbox, message_id)
            );
            CREATE INDEX IF NOT EXISTS junk_campaign_local_part_idx
                ON junk_campaign_observation (account, local_part, domain);
            CREATE TABLE IF NOT EXISTS junk_auto_delete_domain (
                domain TEXT PRIMARY KEY,
                source_account TEXT NOT NULL,
                source_mailbox TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                first_flagged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_flagged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS junk_cleanup_action (
                account TEXT NOT NULL,
                mailbox TEXT NOT NULL,
                rfc_message_id TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (account, mailbox, rfc_message_id, action)
            );
            CREATE TABLE IF NOT EXISTS provider_health (
                account TEXT PRIMARY KEY,
                healthy INTEGER NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    @staticmethod
    def _migrate_observations(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(junk_campaign_observation)")
        }
        for name, declaration in (
            ("rfc_message_id", "TEXT"),
            ("was_flagged", "INTEGER NOT NULL DEFAULT 0"),
            ("last_seen_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE junk_campaign_observation ADD COLUMN {name} {declaration}"
                )
        connection.execute(
            """
            INSERT OR IGNORE INTO junk_auto_delete_domain
                (domain, source_account, source_mailbox, source_message_id)
            SELECT domain, account, mailbox, message_id
            FROM junk_campaign_observation
            WHERE was_flagged = 1
            """
        )

    def record_messages(
        self,
        *,
        account: str,
        mailbox: str,
        messages: Iterable[dict[str, object]],
    ) -> list[JunkMessage]:
        """Persist valid current-message evidence and return the normalized rows."""
        message_rows = list(messages)
        messages_by_id = {
            message_id: message
            for message in message_rows
            if isinstance(message_id := message.get("id"), str)
        }
        evidence = [
            normalized
            for message in message_rows
            if (normalized := message_evidence(account=account, mailbox=mailbox, message=message))
            is not None
        ]
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO junk_campaign_observation
                    (
                        account, mailbox, message_id, rfc_message_id,
                        local_part, domain, was_flagged, last_seen_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (account, mailbox, message_id) DO UPDATE SET
                    rfc_message_id = excluded.rfc_message_id,
                    local_part = excluded.local_part,
                    domain = excluded.domain,
                    was_flagged = MAX(was_flagged, excluded.was_flagged),
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                [
                    (
                        item.account,
                        item.mailbox,
                        item.connector_id,
                        item.rfc_message_id,
                        item.fingerprint.local_part,
                        item.fingerprint.domain,
                        int(messages_by_id[item.connector_id].get("flagged") is True),
                    )
                    for item in evidence
                ],
            )
            connection.executemany(
                """
                INSERT INTO junk_auto_delete_domain
                    (domain, source_account, source_mailbox, source_message_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (domain) DO UPDATE SET
                    last_flagged_at = CURRENT_TIMESTAMP
                """,
                [
                    (
                        item.fingerprint.domain,
                        item.account,
                        item.mailbox,
                        item.connector_id,
                    )
                    for item in evidence
                    if messages_by_id[item.connector_id].get("flagged") is True
                ],
            )
        return evidence

    def auto_delete_domains(self) -> set[str]:
        """Return sender domains learned permanently from auto-flagged junk."""
        with self._connection() as connection:
            rows = connection.execute("SELECT domain FROM junk_auto_delete_domain").fetchall()
        return {str(row[0]) for row in rows}

    def qualified_local_parts(
        self,
        *,
        account: str,
        minimum_domains: int,
        minimum_messages: int,
        observation_window_days: int,
    ) -> set[str]:
        """Return generated local parts backed by enough distinct junk evidence."""
        window = f"-{observation_window_days} days"
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT local_part
                FROM junk_campaign_observation
                WHERE account = ? AND last_seen_at >= datetime('now', ?)
                GROUP BY local_part
                HAVING COUNT(DISTINCT domain) >= ? AND COUNT(*) >= ?
                """,
                (account, window, minimum_domains, minimum_messages),
            ).fetchall()
        return {str(row[0]) for row in rows if looks_generated_local_part(str(row[0]))}

    def was_deleted(self, message: JunkMessage) -> bool:
        """Return whether this exact provider message already completed deletion."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM junk_cleanup_action
                WHERE account = ? AND mailbox = ? AND rfc_message_id = ?
                    AND action = 'permanent_delete' AND status = 'deleted'
                """,
                (message.account, message.mailbox, message.rfc_message_id),
            ).fetchone()
        return row is not None

    def record_action(self, message: JunkMessage, *, status: str, detail: str = "") -> None:
        """Upsert the audit result for one permanent-delete decision."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO junk_cleanup_action
                    (account, mailbox, rfc_message_id, action, status, detail)
                VALUES (?, ?, ?, 'permanent_delete', ?, ?)
                ON CONFLICT (account, mailbox, rfc_message_id, action) DO UPDATE SET
                    status = excluded.status,
                    detail = excluded.detail,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (message.account, message.mailbox, message.rfc_message_id, status, detail[:500]),
            )

    def record_provider_health(
        self, *, account: str, healthy: bool, detail: str = ""
    ) -> bool | None:
        """Record provider health and return its previous state, when known."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT healthy FROM provider_health WHERE account = ?", (account,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO provider_health (account, healthy, detail)
                VALUES (?, ?, ?)
                ON CONFLICT (account) DO UPDATE SET
                    healthy = excluded.healthy,
                    detail = excluded.detail,
                    checked_at = CURRENT_TIMESTAMP
                """,
                (account, int(healthy), detail[:500]),
            )
        return bool(row[0]) if row is not None else None
