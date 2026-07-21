# Contributing

This is a source-deployed private service. Follow [AGENTS.md](AGENTS.md), keep changes focused, and run `just check` before handoff.

Tests are evidence-driven: add an issue-backed case under `tests/regressions` for a real bug, or a real boundary check under `tests/live`. Do not add coverage-driven test matrices. Changes to AppleScript, IMAP, SMTP, the native helper, or HTTP transport also require the relevant live verification.

Do not add PyPI publishing, `pip` instructions, redundant entry points, or release-only dependencies.
