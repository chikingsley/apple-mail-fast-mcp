"""Shared authenticated Code Mode endpoint for this Mac's Beeper Desktop."""

from pathlib import Path

from fastmcp import Client
from fastmcp.server import create_proxy
from fastmcp.server.auth import StaticTokenVerifier

from .code_mode import communications_code_mode
from .secret_file import read_secret_file


def main() -> None:
    """Keep Beeper's credential on Hochi and share its existing MCP capabilities."""
    home = Path.home()
    upstream_token = read_secret_file(
        str(home / ".config/peacockery-communications/beeper-token"),
        label="Beeper token",
    )
    service_token = read_secret_file(
        str(home / ".config/apple-mail-fast-mcp/http-bearer-token"),
        label="service token",
    )
    proxy = create_proxy(
        Client("http://127.0.0.1:23373/v0/mcp", auth=upstream_token, timeout=60),
        name="Beeper on Hochi",
        provider_error_strategy="raise",
        auth=StaticTokenVerifier(
            tokens={service_token: {"client_id": "simon-agents", "scopes": []}}
        ),
        transforms=[communications_code_mode()],
    )
    proxy.run(
        transport="http",
        host="127.0.0.1",
        port=8766,
        path="/mcp",
        stateless_http=True,
        show_banner=False,
    )


if __name__ == "__main__":
    main()
