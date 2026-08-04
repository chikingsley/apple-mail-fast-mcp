"""Provider-native permanent deletion for qualified Junk/Spam messages."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlencode

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path


class ImapDeleteConnector(Protocol):
    """Narrow connector boundary for standard IMAP Junk cleanup."""

    def check_imap_health(self, account: str, mailbox: str) -> None: ...

    def permanently_delete_imap_message(
        self, *, account: str, mailbox: str, rfc_message_id: str
    ) -> int: ...


class PermanentDeleteError(RuntimeError):
    """A provider could not resolve or permanently delete a junk message."""


class CommandRunner(Protocol):
    """Injectable subprocess boundary used by provider adapters."""

    def __call__(
        self, arguments: Sequence[str], *, environment: Mapping[str, str] | None = None
    ) -> str: ...


def run_command(arguments: Sequence[str], *, environment: Mapping[str, str] | None = None) -> str:
    """Run one fixed-argument provider CLI call and return its JSON output."""
    try:
        completed = subprocess.run(
            list(arguments),
            capture_output=True,
            check=False,
            env=dict(environment) if environment is not None else None,
            text=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired as exc:
        raise PermanentDeleteError("Provider command timed out after 45 seconds") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "provider command failed"
        raise PermanentDeleteError(detail[:500])
    return completed.stdout


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise PermanentDeleteError("Provider returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise PermanentDeleteError("Provider returned an unexpected JSON shape")
    return value


@dataclass(frozen=True)
class ProviderAccount:
    """One explicit Apple Mail account to provider-CLI mapping."""

    kind: str
    credential: str


class GmailPurger:
    """Resolve RFC Message-IDs in Gmail Spam and call messages.delete."""

    def __init__(self, *, account: str, gog_path: str, runner: CommandRunner = run_command) -> None:
        self.account = account
        self.gog_path = gog_path
        self.runner = runner

    def resolve_message_ids(self, rfc_message_id: str) -> list[str]:
        """Resolve a current Spam message to provider-native Gmail ids."""
        query = f"in:spam rfc822msgid:{rfc_message_id}"
        params = json.dumps({"userId": "me", "q": query, "maxResults": 10})
        raw = self.runner(
            (
                self.gog_path,
                "api",
                "call",
                "gmail",
                "v1",
                "users.messages.list",
                "--params",
                params,
                "--account",
                self.account,
                "--json",
                "--no-input",
            )
        )
        messages = _json_object(raw).get("messages", [])
        message_ids = [row.get("id") for row in messages if isinstance(row, dict)]
        valid_ids = [message_id for message_id in message_ids if isinstance(message_id, str)]
        if not valid_ids:
            raise PermanentDeleteError("Gmail could not resolve the message in Spam")
        return valid_ids

    def check_health(self) -> None:
        """Prove that the configured Gmail credential can read its profile."""
        params = json.dumps({"userId": "me"})
        raw = self.runner(
            (
                self.gog_path,
                "api",
                "call",
                "gmail",
                "v1",
                "users.getProfile",
                "--params",
                params,
                "--account",
                self.account,
                "--json",
                "--no-input",
            )
        )
        email_address = _json_object(raw).get("emailAddress")
        if not isinstance(email_address, str) or email_address.lower() != self.account.lower():
            raise PermanentDeleteError(
                "Gmail profile identity did not match the configured account"
            )

    def permanently_delete(self, rfc_message_id: str) -> int:
        """Permanently delete every Spam copy matching one RFC Message-ID."""
        valid_ids = self.resolve_message_ids(rfc_message_id)
        for message_id in valid_ids:
            delete_params = json.dumps({"userId": "me", "id": message_id})
            self.runner(
                (
                    self.gog_path,
                    "api",
                    "call",
                    "gmail",
                    "v1",
                    "users.messages.delete",
                    "--params",
                    delete_params,
                    "--account",
                    self.account,
                    "--allow-write",
                    "--force",
                    "--no-input",
                )
            )
        return len(valid_ids)


class MicrosoftPurger:
    """Use one locked named CLI connection to call Graph permanentDelete."""

    def __init__(
        self,
        *,
        connection_name: str,
        m365_path: str,
        source_home: Path,
        runner: CommandRunner = run_command,
    ) -> None:
        self.connection_name = connection_name
        self.m365_path = m365_path
        self.source_home = source_home
        self.runner = runner

    @contextmanager
    def _selected_connection(self) -> Iterator[Mapping[str, str]]:
        lock_path = self.source_home / ".config/apple-mail-fast-mcp/m365-connection.lock"
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            lock_path.chmod(0o600)
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            raw = self.runner((self.m365_path, "connection", "list", "--output", "json"))
            try:
                connections = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise PermanentDeleteError("Microsoft 365 connection inventory is invalid") from exc
            if not isinstance(connections, list):
                raise PermanentDeleteError("Microsoft 365 connection inventory is invalid")
            available = {row.get("name") for row in connections if isinstance(row, dict)}
            active_names = [
                row.get("name")
                for row in connections
                if isinstance(row, dict) and row.get("active") is True
            ]
            original = active_names[0] if active_names else None
            if self.connection_name not in available:
                raise PermanentDeleteError(
                    f"Microsoft 365 connection is missing: {self.connection_name}"
                )
            changed = original != self.connection_name
            try:
                if changed:
                    self._select_connection(self.connection_name)
                yield os.environ
            finally:
                if changed and isinstance(original, str):
                    self._select_connection(original)
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _select_connection(self, name: str) -> None:
        self.runner(
            (
                self.m365_path,
                "connection",
                "use",
                "--name",
                name,
                "--output",
                "none",
            )
        )

    def _resolve_message_ids(
        self, rfc_message_id: str, *, environment: Mapping[str, str]
    ) -> list[str]:
        bracketed_id = f"<{rfc_message_id.strip('<>')}>".replace("'", "''")
        query = urlencode(
            {
                "$filter": f"internetMessageId eq '{bracketed_id}'",
                "$select": "id",
                "$top": "10",
            }
        )
        list_url = "https://graph.microsoft.com/v1.0/me/mailFolders/junkemail/messages?" + query
        raw = self.runner(
            (self.m365_path, "request", "--url", list_url, "--output", "json"),
            environment=environment,
        )
        messages = _json_object(raw).get("value", [])
        message_ids = [row.get("id") for row in messages if isinstance(row, dict)]
        valid_ids = [message_id for message_id in message_ids if isinstance(message_id, str)]
        if not valid_ids:
            raise PermanentDeleteError("Microsoft Graph could not resolve the message in Junk")
        return valid_ids

    def resolve_message_ids(self, rfc_message_id: str) -> list[str]:
        """Resolve a current Junk message and restore the prior active connection."""
        with self._selected_connection() as environment:
            return self._resolve_message_ids(rfc_message_id, environment=environment)

    def check_health(self, *, expected_email: str) -> None:
        """Prove that a named Microsoft connection can read the expected profile."""
        profile_url = "https://graph.microsoft.com/v1.0/me?$select=userPrincipalName,mail"
        with self._selected_connection() as environment:
            raw = self.runner(
                (self.m365_path, "request", "--url", profile_url, "--output", "json"),
                environment=environment,
            )
        profile = _json_object(raw)
        identities = {
            value.lower()
            for key in ("userPrincipalName", "mail")
            if isinstance((value := profile.get(key)), str)
        }
        if expected_email.lower() not in identities:
            raise PermanentDeleteError(
                "Microsoft profile identity did not match the configured account"
            )

    def permanently_delete(self, rfc_message_id: str) -> int:
        """Resolve a message inside Junk Email and invoke Graph permanentDelete."""
        with self._selected_connection() as environment:
            valid_ids = self._resolve_message_ids(rfc_message_id, environment=environment)
            for message_id in valid_ids:
                self.runner(
                    (
                        self.m365_path,
                        "request",
                        "--url",
                        f"https://graph.microsoft.com/v1.0/me/messages/{message_id}/permanentDelete",
                        "--method",
                        "post",
                        "--output",
                        "json",
                    ),
                    environment=environment,
                )
        return len(valid_ids)


@dataclass(frozen=True)
class ImapPurger:
    """Use scoped UID EXPUNGE for a standard IMAP Junk mailbox."""

    account: str
    mailbox: str
    connector: ImapDeleteConnector

    def check_health(self) -> None:
        """Prove direct IMAP access to the configured Junk mailbox."""
        try:
            self.connector.check_imap_health(self.account, self.mailbox)
        except Exception as exc:
            raise PermanentDeleteError(str(exc) or "IMAP health check failed") from exc

    def permanently_delete(self, rfc_message_id: str) -> int:
        """Permanently delete one RFC Message-ID from the configured Junk mailbox."""
        try:
            deleted = self.connector.permanently_delete_imap_message(
                account=self.account,
                mailbox=self.mailbox,
                rfc_message_id=rfc_message_id,
            )
        except Exception as exc:
            raise PermanentDeleteError(str(exc) or "IMAP permanent deletion failed") from exc
        if deleted < 1:
            raise PermanentDeleteError("IMAP could not resolve the message in Junk")
        return deleted


def build_purger(
    account: ProviderAccount,
    *,
    email_account: str,
    source_home: Path,
    source_mailbox: str | None = None,
    connector: ImapDeleteConnector | None = None,
    runner: CommandRunner = run_command,
) -> GmailPurger | MicrosoftPurger | ImapPurger:
    """Build the configured provider adapter for one Apple Mail account."""
    if account.kind == "gmail":
        gog_path = shutil.which("gog")
        if gog_path is None:
            raise PermanentDeleteError("gog is unavailable on this host")
        return GmailPurger(
            account=account.credential or email_account, gog_path=gog_path, runner=runner
        )
    if account.kind == "microsoft":
        m365_path = shutil.which("m365")
        if m365_path is None:
            raise PermanentDeleteError("m365 is unavailable on this host")
        return MicrosoftPurger(
            connection_name=account.credential,
            m365_path=m365_path,
            source_home=source_home,
            runner=runner,
        )
    if account.kind == "imap":
        if connector is None or source_mailbox is None:
            raise PermanentDeleteError("IMAP cleanup requires its connector and Junk mailbox")
        return ImapPurger(
            account=account.credential or email_account,
            mailbox=source_mailbox,
            connector=connector,
        )
    raise PermanentDeleteError(f"Unsupported junk provider: {account.kind}")
