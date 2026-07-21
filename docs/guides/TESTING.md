# Testing

The suite protects behavior, not a coverage percentage. `scripts/check_test_policy.py` rejects tests outside the two supported categories and rejects regression tests that do not identify an issue, bug, or regression.

## Regressions

`tests/regressions` contains small deterministic checks for previously observed failures. The normal command runs this category only:

```bash
just test
```

A new regression must explain what broke and include an issue number or explicit bug/regression wording in its own name or docstring. Do not add routine happy-path permutations, generated edge-case matrices, or tests solely to execute uncovered lines.

The real stdio handshake belongs here because it pins transport regression #50 without pretending that mocked tool calls are end-to-end behavior.

## Live behavior

`tests/live` talks to real AppleScript, Mail.app, IMAP, SMTP, or MCP boundaries. It is disabled unless `--run-live` is present, and account-changing tests also require `MAIL_TEST_ACCOUNT`:

```bash
MAIL_TEST_ACCOUNT=simon@peacockery.studio just live
```

Live tests may create and remove drafts, rules, messages, and fixture mailboxes. Use an account where those mutations are acceptable. `MAIL_TEST_MODE=true` restricts account-gated operations to `MAIL_TEST_ACCOUNT` and prevents ordinary sends.

## Real send and delivery

The outbound test has a second command-line gate and requires an explicit recipient routed back to the selected inbox:

```bash
MAIL_TEST_ACCOUNT=simon@peacockery.studio \
MAIL_LIVE_RECIPIENT=ci@peacockery.studio \
just live-send
```

The test submits one real message through the account’s SMTP path and polls `MAIL_LIVE_MAILBOX` (default `INBOX`) for up to `MAIL_LIVE_DELIVERY_TIMEOUT` seconds (default `90`). It skips unless both `--run-send-live` and `MAIL_LIVE_RECIPIENT` are present.

## Full local gate

```bash
just check
```

This runs linting, formatting, static typing, dead-code detection, repository policy checks, the regression suite, vulnerability auditing, and a local package build. A green regression suite does not prove the live macOS deployment; run the relevant live command after changes to AppleScript, IMAP, SMTP, the native helper, or the remote transport.
