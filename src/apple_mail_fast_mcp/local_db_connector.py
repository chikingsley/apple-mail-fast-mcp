"""Read-only metadata search against Apple Mail's Envelope Index."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote


class LocalDbUnavailableError(RuntimeError):
    """Apple Mail's local metadata database is unavailable or incompatible."""


class LocalDbUnsupportedQueryError(RuntimeError):
    """A query requires content Apple Mail's metadata database does not expose."""


_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "messages": {
        "message_id",
        "subject",
        "sender",
        "mailbox",
        "date_received",
        "read",
        "flagged",
        "deleted",
    },
    "subjects": {"subject"},
    "addresses": {"address", "comment"},
    "mailboxes": {"url"},
    "message_global_data": {"message_id", "message_id_header"},
}

_BASE_SQL = """
SELECT m.message_id AS id,
       g.message_id_header AS rfc,
       s.subject AS subject,
       a.address AS sender,
       a.comment AS sender_name,
       m.date_received AS date_received,
       m.read AS read,
       m.flagged AS flagged
FROM messages AS m
LEFT JOIN subjects AS s ON s.ROWID = m.subject
LEFT JOIN addresses AS a ON a.ROWID = m.sender
LEFT JOIN mailboxes AS mb ON mb.ROWID = m.mailbox
LEFT JOIN message_global_data AS g ON g.message_id = m.message_id
WHERE m.deleted = 0
"""


def _version_key(path: Path) -> int:
    try:
        return int(path.parent.parent.name.removeprefix("V"))
    except ValueError:
        return -1


def discover_envelope_index() -> Path:
    """Return the newest readable Apple Mail Envelope Index path."""
    mail_root = Path.home() / "Library" / "Mail"
    try:
        candidates = sorted(
            mail_root.glob("V*/MailData/Envelope Index"),
            key=_version_key,
        )
    except OSError as exc:
        raise LocalDbUnavailableError(
            "Apple Mail metadata access requires Full Disk Access for the MCP service"
        ) from exc
    if not candidates:
        raise LocalDbUnavailableError(
            "Apple Mail Envelope Index is unavailable; grant Full Disk Access to the MCP service"
        )
    return candidates[-1]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _mailbox_url_patterns(account_id: str, mailbox: str) -> list[str]:
    account_prefix = f"%://{_escape_like(account_id.lower())}/"
    variants = {
        mailbox,
        quote(mailbox, safe="/"),
        quote(mailbox, safe="/[]"),
    }
    return [f"{account_prefix}%{_escape_like(value.lower())}" for value in variants]


def _format_sender(name: str | None, address: str | None) -> str:
    clean_name = (name or "").strip()
    clean_address = (address or "").strip()
    if clean_name and clean_address:
        return f"{clean_name} <{clean_address}>"
    return clean_address or clean_name


def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    timestamp = row["date_received"]
    received = (
        datetime.fromtimestamp(timestamp, tz=UTC).isoformat()
        if isinstance(timestamp, int | float) and timestamp > 0
        else ""
    )
    rfc_id = str(row["rfc"] or "").strip().strip("<>") or None
    return {
        "id": str(row["id"]),
        "rfc_message_id": rfc_id,
        "subject": str(row["subject"] or ""),
        "sender": _format_sender(row["sender_name"], row["sender"]),
        "date_received": received,
        "read_status": bool(row["read"]),
        "flagged": bool(row["flagged"]),
    }


