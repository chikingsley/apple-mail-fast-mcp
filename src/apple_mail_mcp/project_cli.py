"""Repository checks for the Python project."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

from .check_client_server_parity import main as check_parity
from .check_docs import main as check_docs
from .check_test_policy import main as check_test_policy

ROOT = Path(__file__).resolve().parents[2]


def check_applescript_safety() -> int:
    """Check the connector's AppleScript execution and escaping boundaries."""
    connector = ROOT / "src/apple_mail_mcp/mail_connector.py"
    lines = connector.read_text(encoding="utf-8").splitlines()
    text = "\n".join(lines)
    errors: list[str] = []
    if text.count("escape_applescript_string") < 2:
        errors.append("escape_applescript_string needs an import and a use")
    if text.count("sanitize_input") < 2:
        errors.append("sanitize_input needs an import and a use")
    subprocess_lines = [index for index, line in enumerate(lines, 1) if "subprocess.run" in line]
    if len(subprocess_lines) > 1:
        errors.append(f"direct subprocess.run calls found on lines {subprocess_lines}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("AppleScript safety passed")
    return 0


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as source:
        return str(tomllib.load(source)["project"]["version"])


def check_versions() -> int:
    """Require package metadata and the Python module to share one version."""
    expected = _project_version()
    init_match = re.search(
        r'^__version__ = "([^"]+)"',
        (ROOT / "src/apple_mail_mcp/__init__.py").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    versions: list[tuple[str, str]] = [
        ("src/apple_mail_mcp/__init__.py", init_match.group(1) if init_match else "")
    ]
    mismatches = [(path, value) for path, value in versions if value != expected]
    for path, value in versions:
        print(f"{path}: {value}")
    if mismatches:
        print(f"Expected every version to equal {expected}", file=sys.stderr)
        return 1
    print(f"All versions equal {expected}")
    return 0


def main() -> int:
    """Run one repository-owned operation."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "check-applescript",
        "check-docs",
        "check-parity",
        "check-test-policy",
        "check-versions",
    ):
        subparsers.add_parser(command)
    args = parser.parse_args()
    commands = {
        "check-applescript": check_applescript_safety,
        "check-docs": check_docs,
        "check-parity": check_parity,
        "check-test-policy": check_test_policy,
        "check-versions": check_versions,
    }
    return commands[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
