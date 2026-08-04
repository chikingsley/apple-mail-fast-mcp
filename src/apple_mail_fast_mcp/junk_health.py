"""Provider authentication health checks and transition-only Discord alerts."""

from __future__ import annotations

import json
import logging
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .junk_providers import (
    GmailPurger,
    ImapPurger,
    PermanentDeleteError,
    ProviderAccount,
    build_purger,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from .junk_campaigns import JunkCampaignStore
    from .mail_connector import AppleMailConnector

logger = logging.getLogger(__name__)


class HealthNotifier(Protocol):
    """Delivery boundary for provider-health state transitions."""

    def notify(self, *, failed: Sequence[str], recovered: Sequence[str]) -> None: ...


@dataclass(frozen=True)
class DiscordWebhookNotifier:
    """Send one concise provider-health transition to a private Discord webhook."""

    webhook_file: Path

    def _webhook_url(self) -> str:
        mode = stat.S_IMODE(self.webhook_file.stat().st_mode)
        if mode & 0o077:
            raise PermissionError("Discord webhook file must be owner-only")
        url = self.webhook_file.read_text(encoding="utf-8").strip()
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"discord.com", "discordapp.com"}
            or not parsed.path.startswith("/api/webhooks/")
        ):
            raise ValueError("Discord webhook file contains an unsupported endpoint")
        return url

    def notify(self, *, failed: Sequence[str], recovered: Sequence[str]) -> None:
        """Deliver one aggregate authentication-health transition."""
        lines = []
        if failed:
            accounts = ", ".join(f"`{account}`" for account in failed)
            lines.append(
                "Apple Mail provider authentication requires renewal: "
                f"{accounts}. The operations supervisor recorded the account-specific failure."
            )
        if recovered:
            accounts = ", ".join(f"`{account}`" for account in recovered)
            lines.append(f"Apple Mail provider access recovered: {accounts}.")
        self.send_text("\n".join(lines))

    def send_text(self, content: str) -> None:
        """Deliver one bounded plain-text operational message."""
        if not content.strip() or len(content) > 1800:
            raise ValueError("Discord operational message must contain 1 to 1800 characters")
        request = Request(  # ruff: ignore[suspicious-url-open-usage] -- validated HTTPS
            self._webhook_url(),
            data=json.dumps({"content": content}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "apple-mail-fast-mcp"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:  # ruff: ignore[suspicious-url-open-usage]
            if response.status not in {200, 204}:
                raise RuntimeError(f"Discord webhook returned HTTP {response.status}")


def _check_account(
    *,
    account: str,
    provider: ProviderAccount,
    source_home: Path,
    connector: AppleMailConnector,
    mailbox: str | None,
) -> None:
    purger = build_purger(
        provider,
        email_account=account,
        source_home=source_home,
        source_mailbox=mailbox,
        connector=connector,
    )
    if isinstance(purger, GmailPurger):
        purger.check_health()
        return
    if isinstance(purger, ImapPurger):
        purger.check_health()
        return
    purger.check_health(expected_email=account)


def check_provider_health(
    *,
    providers: Mapping[str, ProviderAccount],
    store: JunkCampaignStore,
    source_home: Path,
    connector: AppleMailConnector,
    mailboxes: Mapping[str, str],
    notifier: HealthNotifier | None = None,
) -> list[dict[str, object]]:
    """Check every provider and notify only when its recorded state changes."""
    results: list[dict[str, object]] = []
    failed_transitions: list[str] = []
    recovered_transitions: list[str] = []
    for account, provider in sorted(providers.items()):
        detail = ""
        try:
            _check_account(
                account=account,
                provider=provider,
                source_home=source_home,
                connector=connector,
                mailbox=mailboxes.get(account),
            )
            healthy = True
        except (OSError, PermanentDeleteError, ValueError) as exc:
            healthy = False
            detail = str(exc)[:500]
            logger.error("Provider health check failed for %s: %s", account, detail)
        previous = store.record_provider_health(account=account, healthy=healthy, detail=detail)
        transitioned = previous is not None and previous != healthy
        first_failure = previous is None and not healthy
        transition = None
        if transitioned or first_failure:
            (recovered_transitions if healthy else failed_transitions).append(account)
            transition = "recovered" if healthy else "failed"
        results.append(
            {
                "account": account,
                "healthy": healthy,
                "detail": detail,
                "transition": transition,
            }
        )
    if notifier is not None and (failed_transitions or recovered_transitions):
        try:
            notifier.notify(failed=failed_transitions, recovered=recovered_transitions)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("Provider health notification failed: %s", exc)
    return results
