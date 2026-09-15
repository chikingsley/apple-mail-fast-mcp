# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "tomlkit==0.14.0",
#     "json5==0.14.0",
#     "ruamel-yaml>=0.19.1",
# ]
# ///
"""Install the same credential-file adapter into supported user-level harnesses."""

import argparse
import datetime
import json
import io
import os
import pathlib
import shutil
import sys

import json5
import tomlkit
from ruamel.yaml import YAML

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
endpoints = json.loads((root / "endpoints.json").read_text()) if (root / "endpoints.json").exists() else {}
endpoints.update({
    "apple-contacts": "http://127.0.0.1:8768/mcp" if args.local_service else "https://hochi.tailbce39f.ts.net/apple-contacts/mcp",
    "apple-mail": "http://127.0.0.1:8765/mcp"
    if args.local_service
    else "https://hochi.tailbce39f.ts.net/apple-mail/mcp",
    "beeper": "http://127.0.0.1:8766/mcp"
    if args.local_service
    else "https://hochi.tailbce39f.ts.net/beeper/mcp",
    "apple-calendar": "http://127.0.0.1:8767/mcp" if args.local_service else "https://hochi.tailbce39f.ts.net/apple-calendar/mcp",
})
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
names = ["apple-mail", "apple-calendar", "apple-contacts", "beeper"]
configured = []
p = home / ".codex/config.toml"
if p.exists() or (home / ".codex").is_dir() or shutil.which("codex"):
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
if p.exists() or (home / ".claude").is_dir() or shutil.which("claude"):
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
if p.exists() or (home / ".kimi-code").is_dir() or shutil.which("kimi"):
    doc = json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    for name in names:
        doc.setdefault("mcpServers", {})[name] = {
            "command": uv,
            "args": [*client_args, name],
        }
    save(p, json.dumps(doc, indent=2) + "\n")
    configured.append("kimi")
p = (home / "AppData/Roaming/Claude/claude_desktop_config.json") if os.name == "nt" else (home / "Library/Application Support/Claude/claude_desktop_config.json")
if p.exists():
    doc = json.loads(p.read_text(encoding="utf-8-sig"))
    for name in names:
        doc.setdefault("mcpServers", {})[name] = {
            "command": uv,
            "args": [*client_args, name],
        }
    save(p, json.dumps(doc, indent=2) + "\n")
    configured.append("claude-desktop")
# Additional installed agent applications use the same adapter, never inline tokens.
for relative, agent in [(".gemini/settings.json", "gemini"), (".copilot/mcp-config.json", "copilot"), (".cursor/mcp.json", "cursor")]:
    p = home / relative
    if not p.parent.is_dir():
        continue
    doc = json5.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    for name in names:
        entry = {"command": uv, "args": [*client_args, name]}
        if agent == "copilot":
            entry.update({"type": "local", "tools": ["*"]})
        doc.setdefault("mcpServers", {})[name] = entry
    save(p, json.dumps(doc, indent=2) + "\n")
    configured.append(agent)

p = home / ".hermes/config.yaml"
if p.parent.is_dir():
    yaml = YAML()
    doc = yaml.load(p.read_text()) if p.exists() else {}
    if doc is None:
        doc = {}
    for name in names:
        doc.setdefault("mcp_servers", {})[name] = {"command": uv, "args": [*client_args, name]}
    output = io.StringIO()
    yaml.dump(doc, output)
    save(p, output.getvalue())
    configured.append("hermes")

# VS Code's user-level MCP file covers its agent mode and remote server installations.
for directory in [home / ".config/Code/User", home / "Library/Application Support/Code/User", home / "AppData/Roaming/Code/User", home / ".vscode-server/data/User"]:
    if not directory.is_dir():
        continue
    p = directory / "mcp.json"
    doc = json5.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    for name in names:
        doc.setdefault("servers", {})[name] = {"type": "stdio", "command": uv, "args": [*client_args, name]}
    save(p, json.dumps(doc, indent=2) + "\n")
    configured.append(str(directory.relative_to(home)))

for prefix in [".agents/skills", ".codex/skills", ".claude/skills", ".kimi-code/skills", ".gemini/skills", ".copilot/skills", ".hermes/skills"]:
    for name in ["apple-mail", "apple-calendar", "apple-contacts", "messages"]:
        src = source.parent / "skills" / name / "SKILL.md"
        if name == "apple-calendar" and not src.exists():
            src = source.parent.parent / "apple-calendar-global/skills/apple-calendar/SKILL.md"
        if name == "apple-contacts" and not src.exists():
            src = source.parent.parent / "apple-contacts-global/skills/apple-contacts/SKILL.md"
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
save(root / "installation.json", json.dumps({"configured": sorted(set(configured)), "services": names, "adapter_version": "4.0.3"}, indent=2) + "\n")
for name in ["calendar-client.py", "calendar-client.py.lock"]:
    obsolete = root / name
    if obsolete.exists():
        obsolete.unlink()
print(
    json.dumps(
        {"configured": configured, "services": list(endpoints), "local_service": args.local_service}
    )
)
