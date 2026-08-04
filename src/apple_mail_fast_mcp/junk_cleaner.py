"""Scheduled, metadata-only junk campaign cleanup across Apple Mail accounts."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .auth_recovery import AuthenticationRecoveryDispatcher, AuthenticationRecoveryPolicy
from .exceptions import MailMessageNotFoundError
from .junk_campaigns import JunkCampaignStore, JunkMessage
from .junk_health import DiscordWebhookNotifier, check_provider_health
from .junk_providers import PermanentDeleteError, ProviderAccount, build_purger
from .mail_connector import AppleMailConnector

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

JUNK_MAILBOX_NAMES = frozenset({"junk", "junk email", "junk mail", "spam"})
FLAG_BATCH_SIZE = 100
MAX_FLAG_BATCHES = 100
DEFAULT_CONFIG = Path("~/.config/apple-mail-fast-mcp/mail-ops.json").expanduser()
DEFAULT_DATABASE = Path("~/.config/apple-mail-fast-mcp/junk-campaigns.sqlite3").expanduser()
DEFAULT_STATUS = Path("~/.local/state/apple-mail-fast-mcp/ops-status.json").expanduser()
DEFAULT_RECOVERIES = Path("~/.local/state/apple-mail-fast-mcp/auth-recovery").expanduser()


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
    authentication_recovery: AuthenticationRecoveryPolicy = field(
        default_factory=AuthenticationRecoveryPolicy
    )

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
            authentication_recovery=AuthenticationRecoveryPolicy.from_json(
                raw.get("authentication_recovery")
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
        if any(
            provider.kind not in {"gmail", "imap", "microsoft"}
            for provider in self.providers.values()
        ):
            raise ValueError("Every junk provider must be gmail, imap, or microsoft")


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


def _result_count(result: Mapping[str, object], key: str) -> int:
    value = result.get(key, 0)
    return value if isinstance(value, int) else 0


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
    *,
    flagged_connector_ids: set[str],
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
        if (
            message.connector_id in flagged_connector_ids
            or message.fingerprint.local_part in local_parts
        )
        and not store.was_deleted(message)
    ]


def _process_candidates(
    store: JunkCampaignStore,
    candidates: Sequence[JunkMessage],
    *,
    config: JunkCleanerConfig,
    provider: ProviderAccount,
    source_home: Path,
    connector: AppleMailConnector,
    source_mailbox: str,
    deletions_remaining: int,
) -> tuple[int, int, int, int]:
    """Observe or delete one mailbox's bounded candidate set."""
    reported = 0
    deleted_total = 0
    failed = 0
    purger = None
    for message in candidates[:deletions_remaining]:
        if config.mode == "observe":
            store.record_action(message, status="observed")
            reported += 1
            continue
        try:
            if purger is None:
                purger = build_purger(
                    provider,
                    email_account=message.account,
                    source_home=source_home,
                    source_mailbox=source_mailbox,
                    connector=connector,
                )
            deleted = purger.permanently_delete(message.rfc_message_id)
            store.record_action(message, status="deleted", detail=f"provider copies: {deleted}")
            deleted_total += deleted
            deletions_remaining -= 1
        except PermanentDeleteError as exc:
            logger.error(
                "Permanent junk deletion failed for %s/%s: %s",
                message.account,
                message.mailbox,
                exc,
            )
            store.record_action(message, status="failed", detail=str(exc))
            failed += 1
            break
    return reported, deleted_total, failed, deletions_remaining


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
    results: list[dict[str, object]] = []
    inventories: list[tuple[dict[str, object], JunkMailbox, list[JunkMessage], set[str]]] = []
    discovered_mailboxes = junk_mailboxes(connector)
    for junk_mailbox in discovered_mailboxes:
        account = junk_mailbox.provider_account
        mailbox = junk_mailbox.path
        result: dict[str, object] = {
            "account": account,
            "mailbox": mailbox,
            "qualified": 0,
            "reported": 0,
            "deleted": 0,
            "failed": 0,
        }
        results.append(result)
        try:
            rows = connector.search_messages(
                account=junk_mailbox.connector_account, mailbox=mailbox, limit=500
            )
            evidence = store.record_messages(account=account, mailbox=mailbox, messages=rows)
            flagged_connector_ids = store.flagged_message_ids(account=account, mailbox=mailbox)
            result["messages_seen"] = len(rows)
            result["evidence_recorded"] = len(evidence)
            result["flags_cleared"] = clear_junk_flags(
                connector,
                account=junk_mailbox.connector_account,
                mailbox=mailbox,
            )
        except MailMessageNotFoundError:
            result["sync_retry"] = True
            continue
        inventories.append((result, junk_mailbox, evidence, flagged_connector_ids))

    provider_health = check_provider_health(
        providers=config.providers,
        store=store,
        source_home=source_home,
        connector=connector,
        mailboxes={mailbox.provider_account: mailbox.path for mailbox in discovered_mailboxes},
        notifier=notifier,
    )
    recovery_dispatch_error = ""
    try:
        recoveries = AuthenticationRecoveryDispatcher(
            policy=config.authentication_recovery,
            state_directory=DEFAULT_RECOVERIES,
            source_directory=Path.cwd(),
        ).dispatch(health=provider_health, providers=config.providers)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.error("Authentication recovery dispatch failed: %s", exc)
        recoveries = []
        recovery_dispatch_error = str(exc)[:500]
    health_by_account = {str(row["account"]): row.get("healthy") is True for row in provider_health}
    deletions_remaining = config.maximum_deletions_per_run
    for result, junk_mailbox, evidence, flagged_connector_ids in inventories:
        account = junk_mailbox.provider_account
        candidates = _eligible_messages(
            store,
            evidence,
            config,
            flagged_connector_ids=flagged_connector_ids,
        )
        result["qualified"] = len(candidates)
        provider = config.providers.get(account)
        if provider is None:
            result["provider"] = "unconfigured"
            continue
        if not health_by_account.get(account):
            result["deferred"] = len(candidates)
            continue

        reported, deleted_total, failed, deletions_remaining = _process_candidates(
            store,
            candidates,
            config=config,
            provider=provider,
            source_home=source_home,
            connector=connector,
            source_mailbox=junk_mailbox.path,
            deletions_remaining=deletions_remaining,
        )
        result["reported"] = reported
        result["deleted"] = deleted_total
        result["failed"] = failed
    stages = {
        "capture_unflag": {
            "success": all(result.get("sync_retry") is not True for result in results),
            "mailboxes": len(results),
            "flags_cleared": sum(_result_count(result, "flags_cleared") for result in results),
        },
        "provider_health": {
            "success": all(result["healthy"] is True for result in provider_health),
            "healthy": sum(result["healthy"] is True for result in provider_health),
            "unhealthy": sum(result["healthy"] is False for result in provider_health),
        },
        "authentication_recovery": {
            "success": not recovery_dispatch_error,
            "started": sum(recovery.get("state") == "starting" for recovery in recoveries),
        },
        "deletion": {
            "success": all(result["failed"] == 0 for result in results),
            "deleted": sum(_result_count(result, "deleted") for result in results),
            "deferred": sum(_result_count(result, "deferred") for result in results),
            "failed": sum(_result_count(result, "failed") for result in results),
        },
    }
    return {
        "mode": config.mode,
        "database_path": str(database_path),
        "stages": stages,
        "providers": provider_health,
        "authentication_recoveries": recoveries,
        "authentication_recovery_error": recovery_dispatch_error,
        "mailboxes": results,
    }


