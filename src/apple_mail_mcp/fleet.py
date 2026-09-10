"""Versioned deployment and cross-harness distribution for Apple Mail MCP."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shlex
from pathlib import Path, PureWindowsPath

from fastmcp import Client

from .fleet_clients import (
    configure_local_clients,
    configure_remote_clients,
    verify_local_clients,
    verify_remote_clients,
)
from .fleet_support import (
    MCP_URL,
    FleetHost,
    install_local_skill,
    install_local_support,
    install_remote_skill,
    install_remote_support,
    resolve_host,
    run,
    run_bytes,
    sha256_tree,
)

DEFAULT_REMOTES = ("hochi", "hojo", "hoboy")
DEFAULT_MAIL_HOST = "hochi"
REMOTE_RELEASE_ROOT = ".local/share/peacockery/apple-mail/releases"


def parse_args() -> argparse.Namespace:
    """Parse the fleet release command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mail-host", default=DEFAULT_MAIL_HOST)
    parser.add_argument("--remote", action="append", dest="remotes")
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--skip-deploy", action="store_true")
    parser.add_argument("--skip-clients", action="store_true")
    parser.add_argument("--skip-skill", action="store_true")
    args = parser.parse_args()
    args.remotes = list(dict.fromkeys(args.remotes or DEFAULT_REMOTES))
    return args


def repository_root() -> Path:
    """Return the canonical repository root."""
    return Path(__file__).resolve().parents[2]


def committed_revision(repo: Path) -> str:
    """Require a clean commit already present on origin/main."""
    if run(["git", "status", "--porcelain"], capture=True, cwd=repo).stdout.strip():
        raise RuntimeError("fleet release requires a clean working tree")
    revision = run(["git", "rev-parse", "HEAD"], capture=True, cwd=repo).stdout.strip()
    branch = run(["git", "branch", "--show-current"], capture=True, cwd=repo).stdout.strip()
    if branch != "main":
        raise RuntimeError(f"fleet release requires main, found {branch!r}")
    remote = run(["git", "ls-remote", "origin", "refs/heads/main"], capture=True, cwd=repo).stdout
    remote_revision = remote.split()[0] if remote.split() else ""
    if revision != remote_revision:
        raise RuntimeError("local HEAD must equal origin/main before fleet release")
    return revision


def _remote_release_path(host: FleetHost, revision: str) -> str:
    return f"{host.home}/{REMOTE_RELEASE_ROOT}/{revision}"


def deploy_release(repo: Path, host: FleetHost, revision: str) -> str:
    """Install one immutable tracked-tree release without touching Mac checkouts."""
    if host.platform != "unix":
        raise RuntimeError("the Apple Mail service host must be macOS")
    archive = run_bytes(
        ["git", "archive", "--format=tar", revision],
        capture=True,
        cwd=repo,
    ).stdout
    release_path = _remote_release_path(host, revision)
    prepare = f"mkdir -p {shlex.quote(release_path)} && tar -xf - -C {shlex.quote(release_path)}"
    run_bytes(["ssh", host.name, prepare], input_data=archive)
    install = (
        f"cd {shlex.quote(release_path)} && "
        "uv run --locked apple-mail-install service && "
        "uv run --locked apple-mail-install ops && "
        "tailscale serve --bg --set-path=/apple-mail http://127.0.0.1:8765 && "
        f"ln -sfn {shlex.quote(release_path)} "
        f"{shlex.quote(host.home + '/.local/share/peacockery/apple-mail/current')}"
    )
    run(["ssh", host.name, f"zsh -lic {shlex.quote(install)}"])
    return release_path


def read_mail_host_token(host: FleetHost) -> str:
    """Read the owner-only service token over SSH without printing it."""
    token_path = f"{host.home}/.config/apple-mail-fast-mcp/http-bearer-token"
    result = run(["ssh", host.name, "cat", token_path], capture=True)
    token = result.stdout.strip()
    if len(token) < 32 or any(character.isspace() for character in token):
        raise RuntimeError("mail host returned an invalid bearer token")
    return token


