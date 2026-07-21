# Private remote service on macOS

Run one Apple Mail MCP process on the Mac that owns Mail.app, then reach it from every device on the same Tailscale network. The service binds only to loopback, Tailscale Serve terminates HTTPS, and the MCP process requires a bearer token in addition to tailnet membership.

## Install the LaunchAgent

From the repository checkout on the Mac:

```bash
./scripts/install-macos-launch-agent.sh
```

The installer builds and signs `~/Applications/Apple Mail MCP Helper.app`, loads the helper and MCP server as separate per-user LaunchAgents, and performs a locked, runtime-only `uv` sync. The MCP service enables the IMAP connection pool, masks internal FastMCP errors, disables startup update checks, and writes logs to `~/Library/Logs/apple-mail-fast-mcp/`.

The helper is a small resident native app with no TCP or HTTP listener. It creates `~/.config/apple-mail-fast-mcp/applescript-helper.sock` as an owner-only `0600` Unix socket and rejects clients from another user ID. The Python service sends its internally generated AppleScript through that local socket, and the helper executes it through `NSAppleScript`. Because launchd owns the helper process directly, macOS Automation attributes Mail access to the app instead of `uv` or an ephemeral Python executable.

## Grant Mail Automation once

After the first install, trigger the macOS consent dialog from the signed helper:

```bash
"$HOME/Applications/Apple Mail MCP Helper.app/Contents/MacOS/AppleMailMCPHelper" \
  --request-mail-automation
```

The command asks the resident helper for Mail's account count, which triggers the real Automation permission rather than an unrestricted metadata query such as Mail's version. Click **Allow**. If access was previously denied, enable **Apple Mail MCP Helper > Mail** under **System Settings > Privacy & Security > Automation** and run the command again. The `uv > Mail` toggle is not used by this service.

The default signature is ad hoc because it requires no certificate or Keychain setup. Reinstalling unchanged helper source on the same Swift toolchain preserves its code identity; changing the helper binary changes that identity and can require granting Automation again.

For a stable identity across helper rebuilds, install a code-signing certificate and set its exact identity name before running the installer:

```bash
export APPLE_MAIL_MCP_CODESIGN_IDENTITY="Apple Development: Your Name (TEAMID)"
./scripts/install-macos-launch-agent.sh
```

The practical certificate choices are:

- A local self-signed code-signing certificate: free and stable on Hochi, but not Apple-trusted, notarizable, or suitable for distribution.
- Apple Development: Apple-issued and appropriate for development on registered machines.
- Developer ID Application: Apple-issued for software distributed outside the Mac App Store and the correct choice if the helper will later be notarized or installed elsewhere.

Every certificate-backed option stores the certificate and its private key in Keychain; the environment variable supplies only the identity name. If Keychain is completely off-limits, ad hoc signing is the remaining built-in option. Switching from the current ad hoc identity to a certificate-backed identity can require one new Automation grant, after which rebuilds signed by that identity should retain the same designated requirement.

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
launchctl print "gui/$(id -u)/studio.peacockery.apple-mail-mcp-helper"
tail -n 100 ~/Library/Logs/apple-mail-fast-mcp/service.err.log
tail -n 100 ~/Library/Logs/apple-mail-fast-mcp/helper.err.log
```

After updating the checkout, rerun `./scripts/install-macos-launch-agent.sh`. The installer rebuilds the native helper, performs a locked sync, and restarts only this LaunchAgent. If the helper source changed and the install uses the default ad hoc signature, grant Mail Automation again.
