# Apple Mail and shared Beeper MCP

Private communications services hosted on Hochi. All configured agents reach the same Mail.app and Beeper Desktop accounts through authenticated local or Tailscale connections.

FastMCP 4.0.3 Code Mode exposes three tools per service while retaining the full underlying catalog: 27 Mail operations and Beeper Desktop's 12 operations. Mail includes search, bodies, attachments, threads, statistics, native drafts and saved MIME inspection, sending, mailbox and rule changes, templates, and Junk status.

Mail metadata uses a read-only database query through the signed Full Disk Access helper. Message bodies and mutations use the existing Mail/IMAP connectors. The standalone Junk cleaner remains independent of agent sessions. Beeper uses its own MCP with one shared upstream credential stored only on Hochi.

See [installation, routes, verification, and rollback](docs/guides/GLOBAL_COMMUNICATIONS.md) for the current deployment. [Tool reference](docs/reference/TOOLS.md) documents underlying Mail operations; agents discover these through `search` and `get_schema`, then invoke them inside `execute`.

## Development

Use Python 3.14 and `uv`:

```sh
uv sync --locked
just check
```

The original checkout may contain an unfinished replacement. Preserve it; develop and deploy from a reviewed release tree. Service credentials, account data, and the Junk ledger stay outside source control.
