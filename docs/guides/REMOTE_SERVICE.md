# Private remote service on macOS

Run one Apple Mail MCP process on the Mac that owns Mail.app, then reach it from every device on the same Tailscale network. The service binds only to loopback; Tailscale Serve terminates HTTPS and provides the network boundary.

## Install the LaunchAgent

From the repository checkout on the Mac:

```bash
./scripts/install-macos-launch-agent.sh
```

The installer performs a locked `uv` sync and loads `studio.peacockery.apple-mail-mcp` as a per-user LaunchAgent. It enables the IMAP connection pool, masks internal FastMCP errors, and writes logs to `~/Library/Logs/apple-mail-fast-mcp/`.

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

List the complete tool surface over Streamable HTTP:

```bash
uv run fastmcp list \
  https://hochi.tailbce39f.ts.net/apple-mail/mcp \
  --auth none \
  --json
```

Call a read-only tool directly and receive JSON without a separate Go binary:

```bash
uv run fastmcp call \
  https://hochi.tailbce39f.ts.net/apple-mail/mcp \
  list_accounts \
  --auth none \
  --json
```

For an account with large mailboxes, run `apple-mail-fast-mcp setup-imap --account <name>` once on the Mac. The service then uses server-side IMAP search and falls back to AppleScript when IMAP is unavailable.

## MCP client configuration

Point any Streamable HTTP MCP client at the HTTPS endpoint above. Tailscale is the authentication and authorization boundary, so the client needs no bearer token. Write-capable tools still retain their MCP confirmation gates.

## Operations

Inspect the service and recent errors:

```bash
launchctl print "gui/$(id -u)/studio.peacockery.apple-mail-mcp"
tail -n 100 ~/Library/Logs/apple-mail-fast-mcp/service.err.log
```

After updating the checkout, rerun `./scripts/install-macos-launch-agent.sh`. The installer performs a locked sync and restarts only this LaunchAgent.
