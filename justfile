set shell := ["bash", "-cu"]

check:
    uv run ruff check src tests scripts/check_*.py
    uv run ruff format --check src tests scripts/check_*.py
    uv run ty check
    # pytest injects these fixtures and mock callables intentionally accept **kw.
    uv run vulture src tests --min-confidence 90 --ignore-names prompt,kw,isolated_drafts
    uv run scripts/check_test_policy.py
    uv run scripts/check_client_server_parity.py
    uv run scripts/check_docs.py
    ./scripts/check_applescript_safety.sh
    uv run pytest
    uv --preview-features audit-command audit --locked
    uv build

fix:
    uv run ruff check --fix src tests scripts/check_*.py
    uv run ruff format src tests scripts/check_*.py

test:
    uv run scripts/check_test_policy.py
    uv run pytest

live:
    MAIL_TEST_MODE=true uv run pytest tests/live --run-live -v

live-send:
    MAIL_TEST_MODE=true uv run pytest tests/live/test_mail_integration.py \
        -k send_email_arrives_in_inbox --run-live --run-send-live -v

install-macos:
    ./scripts/install-macos-launch-agent.sh
