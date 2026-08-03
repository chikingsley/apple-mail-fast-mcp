"""Shared installation primitives for the Apple Mail fleet release."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

MCP_NAME = "apple-mail"
MCP_URL = "https://hochi.tailbce39f.ts.net/apple-mail/mcp"
MCP_TOKEN_ENV = "APPLE_MAIL_MCP_BEARER_TOKEN"
FASTMCP_VERSION = "3.4.5"
UNIX_SKILL_DIRS = (
    ".codex/skills/apple-mail",
    ".agents/skills/apple-mail",
    ".claude/skills/apple-mail",
    ".kimi-code/skills/apple-mail",
)


@dataclass(frozen=True)
class FleetHost:
    """One SSH-reachable agent host."""

    name: str
    home: str
    platform: str


def run(
    command: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one text subprocess with consistent failure handling."""
    return subprocess.run(
        command,
        check=check,
        env=env,
        cwd=cwd,
        input=input_text,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )


def run_bytes(
    command: list[str],
    *,
    input_data: bytes | None = None,
    capture: bool = False,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one binary subprocess for tar streaming."""
    return subprocess.run(
        command,
        check=True,
        cwd=cwd,
        input=input_data,
        stdout=subprocess.PIPE if capture else None,
    )


def sha256_tree(root: Path) -> str:
    """Hash file names, modes, and contents for distribution verification."""
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update((path.stat().st_mode & 0o777).to_bytes(4, "big"))
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def tar_payload(files: dict[str, tuple[bytes, int]]) -> bytes:
    """Build a portable archive containing exact file modes."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, (content, mode) in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def resolve_host(name: str) -> FleetHost:
    """Resolve a remote host's platform and home directory."""
    unix = run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", name, 'printf "%s" "$HOME"'],
        check=False,
        capture=True,
    )
    if unix.returncode == 0 and unix.stdout.startswith("/"):
        return FleetHost(name=name, home=unix.stdout.strip(), platform="unix")
    windows = run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", name, "[Console]::Write($HOME)"],
        capture=True,
    )
    return FleetHost(name=name, home=windows.stdout.strip(), platform="windows")


