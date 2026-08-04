"""Deterministic provider-authentication recovery without an LLM agent."""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .junk_health import DiscordWebhookNotifier
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

DEVICE_CODE_PATTERN = re.compile(
    r"open the page (?P<url>https://\S+) and enter the code (?P<code>[A-Z0-9]+)",
    re.IGNORECASE,
)
AUTHENTICATION_FAILURE_MARKERS = (
    "authentication required",
    "authorization required",
    "connection is missing",
    "invalid_grant",
    "token has expired",
    "token expired",
    "token revoked",
    "unauthorized",
    "aadsts",
)


@dataclass(frozen=True)
class AuthenticationRecoveryPolicy:
    """Bounded deterministic recovery policy for unhealthy provider credentials."""

    enabled: bool = False
    timeout_seconds: int = 900

    @classmethod
    def from_json(cls, value: object) -> AuthenticationRecoveryPolicy:
        """Parse the private operations configuration."""
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise TypeError("authentication_recovery must be an object")
        timeout_seconds = value.get("timeout_seconds", 900)
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
            raise TypeError("authentication_recovery.timeout_seconds must be an integer")
        policy = cls(enabled=value.get("enabled", False) is True, timeout_seconds=timeout_seconds)
        if not 60 <= policy.timeout_seconds <= 3600:
            raise ValueError("authentication_recovery.timeout_seconds must be between 60 and 3600")
        return policy


