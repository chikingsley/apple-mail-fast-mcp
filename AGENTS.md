# Project rules

- Use `uv` for Python versions, dependencies, locking, execution, auditing, and builds. The project targets Python 3.14 and keeps a `src` layout with one console script.
- Use Ruff with `ALL`, `ty` with warnings as errors, Vulture, pytest, and `uv audit`. Run `just check` before handoff.
- This is a private source deployment. Do not add PyPI publishing, `pip` installation instructions, or release machinery that exists only for a package registry. `uv build` remains a local installability check.
- Tests belong in exactly two categories: `tests/regressions` for a documented issue or bug and `tests/live` for real Apple Mail, IMAP, SMTP, or MCP behavior. Do not add coverage-driven or routine mocked tests. `scripts/check_test_policy.py` enforces this boundary.
- Live tests require `--run-live` and an explicit `MAIL_TEST_ACCOUNT`. A real send additionally requires `--run-send-live` and `MAIL_LIVE_RECIPIENT`.
- Keep all AppleScript execution behind `AppleMailConnector._run_applescript`. Sanitize and escape user-controlled values before interpolation, and preserve typed error translation.
- Treat the resident macOS helper, its owner-only Unix socket, Tailscale Serve, and bearer-token authentication as the supported remote deployment path.
