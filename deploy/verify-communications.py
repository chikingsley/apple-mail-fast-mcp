# /// script
# requires-python = ">=3.14"
# dependencies = ["fastmcp==4.0.3"]
# ///
import asyncio
import json
import os
import pathlib
import shutil
import time
from fastmcp import Client


async def main():
    h = pathlib.Path.home()
    uv = shutil.which("uv") or str(h / ".local/bin" / ("uv.exe" if os.name == "nt" else "uv"))
    opencode_path = h / ".config/opencode/opencode.json"
    opencode = json.loads(opencode_path.read_text(encoding="utf-8-sig")) if opencode_path.exists() else None
    if opencode is not None:
        for skill in ["apple-mail", "apple-calendar", "apple-contacts", "messages"]:
            installed = h / ".config/opencode/skills" / skill / "SKILL.md"
            shared = h / ".agents/skills" / skill / "SKILL.md"
            if not installed.exists() or installed.read_bytes() != shared.read_bytes():
                raise RuntimeError(f"OpenCode skill missing or stale: {skill}")
    for name, operation in [("apple-mail", "list_accounts"), ("beeper", "get_accounts"), ("apple-calendar", "calendar_status"), ("apple-contacts", "list_containers")]:
        config = {
            "mcpServers": {
                name: {
                    "command": uv,
                    "args": [
                        "run",
                        "--locked",
                        "--script",
                        str(h / ".config/peacockery-communications/client.py"),
                        name,
                    ],
                }
            }
        }
        pin = h / ".config/peacockery-communications/python-path"
        if pin.exists():
            config["mcpServers"][name]["args"][1:1] = ["--python", pin.read_text().strip()]
        if opencode is not None:
            entry = opencode.get("mcp", {}).get(name, {})
            command = entry.get("command", [])
            if not entry.get("enabled") or entry.get("type") != "local" or not command:
                raise RuntimeError(f"OpenCode MCP missing or disabled: {name}")
            config["mcpServers"][name] = {"command": command[0], "args": command[1:]}
        t = time.monotonic()
        async with Client(config, timeout=120) as c:
            ts = await c.list_tools()
            discovery = await c.call_tool("search", {"query": operation, "limit": 3})
            schema = await c.call_tool("get_schema", {"tools": [operation]})
            r = await c.call_tool(
                "execute", {"code": f'return await call_tool("{operation}", {{}})'}
            )
            if {tool.name for tool in ts} != {"search", "get_schema", "execute"}:
                raise RuntimeError(f"{name}: unexpected tool catalog")
            if r.is_error or discovery.is_error or schema.is_error or not (r.data or r.content):
                raise RuntimeError(f"{name}: discovery or read failed")
            if name in {"apple-mail", "apple-calendar", "apple-contacts"} and not (r.data or {}).get("success"):
                raise RuntimeError(f"{name}: native lookup returned failure")
            print(
                json.dumps(
                    {
                        "service": name,
                        "client": "opencode" if opencode is not None else "shared-adapter",
                        "skills_ok": opencode is not None,
                        "tools": [x.name for x in ts],
                        "error": r.is_error,
                        "has_result": bool(r.data or r.content),
                        "discovery_ok": not discovery.is_error,
                        "schema_ok": not schema.is_error,
                        "seconds": round(time.monotonic() - t, 2),
                    }
                ),
                flush=True,
            )


asyncio.run(main())
