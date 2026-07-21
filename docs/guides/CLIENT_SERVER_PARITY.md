# Client/server parity

Every public method on `AppleMailConnector` must either be exposed as an MCP tool in `server.py` or appear in the explicit intentionally-internal set in [`check_client_server_parity.py`](../../scripts/check_client_server_parity.py).

Run the check directly with:

```bash
uv run scripts/check_client_server_parity.py
```

It is also part of `just check`. The script fails when a connector capability has no tool and no intentional classification, or when an internal entry becomes stale after a rename, removal, or new tool wrapper.

When adding a public connector method, expose it with `@_tool(...)` in `server.py` and document it in [Tools](../reference/TOOLS.md). If it is genuinely only an implementation primitive, add its name to `INTENTIONALLY_INTERNAL`; that set is a shrinking ratchet, not a parking lot.
