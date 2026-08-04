"""Regressions for the centralized Apple Mail operations inventory."""

import json
import plistlib
from pathlib import Path
from subprocess import CompletedProcess

from apple_mail_fast_mcp.ops_cli import MANAGED_SERVICES, status_payload


def test_ops_regression_status_exposes_managed_and_external_launch_agents(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: operators must see every mail component and other user LaunchAgents."""
    launch_agents = tmp_path / "Library/LaunchAgents"
    launch_agents.mkdir(parents=True)
    for label in [*MANAGED_SERVICES, "com.chat-sync.chatgpt-cdp"]:
        with (launch_agents / f"{label}.plist").open("wb") as target:
            plistlib.dump({"Label": label}, target)
    state = tmp_path / "ops-status.json"
    state.write_text(json.dumps({"success": True, "finished_at": "now"}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPLE_MAIL_MCP_OPS_STATUS", str(state))

    def fake_run(arguments, **_kwargs):
        if arguments[0] == "/bin/hostname":
            return CompletedProcess(arguments, 0, stdout="hochi\n", stderr="")
        return CompletedProcess(
            arguments,
            0,
            stdout="state = not running\nruns = 4\nlast exit code = 0\n",
            stderr="",
        )

    monkeypatch.setattr("apple_mail_fast_mcp.ops_cli.subprocess.run", fake_run)

    payload = status_payload()

    assert {row["label"] for row in payload["managed_services"]} == set(MANAGED_SERVICES)
    external = [
        row for row in payload["all_user_launch_agents"] if not row["managed_by_apple_mail"]
    ]
    assert [row["label"] for row in external] == ["com.chat-sync.chatgpt-cdp"]
    assert payload["latest_run"] == {"success": True, "finished_at": "now"}
