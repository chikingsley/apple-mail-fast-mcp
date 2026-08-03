"""Configure and verify Apple Mail MCP clients across the agent fleet."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
from pathlib import Path, PureWindowsPath

from .fleet_support import (
    MCP_NAME,
    MCP_TOKEN_ENV,
    MCP_URL,
    FleetHost,
    merge_kimi_config,
    remote_executable,
    run,
)


def _local_executable(name: str) -> str | None:
    return shutil.which(name)


def _run_local(executable: str, arguments: list[str], *, check: bool = True) -> str:
    result = run(
        [executable, *arguments],
        check=check,
        capture=True,
        env=os.environ.copy(),
    )
    return result.stdout


def configure_local_clients() -> list[str]:
    """Configure every installed local harness and return their names."""
    configured: list[str] = []
    codex = _local_executable("codex")
    if codex:
        _run_local(codex, ["mcp", "remove", MCP_NAME], check=False)
        _run_local(
            codex,
            [
                "mcp",
                "add",
                MCP_NAME,
                "--url",
                MCP_URL,
                "--bearer-token-env-var",
                MCP_TOKEN_ENV,
            ],
        )
        configured.append("codex")

    claude = _local_executable("claude")
    if claude:
        helper = str(Path.home() / ".local/bin/apple-mail-mcp-headers")
        config = json.dumps(
            {"headersHelper": helper, "type": "http", "url": MCP_URL},
            separators=(",", ":"),
        )
        _run_local(claude, ["mcp", "remove", "--scope", "user", MCP_NAME], check=False)
        _run_local(claude, ["mcp", "add-json", "--scope", "user", MCP_NAME, config])
        configured.append("claude")

    opencode = _local_executable("opencode")
    if opencode:
        _run_local(
            opencode,
            [
                "mcp",
                "add",
                MCP_NAME,
                "--url",
                MCP_URL,
                "--header",
                "Authorization={file:~/.config/apple-mail-fast-mcp/mcp-authorization}",
            ],
        )
        configured.append("opencode")

    kimi = _local_executable("kimi")
    kimi_config = Path.home() / ".kimi-code/mcp.json"
    if kimi or kimi_config.parent.exists():
        merge_kimi_config(kimi_config)
        configured.append("kimi")
    return configured


def _remote_unix(host: FleetHost, command: list[str], *, check: bool = True) -> str:
    shell = shlex.join(command)
    result = run(
        ["ssh", host.name, f"zsh -lic {shlex.quote(shell)}"],
        check=check,
        capture=True,
    )
    return result.stdout


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _remote_windows(host: FleetHost, executable: str, arguments: list[str]) -> str:
    invocation = " ".join(
        [_powershell_literal(executable), *(_powershell_literal(arg) for arg in arguments)]
    )
    result = run(
        ["ssh", host.name, f"& {invocation}"],
        capture=True,
    )
    return result.stdout


def _configure_remote_unix(host: FleetHost) -> list[str]:
    configured: list[str] = []
    codex = remote_executable(host, "codex")
    if codex:
        _remote_unix(host, [codex, "mcp", "remove", MCP_NAME], check=False)
        _remote_unix(
            host,
            [
                codex,
                "mcp",
                "add",
                MCP_NAME,
                "--url",
                MCP_URL,
                "--bearer-token-env-var",
                MCP_TOKEN_ENV,
            ],
        )
        configured.append("codex")

    claude = remote_executable(host, "claude")
    if claude:
        helper = f"{host.home}/.local/bin/apple-mail-mcp-headers"
        config = json.dumps(
            {"headersHelper": helper, "type": "http", "url": MCP_URL},
            separators=(",", ":"),
        )
        _remote_unix(
            host,
            [claude, "mcp", "remove", "--scope", "user", MCP_NAME],
            check=False,
        )
        _remote_unix(
            host,
            [claude, "mcp", "add-json", "--scope", "user", MCP_NAME, config],
        )
        configured.append("claude")

    opencode = remote_executable(host, "opencode")
    if opencode:
        _remote_unix(
            host,
            [
                opencode,
                "mcp",
                "add",
                MCP_NAME,
                "--url",
                MCP_URL,
                "--header",
                "Authorization={file:~/.config/apple-mail-fast-mcp/mcp-authorization}",
            ],
        )
        configured.append("opencode")

    if remote_executable(host, "kimi"):
        config_path = f"{host.home}/.kimi-code/mcp.json"
        proxy_path = f"{host.home}/.local/bin/apple-mail-mcp-proxy"
        merge_script = (
            "import json,pathlib,sys;"
            "path=pathlib.Path(sys.argv[1]);"
            "data=json.loads(path.read_text()) if path.exists() else {};"
            "data.setdefault('mcpServers',{})[sys.argv[2]]={'command':sys.argv[3],'args':[]};"
            "path.parent.mkdir(parents=True,exist_ok=True);"
            "path.write_text(json.dumps(data,indent=2)+'\\n');"
            "path.chmod(0o600)"
        )
        _remote_unix(
            host,
            ["python3", "-c", merge_script, config_path, MCP_NAME, proxy_path],
        )
        configured.append("kimi")
    return configured


def _configure_remote_windows(host: FleetHost) -> list[str]:
    configured: list[str] = []
    codex = remote_executable(host, "codex")
    if codex:
        with contextlib.suppress(Exception):
            _remote_windows(host, codex, ["mcp", "remove", MCP_NAME])
        _remote_windows(
            host,
            codex,
            [
                "mcp",
                "add",
                MCP_NAME,
                "--url",
                MCP_URL,
                "--bearer-token-env-var",
                MCP_TOKEN_ENV,
            ],
        )
        configured.append("codex")
    opencode = remote_executable(host, "opencode")
    if opencode:
        authorization = PureWindowsPath(
            host.home, ".config", "apple-mail-fast-mcp", "mcp-authorization"
        ).as_posix()
        _remote_windows(
            host,
            opencode,
            [
                "mcp",
                "add",
                MCP_NAME,
                "--url",
                MCP_URL,
                "--header",
                f"Authorization={{file:{authorization}}}",
            ],
        )
        configured.append("opencode")
    return configured


def configure_remote_clients(host: FleetHost) -> list[str]:
    """Configure every installed harness on one remote host."""
    if host.platform == "windows":
        return _configure_remote_windows(host)
    return _configure_remote_unix(host)


def verify_local_clients(expected: list[str]) -> None:
    """Verify local client registrations without exposing credentials."""
    commands = {
        "codex": ["mcp", "get", MCP_NAME, "--json"],
        "claude": ["mcp", "get", MCP_NAME],
        "opencode": ["mcp", "list"],
    }
    for name in expected:
        if name == "kimi":
            data = json.loads((Path.home() / ".kimi-code/mcp.json").read_text(encoding="utf-8"))
            if MCP_NAME not in data.get("mcpServers", {}):
                raise RuntimeError("Kimi Apple Mail configuration is missing")
            continue
        executable = _local_executable(name)
        if executable is None:
            raise RuntimeError(f"{name} disappeared during verification")
        output = _run_local(executable, commands[name])
        if MCP_NAME not in output:
            raise RuntimeError(f"{name} did not report {MCP_NAME}")


def verify_remote_clients(host: FleetHost, expected: list[str]) -> None:
    """Verify remote registrations through each available client."""
    for name in expected:
        if name == "kimi":
            output = _remote_unix(host, ["cat", f"{host.home}/.kimi-code/mcp.json"])
        else:
            executable = remote_executable(host, name)
            if executable is None:
                raise RuntimeError(f"{host.name} {name} disappeared during verification")
            arguments = {
                "codex": ["mcp", "get", MCP_NAME, "--json"],
                "claude": ["mcp", "get", MCP_NAME],
                "opencode": ["mcp", "list"],
            }[name]
            output = (
                _remote_windows(host, executable, arguments)
                if host.platform == "windows"
                else _remote_unix(host, [executable, *arguments])
            )
        if MCP_NAME not in output:
            raise RuntimeError(f"{host.name} {name} did not report {MCP_NAME}")
