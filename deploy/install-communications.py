# /// script
# requires-python = ">=3.14"
# dependencies = ["tomlkit==0.14.0", "json5==0.14.0"]
# ///
"""Install the same credential-file adapter into supported user-level harnesses."""

import argparse
import datetime
import json
import os
import pathlib
import shutil
import sys

import json5
import tomlkit

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--local-service", action="store_true")
args = parser.parse_args()
home = pathlib.Path.home()
source = pathlib.Path(__file__).resolve().parent
root = home / ".config/peacockery-communications"
root.mkdir(mode=0o700, parents=True, exist_ok=True)
backup = root / "backups" / datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%f")
backup.mkdir(mode=0o700, parents=True)
client = root / "client.py"
shutil.copy2(source / "communications-client.py", client)
shutil.copy2(source / "communications-client.py.lock", root / "client.py.lock")
client.chmod(0o600)
uv = shutil.which("uv") or str(home / ".local/bin" / ("uv.exe" if os.name == "nt" else "uv"))
assert pathlib.Path(uv).is_file(), "uv is required"
endpoints = {
    "apple-mail": "http://127.0.0.1:8765/mcp"
    if args.local_service
    else "https://hochi.tailbce39f.ts.net/apple-mail/mcp",
    "beeper": "http://127.0.0.1:8766/mcp"
    if args.local_service
    else "https://hochi.tailbce39f.ts.net/beeper/mcp",
}
(root / "endpoints.json").write_text(json.dumps(endpoints, indent=2) + "\n")
(root / "endpoints.json").chmod(0o600)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        name = path.relative_to(home).as_posix().replace("/", "__")
        shutil.copy2(path, backup / name)
    temp = path.with_name(path.name + ".communications-tmp")
    with os.fdopen(
        os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
        "w",
        encoding="utf-8",
        newline="\n",
    ) as f:
        f.write(data)
    temp.replace(path)


python = str(pathlib.Path(sys._base_executable).resolve())
save(root / "python-path", python + "\n")
client_args = ["run", "--python", python, "--locked", "--script", str(client)]
names = ["apple-mail", "beeper"]
configured = []
p = home / ".codex/config.toml"
if p.exists() or shutil.which("codex"):
    doc = tomlkit.parse(p.read_text(encoding="utf-8-sig")) if p.exists() else tomlkit.document()
    servers = doc.setdefault("mcp_servers", tomlkit.table())
    for name in names:
        servers[name] = {
            "command": uv,
            "args": [*client_args, name],
            "enabled": True,
            "startup_timeout_sec": 90,
            "tool_timeout_sec": 120,
        }
    save(p, tomlkit.dumps(doc))
    configured.append("codex")
p = home / ".claude.json"
if p.exists() or shutil.which("claude"):
    doc = json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    servers = doc.setdefault("mcpServers", {})
    for name in names:
        servers[name] = {
            "type": "stdio",
            "command": uv,
            "args": [*client_args, name],
        }
    save(p, json.dumps(doc, indent=2) + "\n")
    configured.append("claude")
for p in [home / ".config/opencode/opencode.json", home / ".config/opencode/opencode.jsonc"]:
    if not p.exists():
        continue
    doc = json5.loads(p.read_text(encoding="utf-8-sig"))
    for name in names:
        doc.setdefault("mcp", {})[name] = {
            "type": "local",
            "command": [uv, *client_args, name],
            "enabled": True,
            "timeout": 120000,
        }
    save(p, json.dumps(doc, indent=2) + "\n")
    configured.append("opencode")
p = home / ".kimi-code/mcp.json"
if p.exists() or shutil.which("kimi"):
    doc = json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    for name in names:
        doc.setdefault("mcpServers", {})[name] = {
            "command": uv,
            "args": [*client_args, name],
        }
    save(p, json.dumps(doc, indent=2) + "\n")
    configured.append("kimi")
p = home / "Library/Application Support/Claude/claude_desktop_config.json"
if p.exists():
    doc = json.loads(p.read_text(encoding="utf-8-sig"))
    for name in names:
        doc.setdefault("mcpServers", {})[name] = {
            "command": uv,
            "args": [*client_args, name],
        }
    save(p, json.dumps(doc, indent=2) + "\n")
    configured.append("claude-desktop")
for prefix in [".agents/skills", ".codex/skills", ".claude/skills", ".kimi-code/skills"]:
    for name in ["apple-mail", "messages"]:
        src = source.parent / "skills" / name / "SKILL.md"
        p = home / prefix / name / "SKILL.md"
        save(p, src.read_text(encoding="utf-8-sig"))
# A shell convenience command, also useful for manual MCP diagnostics.
binpath = home / ".local/bin"
binpath.mkdir(parents=True, exist_ok=True)
import shlex

launcher = binpath / "peacockery-mcp"
save(
    launcher,
    "#!/bin/sh\nexec "
    + shlex.quote(uv)
    + " run --python "
    + shlex.quote(python)
    + " --locked --script "
    + shlex.quote(str(client))
    + ' "$@"\n',
)
launcher.chmod(0o700)
print(
    json.dumps(
        {"configured": configured, "services": list(endpoints), "local_service": args.local_service}
    )
)
