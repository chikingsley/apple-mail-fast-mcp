# Apple Mail MCP

A private MCP service for reading, searching, drafting, sending, and managing email through Apple Mail on Hochi. The service is source-deployed with `uv`; it is not published to PyPI.

## How it works

FastMCP exposes the mail tools over stdio or Streamable HTTP. Reads use server-side IMAP when account credentials are configured and fall back to AppleScript. Mail.app operations run through a resident signed Swift helper over an owner-only Unix socket, so macOS attributes Automation permission to a stable app rather than an ephemeral Python process.

The supported remote deployment binds the Python service to `127.0.0.1`, publishes it privately with Tailscale Serve, and requires a bearer token in addition to tailnet membership:

```text
https://hochi.tailbce39f.ts.net/apple-mail/mcp
```

Funnel must remain disabled.

## Requirements

- macOS with Apple Mail configured
- Python 3.14 managed by `uv`
- Tailscale on devices that use the remote MCP service
- An IMAP password file or Keychain entry for fast server-side search

## Install on the Mail host

Clone the repository on the Mac and run:

```bash
uv sync --locked
./scripts/install-macos-launch-agent.sh
```

The installer builds the native helper, signs it, validates secret-file permissions, installs both per-user LaunchAgents, and restarts the service. On first install, request Mail Automation from the helper and click **Allow**:

```bash
"$HOME/Applications/Apple Mail MCP Helper.app/Contents/MacOS/AppleMailMCPHelper" \
  --request-mail-automation
```

The current Peacockery deployment reads its IMAP password from `~/.config/apple-mail-fast-mcp/imap-password-peacockery` and its HTTP token from `~/.config/apple-mail-fast-mcp/http-bearer-token`. Both files must be owned by the current user and use mode `0400` or `0600`.

See [Private remote service on macOS](docs/guides/REMOTE_SERVICE.md) for Tailscale Serve, client configuration, logs, and verification commands.

## Run from source

The console script is the only application entry point:

```bash
uv run apple-mail-fast-mcp
```

For a local HTTP process:

```bash
uv run apple-mail-fast-mcp \
  --transport http \
  --listen-host 127.0.0.1 \
  --listen-port 8765 \
  --http-path /mcp \
  --bearer-token-file ~/.config/apple-mail-fast-mcp/http-bearer-token
```

## Tool surface

The server exposes account, mailbox, message, attachment, rule, template, and draft lifecycle operations. Destructive operations retain MCP confirmation gates. Immediate send uses the configured account’s SMTP submission path; inbound mail and search remain independent through Mail.app and IMAP.

The complete request and response contracts are in [Tools](docs/reference/TOOLS.md). The implementation split is documented in [Architecture](docs/reference/ARCHITECTURE.md).

## Development

```bash
uv sync
just check
```

`just check` runs Ruff with `ALL`, Ruff formatting, `ty`, Vulture, the test-policy and API-parity checks, documentation and AppleScript safety checks, the regression suite, `uv audit --locked`, and `uv build`.

The repository intentionally has only two test categories:

- `tests/regressions`: a test must identify the issue or real bug it prevents.
- `tests/live`: a test must touch real Apple Mail, AppleScript, IMAP, SMTP, or MCP transport behavior.

Run live tests only against an explicit expendable account:

```bash
MAIL_TEST_ACCOUNT=simon@peacockery.studio just live
```

The real outbound delivery check is separately gated because it sends an actual message and waits for it to reach the selected inbox:

```bash
MAIL_TEST_ACCOUNT=simon@peacockery.studio \
MAIL_LIVE_RECIPIENT=ci@peacockery.studio \
just live-send
```

See [Testing](docs/guides/TESTING.md) and [Development](docs/guides/DEVELOPMENT.md) for the exact policy.

## Security

The HTTP listener remains loopback-only, Tailscale supplies the private network boundary, and every HTTP request also needs the owner-only bearer token. Browser-originated requests are rejected. The helper socket validates ownership and peer UID, and a configured but invalid socket fails closed.

See [Threat model](docs/guides/THREAT_MODEL.md) and [Security checklist](docs/guides/SECURITY_CHECKLIST.md).
