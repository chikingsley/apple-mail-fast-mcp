# Private remote service on macOS

Run one Apple Mail MCP process on the Mac that owns Mail.app, then reach it from every device on the same Tailscale network. The service binds only to loopback, Tailscale Serve terminates HTTPS, and the MCP process requires a bearer token in addition to tailnet membership.

## Install the LaunchAgent

From the repository checkout on the Mac:

```bash
./scripts/install-macos-launch-agent.sh
```

The installer performs a locked, runtime-only `uv` sync and loads `studio.peacockery.apple-mail-mcp` as a per-user LaunchAgent. It enables the IMAP connection pool, masks internal FastMCP errors, disables startup update checks, and writes logs to `~/Library/Logs/apple-mail-fast-mcp/`.

On first install it generates a 256-bit bearer token at `~/.config/apple-mail-fast-mcp/http-bearer-token`. The file is never printed and must remain owned by the current user with mode `0600` (read-only mode `0400` is also accepted). Later installs reuse the same token.

For the Peacockery IMAP fast path without Keychain, place the Stalwart app password at `~/.config/apple-mail-fast-mcp/imap-password-peacockery` before running the installer:

```bash
install -m 600 /path/to/staged-password ~/.config/apple-mail-fast-mcp/imap-password-peacockery
```

The installer adds the password-file path to the LaunchAgent only when that file exists and passes the owner and mode checks. The secret itself never enters the property list or process environment.

The process listens at `http://127.0.0.1:8765/mcp`. Keeping the bind address on loopback prevents LAN or public access.

## Publish through Tailscale Serve

Preserve any existing root handler and add the MCP server under `/apple-mail`:

```bash
tailscale serve --bg --set-path=/apple-mail http://127.0.0.1:8765
tailscale serve status
```

With Hochi's current MagicDNS name, the MCP endpoint is:

```text
https://hochi.tailbce39f.ts.net/apple-mail/mcp
```

Only tailnet devices can reach a Tailscale Serve endpoint. Keep Funnel disabled for this service.

## Verify the service

Load the token into the current shell without printing it:

```bash
IFS= read -r APPLE_MAIL_MCP_BEARER_TOKEN <~/.config/apple-mail-fast-mcp/http-bearer-token
export APPLE_MAIL_MCP_BEARER_TOKEN
```

List the complete tool surface over Streamable HTTP:

```bash
uv run fastmcp list \
  https://hochi.tailbce39f.ts.net/apple-mail/mcp \
  --auth "${APPLE_MAIL_MCP_BEARER_TOKEN}" \
  --json
```

Call a read-only tool directly and receive JSON without a separate Go binary:

```bash
uv run fastmcp call \
  https://hochi.tailbce39f.ts.net/apple-mail/mcp \
  list_accounts \
  --auth "${APPLE_MAIL_MCP_BEARER_TOKEN}" \
  --json
```

Load `APPLE_MAIL_MCP_BEARER_TOKEN` from the owner-only token file without printing it before running these probes. For an account with large mailboxes, either install the account's password file or run `apple-mail-fast-mcp setup-imap --account <name>` once on the Mac. The service then uses server-side IMAP search and falls back to AppleScript when IMAP is unavailable.

## MCP client configuration

Point any Streamable HTTP MCP client at the HTTPS endpoint above and configure its bearer-token environment-variable option. Tailscale remains the private network boundary, while the bearer token prevents another admitted tailnet process from invoking the MCP endpoint anonymously. Browser-originated HTTP requests are rejected. Write-capable tools still retain their MCP confirmation gates.

## Operations

Inspect the service and recent errors:

```bash
launchctl print "gui/$(id -u)/studio.peacockery.apple-mail-mcp"
tail -n 100 ~/Library/Logs/apple-mail-fast-mcp/service.err.log
```

After updating the checkout, rerun `./scripts/install-macos-launch-agent.sh`. The installer performs a locked sync and restarts only this LaunchAgent.
