# Shared Mail and Beeper

The maintained services run on Hochi using FastMCP 4.0.3. Mail exposes 27 operations, including `junk_status` and `inspect_draft`; Beeper retains the 12 tools exposed by Beeper Desktop. Each service exposes only `search`, `get_schema`, and `execute` through Code Mode. A sandbox limits each execution to 25 calls, 60 seconds, and 50 MB. Mail's existing mutation confirmation gates remain active.

## Routes and credentials

| Service | Local route                 | Remote route                                     |
| ------- | --------------------------- | ------------------------------------------------ |
| Mail    | `http://127.0.0.1:8765/mcp` | `https://hochi.tailbce39f.ts.net/apple-mail/mcp` |
| Beeper  | `http://127.0.0.1:8766/mcp` | `https://hochi.tailbce39f.ts.net/beeper/mcp`     |

Tailscale Serve publishes the routes only within the tailnet. Local clients use loopback because this Mac's Tailscale self-connection fails TLS. Both services require the gateway bearer credential. The underlying Beeper token remains in an owner-only file on Hochi; agents do not initiate separate Beeper OAuth flows. Beeper's own `/v0/mcp` endpoint and `/v1/spec` specification are distinct.

The shared adapter and services use FastMCP 4.0.3. Its proxy negotiates the calling client protocol automatically. Mail supports modern input-required confirmation round trips. Execute mutations as one tool_name/arguments operation; code is reserved for read workflows, so a confirmation retry cannot replay previous writes.

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

### Native draft fidelity

`create_draft` defaults to `composition_mode="mail_defaults"`. The signed helper edits Mail's native composer so the account's signature, typing attributes, and quoted history remain under Mail's control. This requires **Apple Mail MCP Helper** Accessibility access, separate from Mail Automation and Full Disk Access. Native Mail defaults may differ from the user's Outlook setup; inspect a same-account baseline and the actual configured signature/font before claiming fidelity. The helper refuses an unavailable native path instead of silently switching to plain-text replacement. Query the actual resident process with:

```sh
"/Users/simonpeacocks/Applications/Apple Mail MCP Helper.app/Contents/MacOS/AppleMailMCPHelper" --accessibility-check
```

This diagnostic relays to the owner-only resident socket and returns its PID, bundle path, and trust state. It does not inspect the System Settings switch. If the user sees the switch enabled but the process reports false, inspect the displayed identity, launchd process, code-signing requirement, and relevant macOS TCC logs; do not repeatedly assert the user has not enabled access. Do not edit protected permission databases or bypass the OS grant. `--composition-check` also relays to the resident helper and performs a native preflight without creating a draft.

After saving, use `inspect_draft` with the exact account, mailbox, and message ID. Supply expected parent RFC Message-ID, authored text, and signature text when available. Inspect the reply headers, recipients, HTML quote/signature regions, and font declarations. Native text readback and MIME checks are separate evidence; neither alone proves rendered appearance or the conversation grouping shown by a mail client. Return incomplete checks explicitly. Plain `get_messages` content is not a layout or thread verification.

Use the bounded `get_thread` account/mailbox arguments with explicit source, Sent, and Drafts mailbox paths. The result describes the searched scope, caps, and partial failures. Avoid repeating an unscoped scan after a timeout. After any uncertain draft creation, inspect existing composers and saved drafts before retrying.

The helper source and its installed executable must be rebuilt together. The September 2026 incident exposed an older installed helper that terminated with SIGPIPE when a timed-out client disconnected. The native regression uses an isolated socket to verify that a late reply and a broken pipe leave the helper alive; it never sends mail. Raw AppleScript list/record results now require explicit JSON serialization instead of becoming an empty success response.

### Connection checks

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
