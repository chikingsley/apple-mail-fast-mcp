"""Fast read-only Mail metadata and a durable Junk audit ledger."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote

from .bridge import query
from .campaigns import (
    DEFAULT_POLICY,
    CampaignPolicy,
    fingerprint_sender,
    normalize_display_name,
    normalize_sender_email,
    normalize_subject,
)

if TYPE_CHECKING:
    from .mail import Mail

CHECKOUT = Path(__file__).resolve().parents[3]
LEDGER_PATH = Path.home() / ".config/apple-mail-fast-mcp/junk.sqlite"
CAMPAIGN_BATCH_SIZE = 25
PUBLIC_EMAIL_DOMAINS = frozenset(
    {"aol.com", "gmail.com", "hotmail.com", "icloud.com", "live.com", "outlook.com", "yahoo.com"}
)
GENERIC_LOCAL_PARTS = frozenset(
    {
        "admin",
        "billing",
        "contact",
        "hello",
        "help",
        "info",
        "mail",
        "market",
        "marketing",
        "news",
        "no-reply",
        "noreply",
        "notifications",
        "sales",
        "service",
        "support",
        "team",
    }
)
APPROVED_CAMPAIGN_ACTION = "deleted_reviewed_junk"

BASE_SQL = """
SELECT m.ROWID AS id, COALESCE(g.message_id_header, '') AS message_id,
       COALESCE(s.subject, '') AS subject, COALESCE(a.address, '') AS sender_address,
       COALESCE(a.comment, '') AS sender_name, m.date_received AS received,
       m.read AS read, m.flagged AS flagged, mb.url AS mailbox_url