class LocalDbConnector:
    """Read Apple Mail metadata from its live SQLite database in read-only mode."""

    def __init__(self, index_path: Path | None = None) -> None:
        self._index_path = index_path
        self._schema_checked = False

    @property
    def index_path(self) -> Path:
        if self._index_path is None:
            self._index_path = discover_envelope_index()
        return self._index_path

    def _connect(self) -> sqlite3.Connection:
        path = self.index_path
        try:
            path_stat = path.lstat()
        except OSError as exc:
            raise LocalDbUnavailableError(f"Cannot inspect Apple Mail database: {path}") from exc
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise LocalDbUnavailableError(f"Apple Mail database must be a regular file: {path}")
        if path_stat.st_uid != os.getuid():
            raise LocalDbUnavailableError(f"Apple Mail database has a foreign owner: {path}")

        try:
            connection = sqlite3.connect(
                f"file:{quote(str(path))}?mode=ro",
                uri=True,
                timeout=1,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            if not self._schema_checked:
                self._check_schema(connection)
                self._schema_checked = True
            return connection
        except sqlite3.Error as exc:
            raise LocalDbUnavailableError(f"Cannot open Apple Mail database: {exc}") from exc

    def _check_schema(self, connection: sqlite3.Connection) -> None:
        for table, expected in _REQUIRED_COLUMNS.items():
            try:
                actual = {
                    str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")')
                }
            except sqlite3.Error as exc:
                raise LocalDbUnavailableError(
                    f"Cannot inspect Apple Mail table {table!r}: {exc}"
                ) from exc
            missing = expected - actual
            if missing:
                names = ", ".join(sorted(missing))
                raise LocalDbUnavailableError(
                    f"Apple Mail database schema is missing {table}.{names}"
                )

    def search_messages(
        self,
        *,
        account_id: str,
        mailbox: str,
        sender_contains: str | None,
        subject_contains: str | None,
        read_status: bool | None,
        is_flagged: bool | None,
        date_from: str | None,
        date_to: str | None,
        received_within_hours: int | None,
        has_attachment: bool | None,
        limit: int | None,
        include_attachments: bool,
        body_contains: str | None,
        text_contains: str | None,
    ) -> list[dict[str, Any]]:
        """Search metadata while preserving the connector's common row shape."""
        if has_attachment is not None or include_attachments or body_contains or text_contains:
            raise LocalDbUnsupportedQueryError("The query requires message content")

        patterns = _mailbox_url_patterns(account_id, mailbox)
        sql = _BASE_SQL
        params: list[Any] = []
        mailbox_clauses: list[str] = []
        for pattern in patterns:
            mailbox_clauses.append("LOWER(mb.url) LIKE ? ESCAPE '\\'")
            params.append(pattern)
        sql += f" AND ({' OR '.join(mailbox_clauses)})"

        if sender_contains:
            sql += " AND LOWER(a.address) LIKE ? ESCAPE '\\'"
            params.append(f"%{_escape_like(sender_contains.lower())}%")
        if subject_contains:
            sql += " AND LOWER(s.subject) LIKE ? ESCAPE '\\'"
            params.append(f"%{_escape_like(subject_contains.lower())}%")
        if read_status is not None:
            sql += " AND m.read = ?"
            params.append(int(read_status))
        if is_flagged is not None:
            sql += " AND m.flagged = ?"
            params.append(int(is_flagged))

        lower_bound: datetime | None = None
        if date_from:
            lower_bound = datetime.combine(date.fromisoformat(date_from), datetime.min.time(), UTC)
        if received_within_hours is not None:
            relative = datetime.now(tz=UTC) - timedelta(hours=received_within_hours)
            if lower_bound is None or relative > lower_bound:
                lower_bound = relative
        if lower_bound is not None:
            sql += " AND m.date_received >= ?"
            params.append(int(lower_bound.timestamp()))
        if date_to:
            upper_bound = datetime.combine(
                date.fromisoformat(date_to) + timedelta(days=1),
                datetime.min.time(),
                UTC,
            )
            sql += " AND m.date_received < ?"
            params.append(int(upper_bound.timestamp()))

        sql += " ORDER BY m.date_received DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        try:
            with closing(self._connect()) as connection:
                return [_row_to_message(row) for row in connection.execute(sql, params)]
        except sqlite3.Error as exc:
            raise LocalDbUnavailableError(f"Apple Mail metadata query failed: {exc}") from exc