def main() -> int:
    """Execute one bounded scheduled-cleaner pass."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started_at = datetime.now(UTC).isoformat()
    config_path = Path(os.environ.get("APPLE_MAIL_MCP_JUNK_CONFIG", DEFAULT_CONFIG)).expanduser()
    database_path = Path(
        os.environ.get("APPLE_MAIL_MCP_JUNK_DATABASE", DEFAULT_DATABASE)
    ).expanduser()
    status_path = Path(os.environ.get("APPLE_MAIL_MCP_OPS_STATUS", DEFAULT_STATUS)).expanduser()
    try:
        result = clean_junk(
            AppleMailConnector(),
            config=JunkCleanerConfig.load(config_path),
            database_path=database_path,
            source_home=Path.home(),
        )
        providers_healthy = all(row["healthy"] is True for row in result["providers"])
        mailbox_operations_succeeded = all(
            row["failed"] == 0 and row.get("sync_retry") is not True for row in result["mailboxes"]
        )
        success = (
            providers_healthy
            and mailbox_operations_succeeded
            and not result["authentication_recovery_error"]
        )
        status = {
            "success": success,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            **result,
        }
    except Exception as exc:
        logger.exception("Apple Mail operations supervisor failed")
        success = False
        status = {
            "success": False,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "fatal_error": str(exc)[:500],
        }
    _write_status(status_path, status)
    print(json.dumps(status))
    return 0 if success else 1


def _write_status(path: Path, status: dict[str, Any]) -> None:
    """Atomically publish the complete latest-run status."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    Path(temporary).chmod(0o600)
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
