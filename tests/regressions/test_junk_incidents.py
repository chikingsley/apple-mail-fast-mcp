"""Regressions for bounded authentication recovery-agent dispatch."""

from pathlib import Path
from unittest.mock import MagicMock

from apple_mail_fast_mcp.junk_incidents import RecoveryAgentDispatcher, RecoveryAgentPolicy
from apple_mail_fast_mcp.junk_providers import ProviderAccount


def test_junk_regression_dispatches_one_agent_per_failure_signature(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: a five-minute failure must create one daily incident instead of agent spam."""
    process = MagicMock(pid=4321)
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(
        "apple_mail_fast_mcp.junk_incidents.shutil.which", lambda _name: "/bin/agent-cli"
    )
    monkeypatch.setattr("apple_mail_fast_mcp.junk_incidents.subprocess.Popen", popen)
    dispatcher = RecoveryAgentDispatcher(
        policy=RecoveryAgentPolicy(enabled=True),
        state_directory=tmp_path / "incidents",
        source_directory=tmp_path,
        webhook_file=tmp_path / "discord-webhook",
    )
    health: list[dict[str, object]] = [
        {
            "account": "mail@outlook.com",
            "healthy": False,
            "detail": "authentication required",
            "transition": "failed",
        }
    ]
    providers = {"mail@outlook.com": ProviderAccount(kind="microsoft", credential="personal")}

    [first] = dispatcher.dispatch(health=health, providers=providers)
    [second] = dispatcher.dispatch(health=health, providers=providers)

    assert first["id"] == second["id"]
    assert first["state"] == "dispatched"
    assert first["pid"] == 4321
    popen.assert_called_once()
    command = popen.call_args.args[0]
    assert command[:5] == ["/bin/agent-cli", "new", "opencode", "k2.6", "default"]
    assert "apple-mail-ops notify" in command[5]
    assert command[-1] == "edit"