def remote_executable(host: FleetHost, name: str) -> str | None:
    """Find an installed harness executable without changing remote state."""
    if host.platform == "windows":
        command = (
            f"$cmd=Get-Command {name} -ErrorAction SilentlyContinue; "
            "if($cmd){[Console]::Write($cmd.Source)}"
        )
        result = run(["ssh", host.name, command], check=False, capture=True)
        return result.stdout.strip() or None
    candidates = {
        "codex": ["/Applications/Codex.app/Contents/Resources/codex", "$HOME/.local/bin/codex"],
        "claude": ["$HOME/.local/bin/claude"],
        "kimi": ["$HOME/.kimi-code/bin/kimi"],
        "opencode": [
            "$HOME/.opencode/bin/opencode",
            "$HOME/.bun/bin/opencode",
            "/opt/homebrew/bin/opencode",
        ],
    }
    checks = " ".join(
        f'"{candidate}"' if candidate.startswith("$HOME/") else shlex.quote(candidate)
        for candidate in candidates.get(name, ())
    )
    shell = (
        f"command -v {shlex.quote(name)} || for candidate in {checks}; do "
        '[ -x "$candidate" ] && printf "%s\\n" "$candidate" && break; done'
    )
    result = run(
        ["ssh", host.name, f"zsh -lic {shlex.quote(shell)}"],
        check=False,
        capture=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else None


def support_files(token: str) -> dict[str, tuple[bytes, int]]:
    """Return owner-only client credentials and the stdio proxy wrapper."""
    authorization = f"Bearer {token}\n"
    proxy = {
        "mcpServers": {
            MCP_NAME: {
                "auth": token,
                "transport": "http",
                "url": MCP_URL,
            }
        }
    }
    wrapper = f"""#!/bin/sh
set -eu
exec uvx --from fastmcp=={FASTMCP_VERSION} fastmcp run \\
  "$HOME/.config/apple-mail-fast-mcp/proxy.json" --transport stdio --no-banner
"""
    header_helper = """#!/bin/sh
set -eu
authorization=$(cat "$HOME/.config/apple-mail-fast-mcp/mcp-authorization")
printf '{"Authorization":"%s"}\\n' "$authorization"
"""
    return {
        ".config/apple-mail-fast-mcp/mcp-token": (f"{token}\n".encode(), 0o600),
        ".config/apple-mail-fast-mcp/mcp-authorization": (authorization.encode(), 0o600),
        ".config/apple-mail-fast-mcp/proxy.json": (
            (json.dumps(proxy, separators=(",", ":")) + "\n").encode(),
            0o600,
        ),
        ".local/bin/apple-mail-mcp-headers": (header_helper.encode(), 0o700),
        ".local/bin/apple-mail-mcp-proxy": (wrapper.encode(), 0o700),
    }


def install_local_support(token: str) -> None:
    """Install credentials and helpers for local harnesses."""
    home = Path.home()
    for relative, (content, mode) in support_files(token).items():
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(mode)
    environment_dir = home / ".config/environment.d"
    environment_dir.mkdir(parents=True, exist_ok=True)
    environment_file = environment_dir / "60-apple-mail-mcp.conf"
    environment_file.write_text(f"{MCP_TOKEN_ENV}={token}\n", encoding="utf-8")
    environment_file.chmod(0o600)
    os.environ[MCP_TOKEN_ENV] = token
    run(
        ["systemctl", "--user", "import-environment", MCP_TOKEN_ENV],
        check=False,
        env=dict(os.environ),
    )


def install_remote_support(host: FleetHost, token: str) -> None:
    """Install credentials and helpers without placing secrets in command arguments."""
    payload = tar_payload(support_files(token))
    if host.platform == "windows":
        command = "$null=New-Item -ItemType Directory -Force -Path $HOME; tar.exe -xf - -C $HOME"
    else:
        command = 'mkdir -p "$HOME" && tar -xf - -C "$HOME"'
    run_bytes(["ssh", host.name, command], input_data=payload)
    if host.platform == "windows":
        script = (
            "$token=[Console]::In.ReadToEnd().Trim();"
            f"[Environment]::SetEnvironmentVariable('{MCP_TOKEN_ENV}',$token,'User')"
        )
        run(["ssh", host.name, script], input_text=token)
    else:
        script = (
            f'IFS= read -r token; launchctl setenv {MCP_TOKEN_ENV} "$token" 2>/dev/null || true'
        )
        run(["ssh", host.name, script], input_text=f"{token}\n")


def skill_files(
    skill_dir: Path, prefixes: tuple[str, ...] = UNIX_SKILL_DIRS
) -> dict[str, tuple[bytes, int]]:
    """Build a multi-harness skill archive."""
    files: dict[str, tuple[bytes, int]] = {}
    for prefix in prefixes:
        for source in skill_dir.rglob("*"):
            if source.is_file():
                relative = source.relative_to(skill_dir).as_posix()
                files[f"{prefix}/{relative}"] = (source.read_bytes(), source.stat().st_mode & 0o777)
    return files


def install_local_skill(skill_dir: Path) -> None:
    """Replace local installed skill copies and verify their hashes."""
    source_hash = sha256_tree(skill_dir)
    for relative in UNIX_SKILL_DIRS:
        target = Path.home() / relative
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(skill_dir, target)
        if sha256_tree(target) != source_hash:
            raise RuntimeError(f"skill hash mismatch at {target}")


def install_remote_skill(host: FleetHost, skill_dir: Path) -> None:
    """Replace all remote harness skill copies from one archive."""
    files = skill_files(skill_dir)
    payload = tar_payload(files)
    if host.platform == "windows":
        targets = ",".join(
            f'"{PureWindowsPath(host.home, *Path(relative).parts)}"' for relative in UNIX_SKILL_DIRS
        )
        command = (
            f"$targets=@({targets});"
            "foreach($target in $targets){"
            "if(Test-Path -LiteralPath $target){Remove-Item -LiteralPath $target -Recurse -Force}};"
            "$null=New-Item -ItemType Directory -Force -Path $HOME;tar.exe -xf - -C $HOME"
        )
    else:
        targets = " ".join(shlex.quote(f"{host.home}/{relative}") for relative in UNIX_SKILL_DIRS)
        command = f'rm -rf -- {targets} && tar -xf - -C "$HOME"'
    run_bytes(["ssh", host.name, command], input_data=payload)


def merge_kimi_config(path: Path) -> None:
    """Install the local stdio proxy in Kimi's MCP configuration."""
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.setdefault("mcpServers", {})[MCP_NAME] = {
        "args": [],
        "command": str(Path.home() / ".local/bin/apple-mail-mcp-proxy"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
