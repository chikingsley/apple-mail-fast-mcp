"""Regressions for provider-health notification delivery evidence."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from apple_mail_fast_mcp.junk_health import DiscordWebhookNotifier


class DiscordResponse:
    """Minimal successful Discord webhook response."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(
            {"id": "message-123", "channel_id": "channel-456", "guild_id": "guild-789"}
        ).encode()


def test_junk_regression_discord_delivery_returns_created_message_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: transport success alone must not claim a Discord notification arrived."""
    webhook_file = tmp_path / "discord-webhook"
    webhook_file.write_text(
        "https://discord.com/api/webhooks/webhook-id/webhook-token\n", encoding="utf-8"
    )
    webhook_file.chmod(0o600)
    requests = []

    def open_request(request, *, timeout: int):
        requests.append((request, timeout))
        return DiscordResponse()

    monkeypatch.setattr("apple_mail_fast_mcp.junk_health.urlopen", open_request)

    receipt = DiscordWebhookNotifier(webhook_file).send_text("copyable-code")

    [(request, timeout)] = requests
    assert parse_qs(urlparse(request.full_url).query)["wait"] == ["true"]
    assert json.loads(request.data) == {"content": "copyable-code"}
    assert timeout == 10
    assert receipt.message_id == "message-123"
    assert receipt.channel_id == "channel-456"
    assert receipt.guild_id == "guild-789"
