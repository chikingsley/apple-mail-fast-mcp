"""Regressions for deterministic provider-authentication recovery."""

from pathlib import Path
from unittest.mock import MagicMock

from apple_mail_fast_mcp.auth_recovery import (
    AuthenticationRecoveryDispatcher,
    AuthenticationRecoveryPolicy,
    _notify_device_code,
)
from apple_mail_fast_mcp.junk_providers import ProviderAccount


def test_junk_regression_recovery_identity_ignores_error_wording_and_uses_no_agent(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: one account recovery stays stable across changing provider errors."""
    process = MagicMock(pid=4321)
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(
        "apple_mail_fast_mcp.auth_recovery.shutil.which", lambda _name: "/bin/apple-mail-ops"
    )
    monkeypatch.setattr("apple_mail_fast_mcp.auth_recovery.subprocess.Popen", popen)
    monkeypatch.setattr("apple_mail_fast_mcp.auth_recovery._process_is_running", lambda _pid: True)
    dispatcher = AuthenticationRecoveryDispatcher(
        policy=AuthenticationRecoveryPolicy(enabled=True),
        state_directory=tmp_path / "recoveries",
        source_directory=tmp_path,
    )
    providers = {"mail@outlook.com": ProviderAccount(kind="microsoft", credential="personal")}

    [first] = dispatcher.dispatch(
        health=[
            {
                "account": "mail@outlook.com",
                "healthy": False,
                "detail": "authentication required",
                "transition": "failed",
            }
        ],
        providers=providers,
    )
    [second] = dispatcher.dispatch(
        health=[
            {
                "account": "mail@outlook.com",
                "healthy": False,
                "detail": "Microsoft 365 connection is missing: personal",
                "transition": None,
            }
        ],
        providers=providers,
    )

    assert first["id"] == second["id"]
    popen.assert_called_once()
    command = popen.call_args.args[0]
    assert command == [
        "/bin/apple-mail-ops",
        "recover",
        "--account",
        "mail@outlook.com",
        "--recovery-id",
        first["id"],
    ]
    assert "agent-cli" not in command


def test_junk_regression_device_code_is_its_own_discord_message() -> None:
    """Regression: the human approval code must be directly copyable."""
    notifier = MagicMock()

    _notify_device_code(
        notifier,
        account="mail@outlook.com",
        recovery_id="abc123",
        url="https://login.microsoft.com/device",
        code="ABC123XYZ",
    )

    assert notifier.send_text.call_count == 2
    assert notifier.send_text.call_args_list[1].args == ("ABC123XYZ",)


def test_junk_regression_transient_timeout_does_not_start_login(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: a provider timeout must preserve credentials and retry health later."""
    popen = MagicMock()
    monkeypatch.setattr(
        "apple_mail_fast_mcp.auth_recovery.shutil.which", lambda _name: "/bin/apple-mail-ops"
    )
    monkeypatch.setattr("apple_mail_fast_mcp.auth_recovery.subprocess.Popen", popen)
    dispatcher = AuthenticationRecoveryDispatcher(
        policy=AuthenticationRecoveryPolicy(enabled=True),
        state_directory=tmp_path / "recoveries",
        source_directory=tmp_path,
    )

    recoveries = dispatcher.dispatch(
        health=[
            {
                "account": "mail@outlook.com",
                "healthy": False,
                "detail": "Provider command timed out after 45 seconds",
                "transition": "failed",
            }
        ],
        providers={"mail@outlook.com": ProviderAccount(kind="microsoft", credential="personal")},
    )

    assert recoveries == []
    popen.assert_not_called()
