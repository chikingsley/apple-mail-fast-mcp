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
