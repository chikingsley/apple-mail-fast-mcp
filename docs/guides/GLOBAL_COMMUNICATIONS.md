# Shared Mail and Beeper

The maintained services run on Hochi using FastMCP 4.0.3. Mail retains its 25 existing operations and adds `junk_status`; Beeper retains the 12 tools exposed by Beeper Desktop. Each service exposes only `search`, `get_schema`, and `execute` through Code Mode. A sandbox limits each execution to 25 calls, 60 seconds, and 50 MB. Mail's existing mutation confirmation gates remain active.

## Routes and credentials

| Service | Local route                 | Remote route                                     |
| ------- | --------------------------- | ------------------------------------------------ |
| Mail    | `http://127.0.0.1:8765/mcp` | `https://hochi.tailbce39f.ts.net/apple-mail/mcp` |
| Beeper  | `http://127.0.0.1:8766/mcp` | `https://hochi.tailbce39f.ts.net/beeper/mcp`     |

Tailscale Serve publishes the routes only within the tailnet. Local clients use loopback because this Mac's Tailscale self-connection fails TLS. Both services require the gateway bearer credential. The underlying Beeper token remains in an owner-only file on Hochi; agents do not initiate separate Beeper OAuth flows. Beeper's own `/v0/mcp` endpoint and `/v1/spec` specification are distinct.

The stdio adapter pins FastMCP 3.4.5 for the established MCP handshake and interactive confirmation protocol. The hosted services use FastMCP 4.0.3. This compatibility boundary prevents modern protocol negotiation from disabling the existing confirmation flow.

The global `peacockery-mcp` adapter reads `~/.config/peacockery-communications/service-token` and `endpoints.json`. It needs no token environment variable inherited from a shell. Never print these credentials in diagnostics.

## Install clients

From a copy of this deployment tree, with the gateway credential already securely installed:

```sh
uv run --script deploy/install-communications.py --local-service
```

Omit `--local-service` on other machines. The installer backs up affected configurations, preserves unrelated MCP entries, installs both services in supported installed harnesses, and distributes the Mail and Messages skills. It supports Codex, Claude Code, OpenCode, Kimi, and an existing Claude Desktop configuration. Existing sessions may need to reload MCP connections. Keep the installer beside `communications-client.py` and the repository's `skills` directory when copying it to another host.

## Runtime

The Mail service, signed helper, Junk cleaner, and Beeper gateway are separate launchd jobs. Deploy them from one fixed release tree. Keep Beeper Desktop running; the gateway does not impersonate Beeper or own its account sessions. Restart only the affected job after changing code or credentials.

Mail metadata searches query the live read-only Envelope Index through the signed helper before attempting IMAP. The helper owns Full Disk Access, eliminating per-Python permission grants. Exact mailbox paths prevent suffix collisions, and results return numeric Mail IDs for scoped follow-up reads. Bodies and attachments may need provider downloads. A missing or incompatible index falls back to existing connectors; it must not be reported as an empty mailbox.

The cleaner uses `~/.config/apple-mail-fast-mcp/junk.sqlite` and an adjacent lock. It retains the previously active Junk policy and runs independently of client connections. Mail's own daily Trash setting handles permanent Trash erasure.

## Verification

For each installed adapter, establish an MCP connection, list the three exposed tools, discover a schema, and execute a bounded read. Verify Mail and Beeper separately from every reachable host. For Mail, enumerate enabled accounts and exact mailbox paths, then search metadata and read a selected result. Check authentication rejection with no token. A saved configuration alone is not a successful connection.

Run `just check` before deploying code. The regression suite covers folder matching, numeric message IDs, scoped reads, and confirmation preservation through Code Mode. Keep the previous release and backed-up launch definitions available for rollback. The older `apple-mail-fleet` command describes the earlier Mail-only deployment; use this guide for the shared adapter installation.

## Distribution inventory

`deploy/communications-fleet.json` is the explicit list of computers and agent-user accounts. It includes Hochi, gmk, hojo, hoboy, glkvm, and cica. A listed computer is not proof of installation: run the fleet check to establish current access. Cica uses its existing SSH service on port 2222 as `user18`; the inventory records that port and the Hochi key explicitly. Its route was recovered from gmk's chat-sync SSH configuration.

Run these commands on Hochi from this repository:

```sh
python3 deploy/communications-fleet.py list
python3 deploy/communications-fleet.py check --all
python3 deploy/communications-fleet.py install --host glkvm
```

Installation copies the pinned adapter and validated skills, securely transfers the shared gateway credential over SSH, updates supported installed agent configurations, and then verifies discovery, schema lookup, and a read operation on both services. Existing credentials are never included in the inventory or source archive. The target needs `uv`, SSH access, and enough space for managed Python and adapter dependencies. Windows targets also need PowerShell and `tar`.

Add future computers to the inventory with the correct SSH target and actual agent-user account, install that target, and verify it. Run `check --all` after changes; it exits unsuccessfully if any registered target is pending or fails. Installing for one OS user does not configure other users, containers, cloud agents, or already-running sessions on that computer.
