# Development

## Environment

The project targets Python 3.14 and uses `uv` for the interpreter, lockfile, environment, commands, audit, and build:

```bash
uv python install 3.14
uv sync
```

Do not activate `.venv`, run bare `python` or `pip`, or add PyPI publishing. `uv build` remains part of the local gate because the LaunchAgent installs and runs the source package.

## Canonical check

Run:

```bash
just check
```

The recipe executes Ruff `ALL`, Ruff format checking, `ty`, Vulture, the test-policy check, connector/tool parity, documentation drift, AppleScript safety, regression tests, `uv audit --locked`, and `uv build`.

Use `just fix` for Ruff’s safe automatic fixes. Resolve or narrowly document any remaining rule exception in `pyproject.toml`; do not scatter unexplained `noqa` comments.

## Code boundaries

- `server.py` owns MCP registration, input validation, confirmation, transport configuration, and structured responses.
- `mail_connector.py` owns domain orchestration and the single AppleScript execution boundary.
- `imap_connector.py` owns IMAP operations and pooling.
- `smtp_sender.py` owns SMTP submission.
- `native/macos-helper` owns the stable macOS Automation process and local Unix socket.

Every user-controlled value interpolated into AppleScript must be sanitized and escaped. A configured helper socket must fail closed. Destructive tools must preserve their confirmation and test-mode gates.

## Test changes

Add a regression only for an observed, documented failure. Add a live test when proof requires a real platform or network boundary. See [Testing](TESTING.md).

After changing the native helper or launch configuration, rerun `just install-macos` on Hochi and verify the authenticated Tailscale endpoint described in [Private remote service on macOS](REMOTE_SERVICE.md).
