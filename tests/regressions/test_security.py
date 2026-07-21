"""Unit tests for security module."""

from __future__ import annotations

from typing import Any

import pytest

from apple_mail_fast_mcp.security import (
    _get_test_account_identifiers,
    check_test_mode_safety,
    operation_logger,
)

# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# check_rate_limit helper
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test-mode safety
# ---------------------------------------------------------------------------


class TestCheckTestModeSafety:
    """Tests for check_test_mode_safety helper."""

    def setup_method(self) -> None:
        operation_logger.operations.clear()
        # Clear the per-process UUID-resolution cache so tests don't see
        # cached identifiers from other tests' mocked subprocess returns.
        _get_test_account_identifiers.cache_clear()

    @pytest.fixture(autouse=True)
    def _stub_uuid_osascript(self, monkeypatch: Any) -> None:
        """The safety gate shells to ``osascript`` to enrich the identifier
        set with the account's UUID. In CI that first osascript triggers a
        ~20-30s Mail.app cold-launch (#408). Default it to a benign
        "not found" so the gate falls back to name-only matching — which is
        all these name-based tests need. The two UUID-path tests re-stub
        ``subprocess.run`` in their own body (shadowing this).
        """
        monkeypatch.setattr(
            "apple_mail_fast_mcp.security.subprocess.run",
            lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": "n/a"})(),
        )

    # --- Rule-mutation prefix gate (#63) -------------------------------

    def test_send_blocked_when_recipients_none_in_test_mode(self, monkeypatch: Any) -> None:
        """#175: implicit-reply path (no explicit to/cc/bcc, Mail.app
        derives at send time) reaches the gate with recipients=None.
        Must reject — the safety check has nothing to verify.
        """
        monkeypatch.setenv("MAIL_TEST_MODE", "true")

        result = check_test_mode_safety("create_draft", recipients=None)
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "explicit recipients" in result["error"]

    def test_send_blocked_when_recipients_empty_in_test_mode(self, monkeypatch: Any) -> None:
        """#175: same as above but with explicit empty list."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")

        result = check_test_mode_safety("create_draft", recipients=[])
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "explicit recipients" in result["error"]

    def test_send_blocked_for_update_draft_empty_recipients(self, monkeypatch: Any) -> None:
        """#175: same gap applies to update_draft's send path."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")

        result = check_test_mode_safety("update_draft", recipients=[])
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "update_draft" in result["error"]

    def test_send_empty_recipients_passes_outside_test_mode(self, monkeypatch: Any) -> None:
        """#175: regression guard — the empty-recipients reject is
        scoped to test mode. Outside test mode, the gate early-returns
        None and the new branch is never reached.
        """
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)

        assert check_test_mode_safety("create_draft", recipients=None) is None
        assert check_test_mode_safety("create_draft", recipients=[]) is None

    def test_non_send_operation_with_empty_recipients_unchanged(self, monkeypatch: Any) -> None:
        """#175: regression guard — the new empty-recipients reject
        only fires for operations in SEND_OPERATIONS. Other ops with
        empty recipients (which is meaningless for them anyway) are
        unaffected.
        """
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        # delete_messages isn't a send op — the new branch shouldn't fire.
        assert check_test_mode_safety("delete_messages", recipients=None) is None
        assert check_test_mode_safety("delete_messages", recipients=[]) is None
