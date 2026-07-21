"""Tests for ImapConnectionPool (issue #75).

The pool reuses IMAPClient sessions across calls keyed by (host, email).
These tests cover:

- Reuse: same key → same client; login() runs once across N calls.
- Per-account isolation: different keys → independent clients/locks.
- Idle reconnect: stale entries are dropped + reopened transparently.
- Error invalidation: LoginError / IMAPClientError / OSError drops the
  cached entry so the next call gets a fresh connection.
- Per-connection locking: one client serializes use across threads.
- close(): logs out every cached client.
- ImapConnector wiring: methods route through the pool when one is set.

The IMAPClient class itself is mocked at the module level — these are
unit tests, no real network.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_fast_mcp.imap_connector import (
    ImapConnectionPool,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pool() -> ImapConnectionPool:
    """Default pool — production idle threshold."""
    return ImapConnectionPool()


@pytest.fixture
def short_idle_pool() -> ImapConnectionPool:
    """Pool with a tiny idle window for the reconnect tests."""
    return ImapConnectionPool(idle_timeout_s=0.01)


# ---------------------------------------------------------------------------
# Reuse + per-account isolation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Idle reconnect
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Error invalidation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Per-connection locking
# ---------------------------------------------------------------------------


class TestClose:
    @patch("apple_mail_fast_mcp.imap_connector.IMAPClient")
    def test_close_waits_for_in_flight_session_holder(
        self, mock_cls: MagicMock, pool: ImapConnectionPool
    ) -> None:
        """#171: close() must acquire entry.lock before logout() so
        it doesn't race a session()-holder actively using the client.
        Latent today (FastMCP single-threaded) but a real correctness
        hazard for the #127 atexit hook + future threading. Pattern
        mirrors test_same_key_serializes.
        """
        client = MagicMock()
        mock_cls.return_value = client

        inside = threading.Event()
        release = threading.Event()
        logout_observed_during_session = threading.Event()

        def thread_a() -> None:
            """Holds the session for key K until 'release' is set."""
            with pool.session("h", 993, "u@e.com", "pw", 3.0):
                inside.set()
                release.wait(timeout=2.0)
                # If close() jumped the gun, logout() would have
                # fired by now — record that as the bug.
                if client.logout.called:
                    logout_observed_during_session.set()

        ta = threading.Thread(target=thread_a)
        ta.start()
        assert inside.wait(timeout=2.0), "thread_a never entered session"

        # Run close() in its own thread so the test doesn't deadlock
        # if close() blocks waiting on entry.lock (which is the
        # expected behavior post-fix).
        close_done = threading.Event()

        def close_runner() -> None:
            pool.close()
            close_done.set()

        tc = threading.Thread(target=close_runner)
        tc.start()

        # close() should be blocked on entry.lock right now. Give it
        # a moment to (incorrectly) call logout if the bug is present.
        time.sleep(0.05)
        assert not close_done.is_set(), (
            "close() returned while session-holder was still inside; "
            "expected it to block on entry.lock"
        )
        assert not client.logout.called, (
            "close() called logout() before session-holder released "
            "entry.lock — this is the #171 race"
        )

        # Release thread_a; close() should now proceed.
        release.set()
        ta.join(timeout=2.0)
        tc.join(timeout=2.0)
        assert close_done.is_set(), "close() never finished"
        assert client.logout.called, "close() never called logout()"
        assert not logout_observed_during_session.is_set()


# ---------------------------------------------------------------------------
# ImapConnector wiring
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sanity
# ---------------------------------------------------------------------------
