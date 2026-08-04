# Private remote service on macOS

Run one Apple Mail MCP process on the Mac that owns Mail.app, then reach it from every device on the same Tailscale network. The service binds only to loopback, Tailscale Serve terminates HTTPS, and the MCP process requires a bearer token in addition to tailnet membership.

## Install the LaunchAgent

For the Peacockery fleet, run the repository-owned release command from a clean, pushed `main` checkout on GMK:

```bash
uv run apple-mail-fleet
```

It streams `git archive` over SSH into `~/.local/share/peacockery/apple-mail/releases/<commit>`, installs the three LaunchAgents from that immutable tree, updates the `current` symlink, configures the Tailscale Serve path and agent clients, distributes the Apple Mail skill, and verifies the live MCP endpoint. Existing Hochi development checkouts stay independent from the running service.

For direct development or recovery from a repository checkout on the Mac:

```bash
uv run apple-mail-install service
uv run apple-mail-install ops
```

The installer creates or reuses a machine-local code-signing identity, builds and signs `~/Applications/Apple Mail MCP Helper.app`, loads the helper and MCP server as resident per-user LaunchAgents, and loads `studio.peacockery.apple-mail-ops` as one scheduled supervisor. The supervisor exits between five-minute runs. Its internal capture/unflag stage always precedes isolated provider-health, incident-recovery, and permanent-deletion stages. The MCP service enables the IMAP connection pool, masks internal FastMCP errors, disables startup update checks, and writes logs to `~/Library/Logs/apple-mail-fast-mcp/`.

The helper is a small resident native app with no TCP or HTTP listener. It creates `~/.config/apple-mail-fast-mcp/applescript-helper.sock` as an owner-only `0600` Unix socket and rejects clients from another user ID. The Python service sends its internally generated AppleScript through that local socket, and the helper executes it through `NSAppleScript`. Because launchd owns the helper process directly, macOS Automation attributes Mail access to the app instead of `uv` or an ephemeral Python executable.

## Grant Mail Automation once

The installer triggers the macOS consent dialog from the signed helper. Click **Allow**. If access was previously denied, enable **Apple Mail MCP Helper > Mail** under **System Settings > Privacy & Security > Automation**, then verify it directly:

```bash
"$HOME/Applications/Apple Mail MCP Helper.app/Contents/MacOS/AppleMailMCPHelper" \
  --request-mail-automation
```

The command asks the resident helper for Mail's account count, which exercises the real Automation permission rather than an unrestricted metadata query such as Mail's version. The signed helper owns this service's Mail permission.

The installer automatically creates or reuses a valid ten-year local identity named `Apple Mail MCP Local Signing`. macOS requests one administrator authorization when the certificate is first trusted. Later rebuilds retain the same designated requirement and reuse that Automation grant.

To create or repair the identity without reinstalling the service, run:

```bash
uv run apple-mail-install signing-identity
```

To use an Apple-issued identity instead, set its exact name before running the installer:

```bash
export APPLE_MAIL_MCP_CODESIGN_IDENTITY="Apple Development: Your Name (TEAMID)"
uv run apple-mail-install service
```

The practical certificate choices are:

- A local self-signed code-signing certificate: free and stable for private use on Hochi.
- Apple Development: Apple-issued and appropriate for development on registered machines.
- Developer ID Application: Apple-issued for software distributed outside the Mac App Store and the correct choice if the helper will later be notarized or installed elsewhere.

Every certificate-backed option stores the certificate and its private key in Keychain; the environment variable supplies only the identity name. Switching from an ad-hoc identity to a certificate-backed identity can require one new Automation grant, after which rebuilds signed by that identity retain the same designated requirement.

To select ad-hoc signing explicitly, run `APPLE_MAIL_MCP_CODESIGN_IDENTITY=- uv run apple-mail-install service`.

On first install it generates a 256-bit bearer token at `~/.config/apple-mail-fast-mcp/http-bearer-token`. The file is never printed and must remain owned by the current user with mode `0600` (read-only mode `0400` is also accepted). Later installs reuse the same token.

For the Peacockery IMAP fast path without Keychain, place the Stalwart app password at `~/.config/apple-mail-fast-mcp/imap-password-peacockery` before running the installer:

```bash
install -m 600 /path/to/staged-password ~/.config/apple-mail-fast-mcp/imap-password-peacockery
```

The installer adds the password-file path to the LaunchAgent only when that file exists and passes the owner and mode checks. The secret itself never enters the property list or process environment.

The process listens at `http://127.0.0.1:8765/mcp`. Keeping the bind address on loopback prevents LAN or public access.

## Enable the local metadata accelerator

The optional local path reads Mail's live Envelope Index in SQLite read-only mode and accelerates metadata-only `search_messages` calls when direct IMAP is unavailable. IMAP retains priority, and content or attachment queries continue to the existing AppleScript fallback.

The supplied MCP LaunchAgent sets `APPLE_MAIL_MCP_LOCAL_DB=1`. Grant Full Disk Access to the Python executable shown by the running service. For this source deployment, resolve the exact executable after installation:

```bash
service_pid="$(launchctl print "gui/$(id -u)/studio.peacockery.apple-mail-mcp" | awk '/pid =/{print $3; exit}')"
pgrep -P "${service_pid}" | xargs -I{} ps -p {} -o comm=
```

Add that absolute executable under **System Settings > Privacy & Security > Full Disk Access**, restart the LaunchAgent, and check the service log for `Local Apple Mail metadata accelerator enabled`:

```bash
launchctl kickstart -k "gui/$(id -u)/studio.peacockery.apple-mail-mcp"
tail -n 100 ~/Library/Logs/apple-mail-fast-mcp/service.err.log
```

`APPLE_MAIL_MCP_LOCAL_DB_PATH` can select a specific `Envelope Index`; the default discovers the newest `~/Library/Mail/V*/MailData/Envelope Index`. The connector validates file ownership and the required schema, opens SQLite with `mode=ro` plus `query_only`, and disables itself for the process after any access or schema failure.

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
uv run apple-mail-ops status
uv run apple-mail-ops status --json
tail -n 100 ~/Library/Logs/apple-mail-fast-mcp/service.err.log
tail -n 100 ~/Library/Logs/apple-mail-fast-mcp/helper.err.log
tail -n 100 ~/Library/Logs/apple-mail-fast-mcp/mail-ops.err.log
```

The status command reports the immutable release, all three Apple Mail components, the latest complete supervisor result, account-scoped authentication recoveries, and all other user LaunchAgents discovered on the Mac. The supervisor discovers every enabled Junk, Junk Email, Junk Mail, and Spam folder. Its SQLite database retains every usable junk observation, deletion action, provider-health result, and sender domain learned from an auto-flagged junk message. Learned domains apply globally across configured accounts and remain active indefinitely; later messages from those domains qualify when they reach a scanned junk folder. Gmail and Microsoft deletion uses provider APIs, while standard IMAP deletion requires UIDPLUS and uses scoped UID EXPUNGE. Provider authentication failures generate a transition-only Discord alert and one stable deterministic recovery per account and provider credential. Microsoft recovery runs the provider CLI directly and sends two Discord messages: account context first, then the device code as its own copyable message. Google recovery starts the provider CLI's remote OAuth flow and records the account-specific redirect handoff state. Recovery state remains separate from the junk-campaign evidence and deletion-action ledger.

After pushing a fleet update to `main`, run `uv run apple-mail-fleet` on GMK. The command rebuilds the native helper, performs a locked sync, refreshes the configured harnesses and skill copies, then verifies the service. Direct Mac development uses `uv run apple-mail-install service` followed by `uv run apple-mail-install ops`.
