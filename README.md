# Apple Mail MCP

A private MCP service for reading, searching, drafting, sending, and managing email through Apple Mail on Hochi. The service uses private source deployments with `uv`; PyPI distribution sits outside this project.

## How it works

FastMCP exposes the mail tools over stdio or Streamable HTTP. Reads use server-side IMAP when account credentials are configured. An opt-in read-only local metadata accelerator can serve searches from Mail's Envelope Index when IMAP is unavailable, with AppleScript as the universal fallback. Mail.app operations run through a resident signed Swift helper over an owner-only Unix socket, so macOS attributes Automation permission to a stable app rather than an ephemeral Python process.

The supported remote deployment binds the Python service to `127.0.0.1`, publishes it privately with Tailscale Serve, and requires a bearer token in addition to tailnet membership:

```text
https://hochi.tailbce39f.ts.net/apple-mail/mcp
```

Funnel must remain disabled.

## Requirements

- macOS with Apple Mail configured
- Xcode or Xcode Command Line Tools with the Swift compiler
- Python 3.14 managed by `uv`
- Administrator access for the one-time local certificate trust
- Tailscale on devices that use the remote MCP service
- An IMAP password file or Keychain entry for fast server-side search
- Full Disk Access for the service's Python executable when the local metadata accelerator is enabled

## Install on the Mail host

The supported Peacockery release path runs from a clean, pushed `main` checkout on GMK:

```bash
uv run apple-mail-fleet
```

The fleet command creates an immutable Git-archive release on Hochi, installs the LaunchAgents from that release, preserves owner-only credentials, publishes the private Tailscale Serve path, configures each installed agent harness, and distributes the repository-owned Apple Mail skill across the fleet. It leaves existing development checkouts untouched.

For direct development or recovery on the Mac, run:

```bash
./scripts/install-macos-launch-agent.sh
```

The release installs three explicit components: a resident signed AppleScript helper, a resident authenticated MCP service, and one five-minute operations supervisor that exits between runs. The supervisor captures and clears provider-supplied Junk flags before isolated provider health and permanent deletion stages across every enabled Junk, Junk Email, Junk Mail, and Spam folder. Gmail and Microsoft use provider APIs; standard IMAP accounts use scoped UID EXPUNGE when the server advertises UIDPLUS. A provider failure can defer its own deletions while flag cleanup and every other account continue. Account-scoped authentication recovery runs provider CLIs directly; Microsoft device codes arrive as their own copyable Discord messages.

Inspect the complete Apple Mail inventory, latest supervisor result, and every other user LaunchAgent on Hochi with:

```bash
uv run apple-mail-ops status
```

The installer creates or reuses a machine-local code-signing identity, performs a locked `uv` sync, builds and signs the native helper, validates secret-file permissions, installs the per-user LaunchAgents, restarts the services, and verifies Mail Automation. On first install, macOS asks for administrator authorization to trust the local signing certificate and asks whether the helper may control Mail. Approve both prompts once; later rebuilds retain the same signed identity.

The current Peacockery deployment reads its IMAP password from `~/.config/apple-mail-fast-mcp/imap-password-peacockery` and its HTTP token from `~/.config/apple-mail-fast-mcp/http-bearer-token`. Both files must be owned by the current user and use mode `0400` or `0600`.

Set `APPLE_MAIL_MCP_LOCAL_DB=1` in the service environment to enable millisecond metadata searches against Mail's read-only Envelope Index. This opt-in path requires Full Disk Access and automatically falls through to AppleScript if access or schema validation fails. IMAP retains priority because it reads the server-authoritative state.

See [Private remote service on macOS](docs/guides/REMOTE_SERVICE.md) for Tailscale Serve, client configuration, logs, and verification commands.

Use `uv run apple-mail-fleet --help` for scoped repair options such as `--local-only`, `--skip-deploy`, and `--skip-clients`.

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
