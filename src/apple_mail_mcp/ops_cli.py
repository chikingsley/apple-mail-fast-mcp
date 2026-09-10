"""Central run and status surface for Apple Mail operations on the mail host."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from .auth_recovery import run_authentication_recovery
from .junk_cleaner import DEFAULT_CONFIG, DEFAULT_RECOVERIES, DEFAULT_STATUS, JunkCleanerConfig
from .junk_cleaner import main as run_supervisor
from .junk_health import DiscordWebhookNotifier

MANAGED_SERVICES = {
    "studio.peacockery.apple-mail-mcp-helper": "resident AppleScript helper",
    "studio.peacockery.apple-mail-mcp": "resident authenticated MCP service",
    "studio.peacockery.apple-mail-ops": "five-minute mail operations supervisor",
}


def _launchctl_state(label: str) -> dict[str, object]:
    result = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return {"loaded": False, "state": "unloaded"}
    fields: dict[str, object] = {"loaded": True}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        for key, output_key in (
            ("state = ", "state"),
            ("runs = ", "runs"),
            ("last exit code = ", "last_exit_code"),
            ("run interval = ", "run_interval"),
        ):
            if line.startswith(key) and output_key not in fields:
                value = line.removeprefix(key)
                fields[output_key] = int(value) if value.isdigit() else value
    return fields


def _installed_launch_agents() -> list[dict[str, object]]:
    agents: list[dict[str, object]] = []
    directory = Path.home() / "Library/LaunchAgents"
    for path in sorted(directory.glob("*.plist")):
        try:
            with path.open("rb") as source:
                value = plistlib.load(source)
        except OSError, plistlib.InvalidFileException:
            continue
        label = value.get("Label")
        if not isinstance(label, str):
            continue
        agents.append(
            {
                "label": label,
                "managed_by_apple_mail": label in MANAGED_SERVICES,
                "role": MANAGED_SERVICES.get(label, "external user LaunchAgent"),
                **_launchctl_state(label),
            }
        )
    return agents


def _tailscale_serve() -> dict[str, object]:
    executable = shutil.which("tailscale")
    if executable is None:
        return {"available": False, "apple_mail_route": False}
    result = subprocess.run(
        [executable, "serve", "status"],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    return {
        "available": True,
        "apple_mail_route": "/apple-mail proxy http://127.0.0.1:8765" in result.stdout,
    }


def status_payload() -> dict[str, Any]:
    """Return one machine-readable operational inventory and latest run."""
    status_path = Path(os.environ.get("APPLE_MAIL_MCP_OPS_STATUS", DEFAULT_STATUS)).expanduser()
    latest_run: dict[str, object] | None = None
    if status_path.exists():
        value = json.loads(status_path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            latest_run = value
    current = Path.home() / ".local/share/peacockery/apple-mail/current"
    return {
        "host": socket.gethostname(),
        "current_release": str(current.resolve()) if current.exists() else None,
        "managed_services": [
            {"label": label, "role": role, **_launchctl_state(label)}
            for label, role in MANAGED_SERVICES.items()
        ],
        "tailscale_serve": _tailscale_serve(),
        "all_user_launch_agents": _installed_launch_agents(),
        "latest_run": latest_run,
    }


def _print_human_status(payload: dict[str, Any]) -> None:
    print(f"Apple Mail operations on {payload['host']}")
    print(f"Release: {payload['current_release'] or 'unavailable'}")
    for service in payload["managed_services"]:
        exit_code = service.get("last_exit_code", "-")
        print(
            f"{service['label']}: {service.get('state', 'unknown')}; "
            f"last_exit={exit_code}; {service['role']}"
        )
    tailscale = payload["tailscale_serve"]
    print(f"Tailscale /apple-mail route: {tailscale.get('apple_mail_route', False)}")
    latest = payload.get("latest_run")
    if isinstance(latest, dict):
        print(
            f"Latest supervisor run: success={latest.get('success')}; "
            f"finished={latest.get('finished_at', 'unknown')}"
        )
        stages = latest.get("stages")
        if isinstance(stages, dict):
            for name, result in stages.items():
                print(f"  {name}: {json.dumps(result, sort_keys=True)}")
    external = [
        row for row in payload["all_user_launch_agents"] if not row["managed_by_apple_mail"]
    ]
    print("Other user LaunchAgents:")
    for row in external:
        print(f"- {row['label']}: {row.get('state', 'unknown')}")


def _notify(message: str) -> int:
    config_path = Path(os.environ.get("APPLE_MAIL_MCP_JUNK_CONFIG", DEFAULT_CONFIG)).expanduser()
    config = JunkCleanerConfig.load(config_path)
    if config.notification_webhook_file is None:
        raise RuntimeError("The Apple Mail operations Discord webhook is unavailable")
    DiscordWebhookNotifier(config.notification_webhook_file).send_text(message)
    return 0


def main() -> int:
    """Run the supervisor, inspect all related services, or send an incident update."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="run one bounded operations cycle")
    status_parser = subparsers.add_parser("status", help="show services and latest cycle")
    status_parser.add_argument("--json", action="store_true", dest="as_json")
    notify_parser = subparsers.add_parser("notify", help="send a bounded Discord incident update")
    notify_parser.add_argument("--message", required=True)
    recover_parser = subparsers.add_parser(
        "recover", help="run one deterministic provider-authentication recovery"
    )
    recover_parser.add_argument("--account", required=True)
    recover_parser.add_argument("--recovery-id", required=True)
    args = parser.parse_args()
    if args.command == "run":
        return run_supervisor()
    if args.command == "notify":
        return _notify(args.message)
    if args.command == "recover":
        config_path = Path(
            os.environ.get("APPLE_MAIL_MCP_JUNK_CONFIG", DEFAULT_CONFIG)
        ).expanduser()
        config = JunkCleanerConfig.load(config_path)
        if config.notification_webhook_file is None:
            raise RuntimeError("The Apple Mail operations Discord webhook is unavailable")
        timeout_seconds = int(
            os.environ.get(
                "APPLE_MAIL_AUTH_RECOVERY_TIMEOUT",
                str(config.authentication_recovery.timeout_seconds),
            )
        )
        return run_authentication_recovery(
            account=args.account,
            recovery_id=args.recovery_id,
            providers=config.providers,
            state_directory=DEFAULT_RECOVERIES,
            webhook_file=config.notification_webhook_file,
            source_home=Path.home(),
            timeout_seconds=timeout_seconds,
        )
    payload = status_payload()
    if args.as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_human_status(payload)
    return 0
