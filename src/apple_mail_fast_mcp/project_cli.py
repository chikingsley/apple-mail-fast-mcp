"""Repository checks and release utilities for the Python project."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import date
from pathlib import Path

from .check_client_server_parity import main as check_parity
from .check_docs import main as check_docs
from .check_test_policy import main as check_test_policy

ROOT = Path(__file__).resolve().parents[2]
VERSION_PATTERN = re.compile(r"^v\d+\.\d+\.\d+(?:-rc\d+)?$")


def _run(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, cwd=ROOT, check=check, text=True)


def check_applescript_safety() -> int:
    """Check the connector's AppleScript execution and escaping boundaries."""
    connector = ROOT / "src/apple_mail_fast_mcp/mail_connector.py"
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
    """Require package and integration manifests to share one version."""
    expected = _project_version()
    init_match = re.search(
        r'^__version__ = "([^"]+)"',
        (ROOT / "src/apple_mail_fast_mcp/__init__.py").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    versions: list[tuple[str, str]] = [
        ("src/apple_mail_fast_mcp/__init__.py", init_match.group(1) if init_match else "")
    ]
    for relative in (
        "mcpb/manifest.json",
        ".claude-plugin/marketplace.json",
        ".claude-plugin/plugin.json",
    ):
        path = ROOT / relative
        if path.exists():
            values = re.findall(r'"version"\s*:\s*"([^"]+)"', path.read_text(encoding="utf-8"))
            versions.extend((relative, value) for value in values)
    mismatches = [(path, value) for path, value in versions if value != expected]
    for path, value in versions:
        print(f"{path}: {value}")
    if mismatches:
        print(f"Expected every version to equal {expected}", file=sys.stderr)
        return 1
    print(f"All versions equal {expected}")
    return 0


def check_changelog(tag: str) -> int:
    """Require a dated changelog entry for one release tag."""
    version = tag.removeprefix("v")
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(rf"(?mi)^## \[{re.escape(version)}\].*$", text)
    if match is None:
        print(f"CHANGELOG.md needs an entry for {version}", file=sys.stderr)
        return 1
    entry = match.group(0)
    if re.search(r"TBD|UNRELEASED|YYYY", entry, re.IGNORECASE):
        print(f"CHANGELOG.md needs a release date for {version}", file=sys.stderr)
        return 1
    release_date = re.search(r"\d{4}-\d{2}-\d{2}", entry)
    if release_date is None:
        print(f"Use: ## [{version}] - {date.today().isoformat()}", file=sys.stderr)
        return 1
    print(f"CHANGELOG {version}: {release_date.group(0)}")
    return 0


def build_mcpb() -> int:
    """Validate and pack the Claude Desktop MCP bundle."""
    manifest = ROOT / "mcpb/manifest.json"
    version = str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    output = ROOT / f"dist/apple-mail-fast-mcp-{version}.mcpb"
    with tempfile.TemporaryDirectory(prefix="apple-mail-mcpb-") as temporary:
        stage = Path(temporary)
        shutil.copy2(manifest, stage / "manifest.json")
        for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
            shutil.copy2(ROOT / name, stage / name)
        shutil.copytree(ROOT / "src/apple_mail_fast_mcp", stage / "src/apple_mail_fast_mcp")
        output.parent.mkdir(exist_ok=True)
        _run(
            ["npx", "--yes", "@anthropic-ai/mcpb@latest", "validate", str(stage / "manifest.json")]
        )
        _run(["npx", "--yes", "@anthropic-ai/mcpb@latest", "pack", str(stage), str(output)])
        _run(["npx", "--yes", "@anthropic-ai/mcpb@latest", "info", str(output)], check=False)
    print(f"Built {output}")
    return 0


def create_tag(tag: str) -> int:
    """Create one validated annotated release tag."""
    if VERSION_PATTERN.fullmatch(tag) is None:
        print("Tag must use vX.Y.Z or vX.Y.Z-rcN", file=sys.stderr)
        return 1
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    expected_branch = "release/" if "-rc" in tag else "main"
    if (expected_branch == "main" and branch != "main") or (
        expected_branch == "release/" and not branch.startswith("release/")
    ):
        print(f"{tag} cannot be created from {branch}", file=sys.stderr)
        return 1
    existing = subprocess.run(
        ["git", "tag", "--list", tag], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    if existing:
        print(f"Tag already exists: {tag}", file=sys.stderr)
        return 1
    if check_versions() or check_changelog(tag):
        return 1
    _run(["git", "tag", "-a", tag, "-m", f"Release {tag.removeprefix('v')}"])
    print(f"Created {tag}; push with: git push origin {tag}")
    return 0


def main() -> int:
    """Run one repository-owned operation."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "build-mcpb",
        "check-applescript",
        "check-docs",
        "check-parity",
        "check-test-policy",
        "check-versions",
    ):
        subparsers.add_parser(command)
    changelog = subparsers.add_parser("check-changelog")
    changelog.add_argument("tag")
    tag = subparsers.add_parser("create-tag")
    tag.add_argument("tag")
    args = parser.parse_args()
    commands = {
        "build-mcpb": build_mcpb,
        "check-applescript": check_applescript_safety,
        "check-docs": check_docs,
        "check-parity": check_parity,
        "check-test-policy": check_test_policy,
        "check-versions": check_versions,
    }
    if args.command == "check-changelog":
        return check_changelog(args.tag)
    if args.command == "create-tag":
        return create_tag(args.tag)
    return commands[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