def _recovery_id(account: str, provider: ProviderAccount) -> str:
    """Return one stable identity for an account, independent of error wording or date."""
    value = f"{account.lower()}\0{provider.kind}\0{provider.credential}"
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _process_is_running(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _requires_authentication_recovery(detail: str) -> bool:
    """Separate credential failures from transient provider or network failures."""
    normalized = detail.lower()
    return any(marker in normalized for marker in AUTHENTICATION_FAILURE_MARKERS)


class AuthenticationRecoveryDispatcher:
    """Launch one account-scoped deterministic recovery worker."""

    def __init__(
        self,
        *,
        policy: AuthenticationRecoveryPolicy,
        state_directory: Path,
        source_directory: Path,
    ) -> None:
        self.policy = policy
        self.state_directory = state_directory
        self.source_directory = source_directory

    def dispatch(
        self,
        *,
        health: Sequence[dict[str, object]],
        providers: Mapping[str, ProviderAccount],
    ) -> list[dict[str, object]]:
        """Dispatch a stable worker for each unhealthy configured provider."""
        if not self.policy.enabled:
            return []
        operations_cli = shutil.which("apple-mail-ops")
        if operations_cli is None:
            raise FileNotFoundError("apple-mail-ops is unavailable")
        recoveries: list[dict[str, object]] = []
        for result in health:
            if result.get("healthy") is not False:
                continue
            account = result.get("account")
            detail = result.get("detail")
            if not isinstance(account, str) or not isinstance(detail, str):
                continue
            provider = providers.get(account)
            if provider is None or not _requires_authentication_recovery(detail):
                continue
            recovery_id = _recovery_id(account, provider)
            state_path = self.state_directory / f"{recovery_id}.json"
            log_path = self.state_directory / f"{recovery_id}.log"
            existing = (
                json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
            )
            if isinstance(existing, dict):
                active = existing.get("state") in {"starting", "waiting_for_approval"}
                if (active and _process_is_running(existing.get("pid"))) or (
                    existing.get("state") == "awaiting_google_redirect"
                    and result.get("transition") != "failed"
                ):
                    recoveries.append(existing)
                    continue
                if existing.get("state") == "failed" and result.get("transition") != "failed":
                    recoveries.append(existing)
                    continue
            created_at = datetime.now(UTC).isoformat()
            recovery: dict[str, object] = {
                "id": recovery_id,
                "account": account,
                "provider": provider.kind,
                "credential": provider.credential,
                "detail": detail,
                "created_at": created_at,
                "state": "starting",
                "log_path": str(log_path),
            }
            _write_json(state_path, recovery)
            environment = {
                key: value
                for key in (
                    "HOME",
                    "PATH",
                    "APPLE_MAIL_MCP_JUNK_CONFIG",
                    "APPLE_MAIL_MCP_OPS_STATUS",
                )
                if (value := os.environ.get(key)) is not None
            }
            environment["APPLE_MAIL_AUTH_RECOVERY_TIMEOUT"] = str(self.policy.timeout_seconds)
            with log_path.open("ab", buffering=0) as log:
                log_path.chmod(0o600)
                process = subprocess.Popen(
                    [operations_cli, "recover", "--account", account, "--recovery-id", recovery_id],
                    cwd=self.source_directory,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            recovery["pid"] = process.pid
            _write_json(state_path, recovery)
            recoveries.append(recovery)
        return recoveries


def _notify_device_code(
    notifier: DiscordWebhookNotifier, *, account: str, recovery_id: str, url: str, code: str
) -> None:
    notifier.send_text(
        "APPLE MAIL AUTHORIZATION\n\n"
        "Recovery: mail-auth-recovery\n"
        f"Incident: {recovery_id}\n"
        f"Account: {account}\n"
        f"Open: {url}\n"
        "Copy the code from the next Discord message."
    )
    notifier.send_text(code)


def _verify_provider(*, account: str, provider: ProviderAccount, source_home: Path) -> None:
    purger = build_purger(provider, email_account=account, source_home=source_home)
    if isinstance(purger, GmailPurger):
        purger.check_health()
        return
    if isinstance(purger, ImapPurger):
        purger.check_health()
        return
    purger.check_health(expected_email=account)


def _run_microsoft_recovery(
    *,
    account: str,
    provider: ProviderAccount,
    recovery_id: str,
    state_path: Path,
    notifier: DiscordWebhookNotifier,
    timeout_seconds: int,
) -> None:
    m365 = shutil.which("m365")
    if m365 is None:
        raise FileNotFoundError("m365 is unavailable")
    inventory = subprocess.run(
        [m365, "connection", "list", "--output", "json"],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if inventory.returncode != 0:
        raise RuntimeError(
            (inventory.stderr or inventory.stdout or "Connection inventory failed")[-500:]
        )
    try:
        connections = json.loads(inventory.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Microsoft connection inventory is invalid") from exc
    if not isinstance(connections, list):
        raise TypeError("Microsoft connection inventory is invalid")
    if provider.credential in {row.get("name") for row in connections if isinstance(row, dict)}:
        removed = subprocess.run(
            [m365, "connection", "remove", "--name", provider.credential, "--force"],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        if removed.returncode != 0:
            raise RuntimeError(
                (removed.stderr or removed.stdout or "Broken connection removal failed")[-500:]
            )
    process = subprocess.Popen(
        [
            m365,
            "login",
            "--authType",
            "deviceCode",
            "--connectionName",
            provider.credential,
            "--output",
            "text",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output: list[str] = []
    notified = False
    stdout = process.stdout
    if stdout is None:
        process.terminate()
        raise RuntimeError("Microsoft login output stream is unavailable")

    def record_line(line: str) -> None:
        nonlocal notified
        output.append(line)
        match = DEVICE_CODE_PATTERN.search(line)
        if match is not None and not notified:
            _notify_device_code(
                notifier,
                account=account,
                recovery_id=recovery_id,
                url=match.group("url"),
                code=match.group("code").upper(),
            )
            _write_json(
                state_path,
                {
                    "id": recovery_id,
                    "account": account,
                    "provider": provider.kind,
                    "credential": provider.credential,
                    "state": "waiting_for_approval",
                    "pid": os.getpid(),
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
            notified = True

    deadline = time.monotonic() + timeout_seconds
    while process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.terminate()
            raise TimeoutError("Microsoft device-code authorization expired")
        readable, _, _ = select.select([stdout], [], [], min(1.0, remaining))
        if readable:
            record_line(stdout.readline())
    for line in stdout:
        record_line(line)
    return_code = process.wait()
    if return_code != 0:
        detail = "".join(output).strip() or "Microsoft login failed"
        raise RuntimeError(detail[-500:])
    if not notified:
        raise RuntimeError("Microsoft login completed without a device code")


def _run_google_recovery(
    *,
    account: str,
    provider: ProviderAccount,
    recovery_id: str,
    state_path: Path,
    notifier: DiscordWebhookNotifier,
) -> None:
    gog = shutil.which("gog")
    if gog is None:
        raise FileNotFoundError("gog is unavailable")
    completed = subprocess.run(
        [
            gog,
            "auth",
            "add",
            provider.credential or account,
            "--services",
            "gmail",
            "--gmail-scope",
            "full",
            "--remote",
            "--step",
            "1",
            "--plain",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "Google login failed")[-500:])
    url = next((part for part in completed.stdout.split() if part.startswith("https://")), "")
    if not url:
        raise RuntimeError("Google login did not return an authorization URL")
    notifier.send_text(
        "APPLE MAIL GOOGLE AUTHORIZATION\n\n"
        "Recovery: mail-auth-recovery\n"
        f"Incident: {recovery_id}\n"
        f"Account: {account}\n"
        f"Open: {url}\n"
        "Google returns a loopback redirect URL after approval. The completion receiver is required "
        "to exchange that URL for the durable refresh token."
    )
    _write_json(
        state_path,
        {
            "id": recovery_id,
            "account": account,
            "provider": provider.kind,
            "credential": provider.credential,
            "state": "awaiting_google_redirect",
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def run_authentication_recovery(
    *,
    account: str,
    recovery_id: str,
    providers: Mapping[str, ProviderAccount],
    state_directory: Path,
    webhook_file: Path,
    source_home: Path,
    timeout_seconds: int,
) -> int:
    """Run one deterministic provider login and publish durable state."""
    provider = providers.get(account)
    if provider is None:
        raise ValueError(f"Account is absent from the provider configuration: {account}")
    expected_id = _recovery_id(account, provider)
    if recovery_id != expected_id:
        raise ValueError("Recovery identity does not match the configured account")
    state_path = state_directory / f"{recovery_id}.json"
    notifier = DiscordWebhookNotifier(webhook_file)
    try:
        if provider.kind == "microsoft":
            _run_microsoft_recovery(
                account=account,
                provider=provider,
                recovery_id=recovery_id,
                state_path=state_path,
                notifier=notifier,
                timeout_seconds=timeout_seconds,
            )
            _verify_provider(account=account, provider=provider, source_home=source_home)
            _write_json(
                state_path,
                {
                    "id": recovery_id,
                    "account": account,
                    "provider": provider.kind,
                    "credential": provider.credential,
                    "state": "recovered",
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
            notifier.send_text(
                f"APPLE MAIL RECOVERED\n\nAccount: {account}\nAuthentication: healthy\n"
                f"Incident: {recovery_id}"
            )
            return 0
        _run_google_recovery(
            account=account,
            provider=provider,
            recovery_id=recovery_id,
            state_path=state_path,
            notifier=notifier,
        )
        return 0
    except (OSError, PermanentDeleteError, RuntimeError, TimeoutError, ValueError) as exc:
        _write_json(
            state_path,
            {
                "id": recovery_id,
                "account": account,
                "provider": provider.kind,
                "credential": provider.credential,
                "state": "failed",
                "detail": str(exc)[:500],
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        notifier.send_text(
            f"APPLE MAIL RECOVERY FAILED\n\nAccount: {account}\nIncident: {recovery_id}\n"
            f"Detail: {str(exc)[:500]}"
        )
        return 1