FROM messages m
JOIN mailboxes mb ON mb.ROWID = m.mailbox
LEFT JOIN subjects s ON s.ROWID = m.subject
LEFT JOIN addresses a ON a.ROWID = m.sender
LEFT JOIN message_global_data g ON g.message_id = m.message_id
WHERE m.deleted = 0
"""

QUALIFICATION_SQL = """WITH recent AS (
  SELECT * FROM message WHERE unixepoch(last_seen) >= unixepoch('now', ?)
), campaigns AS (
  SELECT 'rotating_local_part' signal, account_id, local_part campaign_key,
         COUNT(*) messages, COUNT(DISTINCT domain) domains
  FROM recent WHERE local_part != '' AND domain != '' GROUP BY account_id, local_part
  HAVING COUNT(*) >= ? AND COUNT(DISTINCT domain) >= ?
  UNION ALL
  SELECT 'rotating_display_name', account_id, display_name, COUNT(*), COUNT(DISTINCT domain)
  FROM recent WHERE display_name != '' AND domain != '' GROUP BY account_id, display_name
  HAVING COUNT(*) >= ? AND COUNT(DISTINCT domain) >= ?
  UNION ALL
  SELECT 'rotating_subject', account_id, subject_key, COUNT(*), COUNT(DISTINCT domain)
  FROM recent WHERE subject_key != '' AND domain != '' GROUP BY account_id, subject_key
  HAVING COUNT(*) >= ? AND COUNT(DISTINCT domain) >= ?
), evidence AS (
  SELECT m.account_id, m.mailbox, m.mail_id, m.message_id, c.signal reason
  FROM message m JOIN campaigns c ON c.account_id=m.account_id AND (
    (c.signal='rotating_local_part' AND c.campaign_key=m.local_part) OR
    (c.signal='rotating_display_name' AND c.campaign_key=m.display_name) OR
    (c.signal='rotating_subject' AND c.campaign_key=m.subject_key)
  ) WHERE m.last_seen=?
  UNION ALL
  SELECT m.account_id, m.mailbox, m.mail_id, m.message_id, 'previously_flagged_domain'
  FROM message m JOIN flagged_domain f ON f.domain=m.domain WHERE m.last_seen=?
)
SELECT account_id, mailbox, mail_id, message_id, GROUP_CONCAT(DISTINCT reason) reasons
FROM evidence GROUP BY account_id, mailbox, mail_id, message_id"""

CAMPAIGN_REPORT_SQL = """WITH recent AS (
  SELECT * FROM message WHERE unixepoch(last_seen) >= unixepoch('now', ?)
), campaigns AS (
  SELECT 'rotating_local_part' signal, account_id, local_part campaign_key,
         COUNT(*) messages, COUNT(DISTINCT domain) domains
  FROM recent WHERE local_part != '' AND domain != '' GROUP BY account_id, local_part
  HAVING COUNT(*) >= ? AND COUNT(DISTINCT domain) >= ?
  UNION ALL
  SELECT 'rotating_display_name', account_id, display_name, COUNT(*), COUNT(DISTINCT domain)
  FROM recent WHERE display_name != '' AND domain != '' GROUP BY account_id, display_name
  HAVING COUNT(*) >= ? AND COUNT(DISTINCT domain) >= ?
  UNION ALL
  SELECT 'rotating_subject', account_id, subject_key, COUNT(*), COUNT(DISTINCT domain)
  FROM recent WHERE subject_key != '' AND domain != '' GROUP BY account_id, subject_key
  HAVING COUNT(*) >= ? AND COUNT(DISTINCT domain) >= ?
)
SELECT * FROM campaigns ORDER BY messages DESC, domains DESC"""


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _mailbox_patterns(account_id: str, mailbox: str) -> list[str]:
    prefix = f"%://{_escape_like(account_id.lower())}/"
    variants = {mailbox, quote(mailbox, safe="/"), quote(mailbox, safe="/[]")}
    return [f"{prefix}%{_escape_like(value.lower())}" for value in variants]


def _identity(url: str) -> tuple[str, str]:
    location = url.split("://", 1)[-1]
    account_id, _, path = location.partition("/")
    return account_id, unquote(path)


def _message(row: dict[str, object]) -> dict[str, Any]:
    name = str(row.get("sender_name") or "").strip()
    address = str(row.get("sender_address") or "").strip()
    sender = f"{name} <{address}>" if name and address else address or name
    received = row.get("received")
    timestamp = int(received) if isinstance(received, int | float | str) else 0
    account_id, mailbox = _identity(str(row["mailbox_url"]))
    return {
        "id": str(row["id"]),
        "message_id": str(row.get("message_id") or "").strip().strip("<>"),
        "subject": str(row.get("subject") or ""),
        "sender": sender,
        "received": datetime.fromtimestamp(timestamp, tz=UTC).isoformat() if timestamp else "",
        "read": bool(row.get("read")),
        "flagged": bool(row.get("flagged")),
        "account_id": account_id,
        "mailbox": mailbox,
    }


class MailIndex:
    """Query the live Envelope Index through its Full-Disk-Access helper."""

    def search(
        self,
        account_id: str,
        mailbox: str,
        *,
        sender: str | None = None,
        subject: str | None = None,
        unread: bool | None = None,
        flagged: bool | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses = []
        parameters: list[str] = []
        for pattern in _mailbox_patterns(account_id, mailbox):
            clauses.append("LOWER(mb.url) LIKE ? ESCAPE '\\'")
            parameters.append(pattern)
        sql = BASE_SQL + f" AND ({' OR '.join(clauses)})"
        if sender:
            sql += " AND LOWER(a.address) LIKE ? ESCAPE '\\'"
            parameters.append(f"%{_escape_like(sender.lower())}%")
        if subject:
            sql += " AND LOWER(s.subject) LIKE ? ESCAPE '\\'"
            parameters.append(f"%{_escape_like(subject.lower())}%")
        if unread is not None:
            sql += " AND m.read = ?"
            parameters.append(str(int(not unread)))
        if flagged is not None:
            sql += " AND m.flagged = ?"
            parameters.append(str(int(flagged)))
        sql += " ORDER BY m.date_received DESC LIMIT ?"
        parameters.append(str(max(1, min(limit, 100))))
        return [_message(row) for row in query(sql, parameters)]

    def junk(self, mailboxes: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Read only the exact account/mailbox pairs discovered from Mail.app."""
        if not mailboxes:
            return []
        patterns = [
            pattern
            for mailbox in mailboxes
            for pattern in _mailbox_patterns(mailbox["account_id"], mailbox["mailbox"])
        ]
        clauses = ["LOWER(mb.url) LIKE ? ESCAPE '\\'" for _ in patterns]
        sql = BASE_SQL + f" AND ({' OR '.join(clauses)}) ORDER BY m.date_received DESC"
        return [_message(row) for row in query(sql, patterns)]