def verify_mail_host(host: FleetHost, release_path: str | None) -> None:
    """Verify launchd, Tailscale Serve, and the installed release path."""
    expected = release_path or f"{host.home}/.local/share/peacockery/apple-mail/current"
    command = (
        "set -eu;"
        "launchctl print gui/$(id -u)/studio.peacockery.apple-mail-mcp >/dev/null;"
        "launchctl print gui/$(id -u)/studio.peacockery.apple-mail-mcp-helper >/dev/null;"
        "launchctl print gui/$(id -u)/studio.peacockery.apple-mail-ops >/dev/null;"
        'test ! -e "$HOME/Library/LaunchAgents/'
        'studio.peacockery.apple-mail-junk-flag-cleaner.plist";'
        "tailscale serve status | grep -F '/apple-mail proxy http://127.0.0.1:8765' >/dev/null;"
        f"grep -F {shlex.quote(expected)} "
        '"$HOME/Library/LaunchAgents/studio.peacockery.apple-mail-mcp.plist" >/dev/null;'
        'grep -F -- "--bearer-token-file" '
        '"$HOME/Library/LaunchAgents/studio.peacockery.apple-mail-mcp.plist" >/dev/null'
    )
    run(["ssh", host.name, command])


async def _verify_endpoint(token: str) -> tuple[int, int]:
    """Verify authenticated MCP discovery and one live read-only call."""
    async with Client(MCP_URL, auth=token) as client:
        tools = await client.list_tools()
        result = await client.call_tool("list_accounts", {})
    data = result.data if isinstance(result.data, dict) else {}
    accounts = data.get("accounts", [])
    return len(tools), len(accounts) if isinstance(accounts, list) else 0


def verify_endpoint(token: str) -> tuple[int, int]:
    """Run the async endpoint verification."""
    return asyncio.run(_verify_endpoint(token))


def verify_skill_hashes(skill_dir: Path, hosts: list[FleetHost]) -> str:
    """Verify the canonical SKILL.md on every installed target."""
    source = skill_dir / "SKILL.md"
    source_hash = sha256_tree(skill_dir)
    local_content = source.read_bytes()
    file_hash = hashlib.sha256(local_content).hexdigest()
    for relative in (
        ".codex/skills/apple-mail/SKILL.md",
        ".agents/skills/apple-mail/SKILL.md",
        ".claude/skills/apple-mail/SKILL.md",
        ".kimi-code/skills/apple-mail/SKILL.md",
    ):
        if hashlib.sha256((Path.home() / relative).read_bytes()).hexdigest() != file_hash:
            raise RuntimeError(f"local skill hash mismatch at {relative}")
    for host in hosts:
        for relative in (
            ".codex/skills/apple-mail/SKILL.md",
            ".agents/skills/apple-mail/SKILL.md",
            ".claude/skills/apple-mail/SKILL.md",
            ".kimi-code/skills/apple-mail/SKILL.md",
        ):
            if host.platform == "windows":
                windows_path = PureWindowsPath(host.home, *Path(relative).parts)
                command = (
                    f"(Get-FileHash -Algorithm SHA256 -LiteralPath '{windows_path}').Hash.ToLower()"
                )
            else:
                command = f"shasum -a 256 {shlex.quote(host.home + '/' + relative)}"
            output = run(["ssh", host.name, command], capture=True).stdout.strip().split()[0]
            if output.lower() != file_hash:
                raise RuntimeError(f"{host.name} skill hash mismatch at {relative}")
    return source_hash


def main() -> None:
    """Deploy, configure, distribute, and verify Apple Mail MCP."""
    args = parse_args()
    repo = repository_root()
    revision = committed_revision(repo)
    mail_host = resolve_host(args.mail_host)
    release_path = None if args.skip_deploy else deploy_release(repo, mail_host, revision)
    token = read_mail_host_token(mail_host)
    hosts = [] if args.local_only else [resolve_host(name) for name in args.remotes]

    install_local_support(token)
    for host in hosts:
        install_remote_support(host, token)

    skill_dir = repo / "skills/apple-mail"
    if not args.skip_skill:
        install_local_skill(skill_dir)
        for host in hosts:
            install_remote_skill(host, skill_dir)

    client_states: dict[str, list[str]] = {}
    if not args.skip_clients:
        client_states["local"] = configure_local_clients()
        for host in hosts:
            client_states[host.name] = configure_remote_clients(host)

    verify_mail_host(mail_host, release_path)
    tool_count, account_count = verify_endpoint(token)
    if not args.skip_clients:
        verify_local_clients(client_states["local"])
        for host in hosts:
            verify_remote_clients(host, client_states[host.name])
    skill_hash = "skipped" if args.skip_skill else verify_skill_hashes(skill_dir, hosts)
    print(
        json.dumps(
            {
                "accounts": account_count,
                "clients": client_states,
                "endpoint": MCP_URL,
                "release": revision,
                "skill_hash": skill_hash,
                "tools": tool_count,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
