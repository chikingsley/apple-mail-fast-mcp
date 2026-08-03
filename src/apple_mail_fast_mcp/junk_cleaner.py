"""Scheduled, metadata-only junk campaign cleanup across Apple Mail accounts."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .exceptions import MailMessageNotFoundError
from .junk_campaigns import JunkCampaignStore, JunkMessage
from .junk_health import DiscordWebhookNotifier, check_provider_health
from .junk_providers import PermanentDeleteError, ProviderAccount, build_purger
from .mail_connector import AppleMailConnector

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

JUNK_MAILBOX_NAMES = frozenset({"junk", "junk email", "spam"})
FLAG_BATCH_SIZE = 100
MAX_FLAG_BATCHES = 100
DEFAULT_CONFIG = Path("~/.config/apple-mail-fast-mcp/junk-cleaner.json").expanduser()
DEFAULT_DATABASE = Path("~/.config/apple-mail-fast-mcp/junk-campaigns.sqlite3").expanduser()


@dataclass(frozen=True)
class JunkMailbox:
    """One Apple Mail junk folder plus its stable provider account identity."""

    connector_account: str
    provider_account: str
    path: str


@dataclass(frozen=True)
class JunkCleanerConfig:
    """Validated scheduled-cleaner policy and provider mappings."""

    mode: str
    minimum_domains: int
    minimum_messages: int
    observation_window_days: int
    maximum_deletions_per_run: int
    providers: Mapping[str, ProviderAccount]
    notification_webhook_file: Path | None

    @classmethod
    def load(cls, path: Path) -> JunkCleanerConfig:
        """Load a private JSON config with conservative defaults."""
        raw = json.loads(path.read_text(encoding="utf-8"))
        providers = {
            email: ProviderAccount(kind=value["kind"], credential=value.get("credential", email))
            for email, value in raw.get("providers", {}).items()
        }
        webhook_value = raw.get("notification_webhook_file")
        if webhook_value is not None and not isinstance(webhook_value, str):
            raise ValueError("notification_webhook_file must be a string path")
        config = cls(
            mode=raw.get("mode", "observe"),
            minimum_domains=int(raw.get("minimum_domains", 3)),
            minimum_messages=int(raw.get("minimum_messages", 3)),
            observation_window_days=int(raw.get("observation_window_days", 30)),
            maximum_deletions_per_run=int(raw.get("maximum_deletions_per_run", 25)),
            providers=providers,
            notification_webhook_file=(
                Path(webhook_value).expanduser() if webhook_value is not None else None
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        """Reject ambiguous or dangerously broad cleaner configuration."""
        if self.mode not in {"observe", "delete"}:
            raise ValueError("Junk cleaner mode must be 'observe' or 'delete'")
        if self.minimum_domains < 3 or self.minimum_messages < 3:
            raise ValueError("Junk campaign evidence thresholds must each be at least 3")
        if not 1 <= self.observation_window_days <= 90:
            raise ValueError("observation_window_days must be between 1 and 90")
        if not 1 <= self.maximum_deletions_per_run <= 100:
            raise ValueError("maximum_deletions_per_run must be between 1 and 100")
        if any(provider.kind not in {"gmail", "microsoft"} for provider in self.providers.values()):
            raise ValueError("Every junk provider must be gmail or microsoft")


def junk_mailboxes(connector: AppleMailConnector) -> list[JunkMailbox]:
    """Return every enabled account's canonical Junk/Spam mailbox path."""
    matches: list[JunkMailbox] = []
    for account in connector.list_accounts():
        account_id = account.get("id")
        account_name = account.get("name")
        if (
            not account.get("enabled", True)
            or not isinstance(account_id, str)
            or not isinstance(account_name, str)
        ):
            continue
        for mailbox in connector.list_mailboxes(account_id):
            name, path = mailbox.get("name"), mailbox.get("path")
            if (
                isinstance(name, str)
                and isinstance(path, str)
                and name.lower() in JUNK_MAILBOX_NAMES
            ):
                matches.append(
                    JunkMailbox(
                        connector_account=account_id,
                        provider_account=account_name.lower(),
                        path=path,
                    )
                )
    return matches


def _message_ids(messages: Sequence[dict[str, object]]) -> list[str]:
    return [message_id for message in messages if isinstance(message_id := message.get("id"), str)]


