"""Regressions for cross-platform fleet installation."""

import sys

import pytest

from apple_mail_fast_mcp.fleet_support import run


@pytest.mark.allow_real_io
def test_regression_platform_probe_captures_expected_foreign_shell_errors() -> None:
    """Regression: Windows detection must not print the failed Unix probe to operators."""
    result = run(
        [sys.executable, "-c", "import sys; print('foreign shell', file=sys.stderr)"],
        check=False,
        capture=True,
    )

    assert result.returncode == 0
    assert result.stderr.strip() == "foreign shell"
