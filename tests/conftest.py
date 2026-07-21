"""Pytest configuration and fixtures."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add custom command line options."""
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run tests against the configured Apple Mail and IMAP accounts",
    )
    parser.addoption(
        "--run-send-live",
        action="store_true",
        default=False,
        help="Allow the opt-in live test that sends a real email",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest."""
    config.addinivalue_line("markers", "live: test real Apple Mail, IMAP, SMTP, or MCP behavior")
    config.addinivalue_line("markers", "live_send: send a real message when explicitly enabled")
