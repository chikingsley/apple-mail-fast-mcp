set shell := ["bash", "-cu"]

check:
    uv run ruff check src tests
    uv run ruff format --check src tests
    uv run ty check
    # pytest injects these fixtures and mock callables intentionally accept **kw.
    uv run vulture src tests --min-confidence 90 --ignore-names prompt,kw,isolated_drafts
    uv run apple-mail-project check-test-policy
    uv run apple-mail-project check-parity
    uv run apple-mail-project check-docs
    uv run apple-mail-project check-applescript
    uv run pytest
    uv --preview-features audit-command audit --locked
    uv build

fix:
    uv run ruff check --fix src tests
    uv run ruff format src tests

test:
    uv run apple-mail-project check-test-policy
    uv run pytest

live:
    MAIL_TEST_MODE=true uv run pytest tests/live --run-live -v

live-send:
    MAIL_TEST_MODE=true uv run pytest tests/live/test_mail_integration.py \
        -k send_email_arrives_in_inbox --run-live --run-send-live -v

install-macos:
    uv run apple-mail-install service
    uv run apple-mail-install ops

fleet:
    uv run apple-mail-fleet