def clear_junk_flags(connector: AppleMailConnector, *, account: str, mailbox: str) -> int:
    """Clear all flags from messages already held in one junk mailbox."""
    total = 0
    for _ in range(MAX_FLAG_BATCHES):
        messages = connector.search_messages(
            account=account,
            mailbox=mailbox,
            is_flagged=True,
            limit=FLAG_BATCH_SIZE,
        )
        message_ids = _message_ids(messages)
        if not message_ids:
            return total
        total += connector.update_message(
            message_ids, flagged=False, account=account, source_mailbox=mailbox
        )
    raise RuntimeError(f"Reached {MAX_FLAG_BATCHES} flag batches for {account}/{mailbox}")


def _eligible_messages(
    store: JunkCampaignStore,
    messages: Sequence[JunkMessage],
    config: JunkCleanerConfig,
) -> list[JunkMessage]:
    if not messages:
        return []
    local_parts = store.qualified_local_parts(
        account=messages[0].account,
        minimum_domains=config.minimum_domains,
        minimum_messages=config.minimum_messages,
        observation_window_days=config.observation_window_days,
    )
    return [
        message
        for message in messages
        if message.fingerprint.local_part in local_parts and not store.was_deleted(message)
    ]


def clean_junk(
    connector: AppleMailConnector,
    *,
    config: JunkCleanerConfig,
    database_path: Path,
    source_home: Path,
) -> dict[str, Any]:
    """Inventory, unflag, classify, and optionally permanently delete junk."""
    store = JunkCampaignStore(database_path)
    notifier = (
        DiscordWebhookNotifier(config.notification_webhook_file)
        if config.notification_webhook_file is not None
        else None
    )
    provider_health = check_provider_health(
        providers=config.providers,
        store=store,
        source_home=source_home,
        notifier=notifier,
    )
    results: list[dict[str, object]] = []
    deletions_remaining = config.maximum_deletions_per_run
    for junk_mailbox in junk_mailboxes(connector):
        account = junk_mailbox.provider_account
        mailbox = junk_mailbox.path
        result: dict[str, object] = {"account": account, "mailbox": mailbox}
        try:
            rows = connector.search_messages(
                account=junk_mailbox.connector_account, mailbox=mailbox, limit=500
            )
            evidence = store.record_messages(account=account, mailbox=mailbox, messages=rows)
            result["messages_seen"] = len(rows)
            result["evidence_recorded"] = len(evidence)
            result["flags_cleared"] = clear_junk_flags(
                connector,
                account=junk_mailbox.connector_account,
                mailbox=mailbox,
            )
        except MailMessageNotFoundError:
            result["sync_retry"] = True
            results.append(result)
            continue

        candidates = _eligible_messages(store, evidence, config)
        result["qualified"] = len(candidates)
        result["reported"] = 0
        result["deleted"] = 0
        result["failed"] = 0
        provider = config.providers.get(account)
        if provider is None:
            result["provider"] = "unconfigured"
            results.append(result)
            continue

        purger = None
        for message in candidates[:deletions_remaining]:
            if config.mode == "observe":
                store.record_action(message, status="observed")
                result["reported"] = int(result["reported"]) + 1
                continue
            try:
                if purger is None:
                    purger = build_purger(provider, email_account=account, source_home=source_home)
                deleted = purger.permanently_delete(message.rfc_message_id)
                store.record_action(message, status="deleted", detail=f"provider copies: {deleted}")
                result["deleted"] = int(result["deleted"]) + deleted
                deletions_remaining -= 1
            except PermanentDeleteError as exc:
                logger.error("Permanent junk deletion failed for %s/%s: %s", account, mailbox, exc)
                store.record_action(message, status="failed", detail=str(exc))
                result["failed"] = int(result["failed"]) + 1
        results.append(result)
    return {
        "mode": config.mode,
        "database_path": str(database_path),
        "providers": provider_health,
        "mailboxes": results,
    }


def main() -> int:
    """Execute one bounded scheduled-cleaner pass."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config_path = Path(os.environ.get("APPLE_MAIL_MCP_JUNK_CONFIG", DEFAULT_CONFIG)).expanduser()
    database_path = Path(
        os.environ.get("APPLE_MAIL_MCP_JUNK_DATABASE", DEFAULT_DATABASE)
    ).expanduser()
    result = clean_junk(
        AppleMailConnector(),
        config=JunkCleanerConfig.load(config_path),
        database_path=database_path,
        source_home=Path.home(),
    )
    providers_healthy = all(row["healthy"] is True for row in result["providers"])
    mailbox_operations_succeeded = all(row["failed"] == 0 for row in result["mailboxes"])
    success = providers_healthy and mailbox_operations_succeeded
    print(json.dumps({"success": success, **result}))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