class JunkLedger:
    """Record every Junk observation, action, and completed cycle."""

    def __init__(self, path: Path = LEDGER_PATH) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS message (
                    account_id TEXT NOT NULL, mailbox TEXT NOT NULL, mail_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, sender TEXT NOT NULL, subject TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, seen_count INTEGER NOT NULL,
                    flagged INTEGER NOT NULL, last_action TEXT, action_at TEXT,
                    PRIMARY KEY (account_id, mailbox, mail_id)
                );
                CREATE TABLE IF NOT EXISTS cycle (
                    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
                    success INTEGER NOT NULL, scanned INTEGER NOT NULL, new_messages INTEGER NOT NULL,
                    flagged INTEGER NOT NULL, flags_cleared INTEGER NOT NULL,
                    moved_to_trash INTEGER NOT NULL, failed INTEGER NOT NULL, error TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flagged_domain (
                    domain TEXT PRIMARY KEY, source_account_id TEXT NOT NULL,
                    source_mailbox TEXT NOT NULL, source_mail_id TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flagged_sender (
                    email TEXT PRIMARY KEY, source_account_id TEXT NOT NULL,
                    source_mailbox TEXT NOT NULL, source_mail_id TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flag_evidence (
                    account_id TEXT NOT NULL, mailbox TEXT NOT NULL, mail_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, raw_sender TEXT NOT NULL,
                    sender_email TEXT NOT NULL, sender_domain TEXT NOT NULL,
                    subject TEXT NOT NULL, received TEXT NOT NULL,
                    first_observed TEXT NOT NULL, last_observed TEXT NOT NULL,
                    observation_count INTEGER NOT NULL,
                    PRIMARY KEY (account_id, mailbox, mail_id)
                );
                CREATE TABLE IF NOT EXISTS junk_mailbox (
                    account_id TEXT NOT NULL, account_name TEXT NOT NULL, mailbox TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY (account_id, mailbox)
                );
                CREATE TABLE IF NOT EXISTS blacklist_control (
                    id INTEGER PRIMARY KEY CHECK(id = 1), activated_at TEXT
                );
                INSERT OR IGNORE INTO blacklist_control(id, activated_at) VALUES (1, NULL);
                CREATE TABLE IF NOT EXISTS campaign_candidate (
                    account_id TEXT NOT NULL, mailbox TEXT NOT NULL, mail_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, reasons TEXT NOT NULL,
                    first_qualified TEXT NOT NULL, last_qualified TEXT NOT NULL,
                    qualification_count INTEGER NOT NULL, status TEXT NOT NULL,
                    action_at TEXT,
                    PRIMARY KEY (account_id, mailbox, mail_id)
                );
            """)
            self._add_column(connection, "message", "local_part", "TEXT NOT NULL DEFAULT ''")
            self._add_column(connection, "message", "domain", "TEXT NOT NULL DEFAULT ''")
            self._add_column(connection, "message", "display_name", "TEXT NOT NULL DEFAULT ''")
            self._add_column(connection, "message", "subject_key", "TEXT NOT NULL DEFAULT ''")
            self._add_column(
                connection, "cycle", "qualified_campaigns", "INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column(
                connection, "cycle", "candidate_messages", "INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column(
                connection, "cycle", "matured_candidates", "INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column(
                connection, "cycle", "campaign_moved_to_trash", "INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column(connection, "cycle", "blacklist_matches", "INTEGER NOT NULL DEFAULT 0")
            self._add_column(connection, "cycle", "blacklist_deleted", "INTEGER NOT NULL DEFAULT 0")
            self._simplify_schema(connection)
        path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    @staticmethod
    def _add_column(
        connection: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _simplify_schema(connection: sqlite3.Connection) -> None:
        """Preserve data from the short-lived redundant schema, then remove it."""
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "blacklist_entry" in tables:
            connection.execute(
                """INSERT OR IGNORE INTO flagged_sender
                   SELECT value, source_account_id, source_mailbox, source_mail_id,
                          first_seen, last_seen
                   FROM blacklist_entry WHERE kind='sender'"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO flagged_domain
                   SELECT value, source_account_id, source_mailbox, source_mail_id,
                          first_seen, last_seen
                   FROM blacklist_entry WHERE kind='domain'"""
            )
            connection.execute("DROP TABLE blacklist_entry")
        if "blacklist_action" in tables:
            connection.execute(
                """UPDATE message SET
                     last_action='blacklist_deleted',
                     action_at=(SELECT action_at FROM blacklist_action a
                                WHERE a.account_id=message.account_id
                                  AND a.mailbox=message.mailbox
                                  AND a.mail_id=message.mail_id)
                   WHERE EXISTS (SELECT 1 FROM blacklist_action a
                                 WHERE a.account_id=message.account_id
                                   AND a.mailbox=message.mailbox
                                   AND a.mail_id=message.mail_id)"""
            )
            connection.execute("DROP TABLE blacklist_action")
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(message)")}
        if "sender_email" in columns:
            connection.execute("ALTER TABLE message DROP COLUMN sender_email")

    def observe(self, messages: list[dict[str, Any]], seen_at: str) -> int:
        keys = {(row["account_id"], row["mailbox"], row["id"]) for row in messages}
        with closing(self._connect()) as connection, connection:
            existing = {
                tuple(row)
                for row in connection.execute("SELECT account_id, mailbox, mail_id FROM message")
                if tuple(row) in keys
            }
            observations = []
            flagged_domains = []
            flagged_senders = []
            flag_evidence = []
            for row in messages:
                fingerprint = fingerprint_sender(str(row["sender"]))
                local_part = fingerprint.local_part if fingerprint else ""
                domain = fingerprint.domain if fingerprint else ""
                sender_email = normalize_sender_email(str(row["sender"]))
                display_name = normalize_display_name(str(row["sender"]))
                subject_key = normalize_subject(str(row["subject"]))
                observations.append(
                    (
                        row["account_id"],
                        row["mailbox"],
                        row["id"],
                        row["message_id"],
                        row["sender"],
                        row["subject"],
                        seen_at,
                        seen_at,
                        int(row["flagged"]),
                        local_part,
                        domain,
                        display_name,
                        subject_key,
                    )
                )
                if row["flagged"]:
                    flag_evidence.append(
                        (
                            row["account_id"],
                            row["mailbox"],
                            row["id"],
                            row["message_id"],
                            row["sender"],
                            sender_email,
                            domain,
                            row["subject"],
                            row.get("received", ""),
                            seen_at,
                            seen_at,
                        )
                    )
                    if sender_email:
                        flagged_senders.append(
                            (
                                sender_email,
                                row["account_id"],
                                row["mailbox"],
                                row["id"],
                                seen_at,
                                seen_at,
                            )
                        )
                    if domain and domain not in PUBLIC_EMAIL_DOMAINS:
                        flagged_domains.append(
                            (domain, row["account_id"], row["mailbox"], row["id"], seen_at, seen_at)
                        )
            connection.executemany(
                """INSERT INTO message (
                       account_id, mailbox, mail_id, message_id, sender, subject,
                       first_seen, last_seen, seen_count, flagged, last_action, action_at,
                       local_part, domain, display_name, subject_key
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL, NULL, ?, ?, ?, ?)
                   ON CONFLICT(account_id, mailbox, mail_id) DO UPDATE SET
                     message_id=excluded.message_id, sender=excluded.sender,
                     subject=excluded.subject, last_seen=excluded.last_seen,
                     seen_count=message.seen_count+1, flagged=excluded.flagged,
                     local_part=excluded.local_part, domain=excluded.domain,
                     display_name=excluded.display_name, subject_key=excluded.subject_key""",
                observations,
            )
            connection.executemany(
                """INSERT INTO flagged_domain VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(domain) DO UPDATE SET last_seen=excluded.last_seen""",
                flagged_domains,
            )
            connection.executemany(
                """INSERT INTO flagged_sender VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET last_seen=excluded.last_seen""",
                flagged_senders,
            )
            connection.executemany(
                """INSERT INTO flag_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(account_id, mailbox, mail_id) DO UPDATE SET
                     message_id=excluded.message_id, raw_sender=excluded.raw_sender,
                     sender_email=excluded.sender_email, sender_domain=excluded.sender_domain,
                     subject=excluded.subject, received=excluded.received,
                     last_observed=excluded.last_observed,
                     observation_count=flag_evidence.observation_count+1""",
                flag_evidence,
            )
        return len(keys - existing)

    def blacklist(self, messages: list[dict[str, Any]], recorded_at: str) -> dict[str, int]:
        """Record exact senders and safe private domains for approved messages."""
        sender_rows = []
        domain_rows = []
        for row in messages:
            email = normalize_sender_email(str(row["sender"]))
            domain = email.rpartition("@")[2]
            source = (row["account_id"], row["mailbox"], row["id"], recorded_at, recorded_at)
            if email:
                sender_rows.append((email, *source))
            if "." in domain and domain not in PUBLIC_EMAIL_DOMAINS:
                domain_rows.append((domain, *source))
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """INSERT INTO flagged_sender VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET last_seen=excluded.last_seen""",
                sender_rows,
            )
            connection.executemany(
                """INSERT INTO flagged_domain VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(domain) DO UPDATE SET last_seen=excluded.last_seen""",
                domain_rows,
            )
        return {
            "senders": len({row[0] for row in sender_rows}),
            "domains": len({row[0] for row in domain_rows}),
        }

    def activate_blacklist(self, activated_at: str) -> None:
        """Enable deletion only for messages first observed after this timestamp."""
        with closing(self._connect()) as connection, connection:
            evidence = connection.execute("SELECT COUNT(*) FROM flag_evidence").fetchone()
            if not evidence or int(evidence[0]) == 0:
                raise RuntimeError("Cannot activate an empty blacklist evidence ledger")
            connection.execute(
                "UPDATE blacklist_control SET activated_at=? WHERE id=1", (activated_at,)
            )

    def blacklist_matches(self, seen_at: str) -> list[dict[str, str]]:
        """Return current messages blacklisted after the evidence baseline was activated."""
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            activated = connection.execute(
                "SELECT activated_at FROM blacklist_control WHERE id=1"
            ).fetchone()
            if not activated or not activated[0]:
                return []
            rows = connection.execute(
                """SELECT account_id, mailbox, mail_id id, message_id, sender, domain
                   FROM message
                   WHERE last_seen=? AND unixepoch(first_seen)>unixepoch(?)
                     AND last_action IS NULL
                   ORDER BY account_id, mailbox, mail_id""",
                (seen_at, str(activated[0])),
            ).fetchall()
            senders = {
                str(row[0]) for row in connection.execute("SELECT email FROM flagged_sender")
            }
            domains = {
                str(row[0]) for row in connection.execute("SELECT domain FROM flagged_domain")
            }
        matches = []
        for row in rows:
            email = normalize_sender_email(str(row["sender"]))
            reasons = []
            if email in senders:
                reasons.append(f"sender:{email}")
            if str(row["domain"]) not in PUBLIC_EMAIL_DOMAINS and str(row["domain"]) in domains:
                reasons.append(f"domain:{row['domain']}")
            if reasons:
                matches.append(
                    {
                        "account_id": str(row["account_id"]),
                        "mailbox": str(row["mailbox"]),
                        "id": str(row["id"]),
                        "message_id": str(row["message_id"]),
                        "reasons": ",".join(reasons),
                    }
                )
        return matches

    def blacklist_actions(self, messages: list[dict[str, str]], action_at: str) -> None:
        """Record each completed delete in the existing message action fields."""
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """UPDATE message SET last_action='blacklist_deleted', action_at=?
                   WHERE account_id=? AND mailbox=? AND mail_id=?""",
                [(action_at, row["account_id"], row["mailbox"], row["id"]) for row in messages],
            )

    def approved_campaign_matches(self, seen_at: str) -> list[dict[str, str]]:
        """Match new Junk against campaign fingerprints from manually approved deletions."""
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            activated = connection.execute(
                "SELECT activated_at FROM blacklist_control WHERE id=1"
            ).fetchone()
            if not activated or not activated[0]:
                return []
            approved_local_parts = {
                str(row[0])
                for row in connection.execute(
                    """SELECT DISTINCT local_part FROM message
                       WHERE last_action=? AND local_part<>''""",
                    (APPROVED_CAMPAIGN_ACTION,),
                )
                if str(row[0]) not in GENERIC_LOCAL_PARTS
            }
            approved_display_names = {
                str(row[0])
                for row in connection.execute(
                    """SELECT display_name FROM message
                       WHERE last_action=? AND display_name<>''
                       GROUP BY display_name HAVING COUNT(DISTINCT domain)>=3""",
                    (APPROVED_CAMPAIGN_ACTION,),
                )
            }
            approved_subjects = {
                str(row[0])
                for row in connection.execute(
                    """SELECT subject_key FROM message
                       WHERE last_action=? AND subject_key<>''
                       GROUP BY subject_key HAVING COUNT(DISTINCT domain)>=3""",
                    (APPROVED_CAMPAIGN_ACTION,),
                )
            }
            rows = connection.execute(
                """SELECT account_id, mailbox, mail_id id, message_id,
                          local_part, display_name, subject_key
                   FROM message
                   WHERE last_seen=? AND unixepoch(first_seen)>unixepoch(?)
                     AND last_action IS NULL
                   ORDER BY account_id, mailbox, mail_id""",
                (seen_at, str(activated[0])),
            ).fetchall()
        matches = []
        for row in rows:
            reasons = []
            if str(row["local_part"]) in approved_local_parts:
                reasons.append(f"approved_local_part:{row['local_part']}")
            if str(row["display_name"]) in approved_display_names:
                reasons.append(f"approved_display_name:{row['display_name']}")
            if str(row["subject_key"]) in approved_subjects:
                reasons.append(f"approved_subject:{row['subject_key']}")
            if reasons:
                matches.append(
                    {
                        "account_id": str(row["account_id"]),
                        "mailbox": str(row["mailbox"]),
                        "id": str(row["id"]),
                        "message_id": str(row["message_id"]),
                        "reasons": ",".join(reasons),
                    }
                )
        return matches

    def approved_campaign_actions(self, messages: list[dict[str, str]], action_at: str) -> None:
        """Record completed deletes caused by an approved campaign fingerprint."""
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """UPDATE message SET last_action='approved_campaign_deleted', action_at=?
                   WHERE account_id=? AND mailbox=? AND mail_id=?""",
                [(action_at, row["account_id"], row["mailbox"], row["id"]) for row in messages],
            )

    def unacted_messages(self, seen_at: str) -> list[dict[str, Any]]:
        """Return current Junk observations that have no completed action."""
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT account_id, mailbox, mail_id id, message_id, sender, subject,
                          flagged
                   FROM message
                   WHERE last_seen=? AND last_action IS NULL
                   ORDER BY account_id, mailbox, mail_id""",
                (seen_at,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_actions(self, messages: list[dict[str, Any]], action: str, action_at: str) -> None:
        """Record a planned or completed mailbox action against exact message keys."""
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """UPDATE message SET last_action=?, action_at=?
                   WHERE account_id=? AND mailbox=? AND mail_id=?""",
                [
                    (
                        action,
                        action_at,
                        row["account_id"],
                        row["mailbox"],
                        row["id"],
                    )
                    for row in messages
                ],
            )

    def blacklist_status(self) -> dict[str, Any]:
        """Return the evidence, activation, and exact operational blacklist counts."""
        with closing(self._connect()) as connection:
            activated = connection.execute(
                "SELECT activated_at FROM blacklist_control WHERE id=1"
            ).fetchone()
            counts = dict(
                connection.execute(
                    """SELECT 'sender', COUNT(*) FROM flagged_sender
                       UNION ALL SELECT 'domain', COUNT(*) FROM flagged_domain"""
                ).fetchall()
            )
            actions = connection.execute(
                "SELECT COUNT(*) FROM message WHERE last_action='blacklist_deleted'"
            ).fetchone()
        return {
            "activated_at": str(activated[0]) if activated and activated[0] else None,
            "sender_entries": int(counts.get("sender", 0)),
            "domain_entries": int(counts.get("domain", 0)),
            "deleted_messages": int(actions[0] if actions else 0),
        }

    def observe_mailboxes(self, mailboxes: list[dict[str, str]], seen_at: str) -> None:
        """Persist the exact account/folder coverage discovered for this run."""
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """INSERT INTO junk_mailbox VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(account_id, mailbox) DO UPDATE SET
                     account_name=excluded.account_name, last_seen=excluded.last_seen""",
                [
                    (
                        row["account_id"],
                        row["account_name"],
                        row["mailbox"],
                        seen_at,
                        seen_at,
                    )
                    for row in mailboxes
                ],
            )

    @staticmethod
    def _policy_parameters(policy: CampaignPolicy) -> tuple[object, ...]:
        return (
            f"-{policy.observation_window_days} days",
            policy.minimum_messages,
            policy.minimum_domains,
            policy.minimum_messages,
            policy.minimum_domains,
            policy.minimum_messages,
            policy.minimum_domains,
        )

    def qualify(self, seen_at: str, policy: CampaignPolicy = DEFAULT_POLICY) -> int:
        """Stage every current message backed by any campaign signal."""
        with closing(self._connect()) as connection, connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                QUALIFICATION_SQL, (*self._policy_parameters(policy), seen_at, seen_at)
            ).fetchall()
            connection.executemany(
                """INSERT INTO campaign_candidate VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'pending', NULL)
                   ON CONFLICT(account_id, mailbox, mail_id) DO UPDATE SET
                     message_id=excluded.message_id, reasons=excluded.reasons,
                     last_qualified=excluded.last_qualified,
                     qualification_count=CASE
                       WHEN campaign_candidate.status='pending'
                        AND campaign_candidate.last_qualified != excluded.last_qualified
                       THEN campaign_candidate.qualification_count+1
                       ELSE campaign_candidate.qualification_count END""",
                [
                    (
                        row["account_id"],
                        row["mailbox"],
                        row["mail_id"],
                        row["message_id"],
                        row["reasons"],
                        seen_at,
                        seen_at,
                    )
                    for row in rows
                ],
            )
        return len(rows)

    def matured_candidates(
        self, seen_at: str, policy: CampaignPolicy = DEFAULT_POLICY
    ) -> list[dict[str, str]]:
        """Return candidates confirmed in Junk across the required cycles."""
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT account_id, mailbox, mail_id id, message_id, reasons
                   FROM campaign_candidate
                   WHERE status='pending' AND last_qualified=? AND qualification_count>=?
                   ORDER BY account_id, mailbox, mail_id""",
                (seen_at, policy.minimum_qualifying_cycles),
            ).fetchall()
        return [dict(row) for row in rows]

    def recognition(self, policy: CampaignPolicy = DEFAULT_POLICY) -> dict[str, Any]:
        """Return current campaigns and the durable candidate queue."""
        window = f"-{policy.observation_window_days} days"
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            campaigns = connection.execute(
                CAMPAIGN_REPORT_SQL,
                self._policy_parameters(policy),
            ).fetchall()
            trusted_domains = {
                str(row[0]) for row in connection.execute("SELECT domain FROM flagged_domain")
            }
            trusted_senders = {
                str(row[0]) for row in connection.execute("SELECT email FROM flagged_sender")
            }
            messages = connection.execute(
                """SELECT c.account_id, c.mailbox, c.mail_id, c.message_id, m.sender,
                          m.subject, m.domain, c.reasons, c.first_qualified,
                          c.last_qualified, c.qualification_count, c.status
                   FROM campaign_candidate c JOIN message m USING(account_id, mailbox, mail_id)
                   WHERE c.status='pending' AND unixepoch(m.last_seen)>=unixepoch('now', ?)
                   ORDER BY c.last_qualified DESC, c.account_id, c.mailbox""",
                (window,),
            ).fetchall()
        candidates = [dict(row) for row in messages]
        return {
            "mode": "delete_after_confirmation_cycle",
            "policy": {
                "minimum_domains": policy.minimum_domains,
                "minimum_messages": policy.minimum_messages,
                "observation_window_days": policy.observation_window_days,
                "minimum_qualifying_cycles": policy.minimum_qualifying_cycles,
            },
            "qualified_campaigns": [dict(row) for row in campaigns],
            "qualified_campaign_count": len(campaigns),
            "candidate_messages": candidates,
            "candidate_message_count": len(candidates),
            "learned_flagged_domains": sorted(trusted_domains),
            "learned_flagged_senders": sorted(trusted_senders),
        }

    def candidate_actions(self, messages: list[dict[str, str]], action_at: str) -> None:
        """Record completed campaign moves in both durable tables."""
        with closing(self._connect()) as connection, connection:
            keys = [(row["account_id"], row["mailbox"], row["id"]) for row in messages]
            connection.executemany(
                """UPDATE campaign_candidate SET status='moved_to_trash', action_at=?
                   WHERE account_id=? AND mailbox=? AND mail_id=?""",
                [(action_at, *key) for key in keys],
            )
            connection.executemany(
                """UPDATE message SET last_action='campaign_moved_to_trash', action_at=?
                   WHERE account_id=? AND mailbox=? AND mail_id=?""",
                [(action_at, *key) for key in keys],
            )

    def campaign_actions_since(self, started_at: str) -> int:
        """Count campaign moves committed during one supervisor run."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT COUNT(*) FROM campaign_candidate
                   WHERE status='moved_to_trash' AND action_at>=?""",
                (started_at,),
            ).fetchone()
        return int(row[0] if row else 0)

    def actions(self, messages: list[dict[str, str]], action_at: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """UPDATE message SET flagged=0, last_action='moved_to_trash', action_at=?
                   WHERE account_id=? AND mail_id=?""",
                [(action_at, row["account_id"], row["id"]) for row in messages],
            )

    def finish(self, result: dict[str, Any]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO cycle (
                       started_at, finished_at, success, scanned, new_messages, flagged,
                       flags_cleared, moved_to_trash, failed, error,
                       qualified_campaigns, candidate_messages, matured_candidates,
                       campaign_moved_to_trash, blacklist_matches, blacklist_deleted
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    result[field]
                    for field in (
                        "started_at",
                        "finished_at",
                        "success",
                        "scanned",
                        "new_messages",
                        "flagged",
                        "flags_cleared",
                        "moved_to_trash",
                        "failed",
                        "error",
                        "qualified_campaigns",
                        "candidate_messages",
                        "matured_candidates",
                        "campaign_moved_to_trash",
                        "blacklist_matches",
                        "blacklist_deleted",
                    )
                ],
            )

    def status(self) -> dict[str, Any]:
        with closing(self._connect()) as connection, connection:
            connection.row_factory = sqlite3.Row
            latest = connection.execute("SELECT * FROM cycle ORDER BY id DESC LIMIT 1").fetchone()
            totals = connection.execute(
                "SELECT COUNT(*) observed, SUM(last_action IS NOT NULL) acted FROM message"
            ).fetchone()
            evidence = connection.execute("SELECT COUNT(*) FROM flag_evidence").fetchone()
            senders = connection.execute("SELECT COUNT(*) FROM flagged_sender").fetchone()
            domains = connection.execute("SELECT COUNT(*) FROM flagged_domain").fetchone()
            mailboxes = connection.execute(
                """SELECT account_id, account_name, mailbox, first_seen, last_seen
                   FROM junk_mailbox ORDER BY account_name, mailbox"""
            ).fetchall()
        return {
            "latest": dict(latest) if latest else None,
            "observed_messages": int(totals[0] or 0),
            "acted_messages": int(totals[1] or 0),
            "flag_evidence_messages": int(evidence[0] if evidence else 0),
            "blacklisted_senders": int(senders[0] if senders else 0),
            "blacklisted_domains": int(domains[0] if domains else 0),
            "junk_mailboxes": [dict(row) for row in mailboxes],
            "blacklist": self.blacklist_status(),
            "recognition": self.recognition(),
            "ledger": str(
                self.path.relative_to(CHECKOUT) if self.path.is_relative_to(CHECKOUT) else self.path
            ),
        }


class JunkSupervisor:
    """Inventory Junk, qualify campaigns, and execute matured cleanup."""

    def __init__(self, index: MailIndex, mail: Mail, ledger: JunkLedger) -> None:
        self.index = index
        self.mail = mail
        self.ledger = ledger

    def _inventory(self, started: str) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
        mailboxes = self.mail.junk_mailboxes()
        self.ledger.observe_mailboxes(mailboxes, started)
        messages = self.mail.junk_messages(mailboxes)
        new_messages = self.ledger.observe(messages, started)
        return (
            messages,
            new_messages,
            {
                "qualified_campaign_count": 0,
                "candidate_message_count": 0,
            },
        )

    def _clean_blacklist(self, matches: list[dict[str, str]]) -> int:
        deleted = 0
        for row in matches:
            changed = self.mail.update(
                row["account_id"],
                row["mailbox"],
                [row["id"]],
                flagged=False,
                delete=True,
            )
            if changed != 1:
                raise RuntimeError(f"Blacklist cleanup changed {changed} of 1 messages")
            self.ledger.blacklist_actions([row], datetime.now(UTC).isoformat())
            deleted += changed
        return deleted

    def _clean_approved_campaigns(self, matches: list[dict[str, str]]) -> int:
        deleted = 0
        for row in matches:
            changed = self.mail.update(
                row["account_id"],
                row["mailbox"],
                [row["id"]],
                flagged=False,
                delete=True,
            )
            if changed != 1:
                raise RuntimeError(f"Approved campaign cleanup changed {changed} of 1 messages")
            self.ledger.approved_campaign_actions([row], datetime.now(UTC).isoformat())
            deleted += changed
        return deleted

    def _clean_campaigns(
        self, matured: list[dict[str, str]], flagged: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        flagged_keys = {(row["account_id"], row["mailbox"], row["id"]) for row in flagged}
        cleaned = [
            row for row in matured if (row["account_id"], row["mailbox"], row["id"]) in flagged_keys
        ]
        remaining = [row for row in matured if row not in cleaned]
        groups: dict[tuple[str, str], list[dict[str, str]]] = {}
        for row in remaining:
            groups.setdefault((row["account_id"], row["mailbox"]), []).append(row)
        for (account_id, mailbox), group in groups.items():
            for offset in range(0, len(group), CAMPAIGN_BATCH_SIZE):
                batch = group[offset : offset + CAMPAIGN_BATCH_SIZE]
                changed = self.mail.update(
                    account_id,
                    mailbox,
                    [row["id"] for row in batch],
                    flagged=False,
                    delete=True,
                )
                if changed != len(batch):
                    raise RuntimeError(
                        f"Campaign cleanup changed {changed} of {len(batch)} messages"
                    )
                self.ledger.candidate_actions(batch, datetime.now(UTC).isoformat())
                cleaned.extend(batch)
        if cleaned:
            self.ledger.candidate_actions(cleaned, datetime.now(UTC).isoformat())
        return cleaned

    def _clean_remaining_junk(self, messages: list[dict[str, Any]]) -> int:
        """Learn and delete Junk that did not match an existing blacklist signal."""
        if not messages:
            return 0
        recorded_at = datetime.now(UTC).isoformat()
        self.ledger.blacklist(messages, recorded_at)
        deleted = 0
        for row in messages:
            changed = self.mail.update(
                str(row["account_id"]),
                str(row["mailbox"]),
                [str(row["id"])],
                flagged=False,
                delete=True,
            )
            if changed != 1:
                raise RuntimeError(f"Default Junk cleanup changed {changed} of 1 messages")
            self.ledger.record_actions([row], "default_junk_deleted", datetime.now(UTC).isoformat())
            deleted += changed
        return deleted

    def run(self) -> dict[str, Any]:
        started = datetime.now(UTC).isoformat()
        messages: list[dict[str, Any]] = []
        new_messages = 0
        recognition: dict[str, Any] = {"qualified_campaign_count": 0, "candidate_message_count": 0}
        errors = []
        try:
            messages, new_messages, recognition = self._inventory(started)
        except Exception as exc:
            errors.append(f"inventory: {exc}")
        blacklist_matches = self.ledger.blacklist_matches(started) if messages else []
        blacklist_deleted = 0
        try:
            blacklist_deleted = self._clean_blacklist(blacklist_matches)
        except Exception as exc:
            errors.append(f"blacklist cleanup: {exc}")
        blacklist_keys = {
            (row["account_id"], row["mailbox"], row["id"]) for row in blacklist_matches
        }
        approved_matches = [
            row
            for row in (self.ledger.approved_campaign_matches(started) if messages else [])
            if (row["account_id"], row["mailbox"], row["id"]) not in blacklist_keys
        ]
        approved_deleted = 0
        try:
            approved_deleted = self._clean_approved_campaigns(approved_matches)
        except Exception as exc:
            errors.append(f"approved campaign cleanup: {exc}")
        approved_keys = {(row["account_id"], row["mailbox"], row["id"]) for row in approved_matches}
        remaining = [
            row
            for row in self.ledger.unacted_messages(started)
            if (row["account_id"], row["mailbox"], row["id"]) not in blacklist_keys | approved_keys
        ]
        default_deleted = 0
        try:
            default_deleted = self._clean_remaining_junk(remaining)
        except Exception as exc:
            errors.append(f"default Junk cleanup: {exc}")
        result = {
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
            "success": int(not errors),
            "scanned": len(messages),
            "new_messages": new_messages,
            "flagged": sum(bool(row["flagged"]) for row in messages),
            "flags_cleared": sum(bool(row["flagged"]) for row in messages) if not errors else 0,
            "moved_to_trash": blacklist_deleted + approved_deleted + default_deleted,
            "failed": len(errors),
            "error": "; ".join(errors),
            "qualified_campaigns": recognition["qualified_campaign_count"],
            "candidate_messages": recognition["candidate_message_count"],
            "matured_candidates": len(approved_matches),
            "campaign_moved_to_trash": approved_deleted,
            "blacklist_matches": len(blacklist_matches),
            "blacklist_deleted": blacklist_deleted,
        }
        self.ledger.finish(result)
        return result
