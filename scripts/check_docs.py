"""Check the maintained documentation against the live MCP surface."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from apple_mail_fast_mcp import server

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
REMOVED_TOOLS = {
    "forward_message",
    "get_message",
    "reply_to_message",
    "send_email",
    "send_email_with_attachments",
}


async def _tool_names() -> set[str]:
    return {tool.name for tool in await server.mcp.list_tools()}


def main() -> int:
    """Validate tool sections, stale tool calls, and relative links."""
    errors: list[str] = []
    live_tools = asyncio.run(_tool_names())
    tools_doc = ROOT / "docs/reference/TOOLS.md"
    tools_text = tools_doc.read_text()
    documented = set(re.findall(r"(?m)^###\s+([a-z][a-z0-9_]+)\s*$", tools_text))

    errors.extend(f"TOOLS.md has no section for {name}" for name in sorted(live_tools - documented))
    errors.extend(
        f"TOOLS.md documents removed tool {name}" for name in sorted(documented - live_tools)
    )

    maintained = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    maintained = [
        path
        for path in maintained
        if not {"plans", "research"}.intersection(path.relative_to(ROOT).parts)
    ]
    for path in maintained:
        text = path.read_text()
        for line_number, line in enumerate(text.splitlines(), 1):
            errors.extend(
                f"{path.relative_to(ROOT)}:{line_number}: stale tool call {name}"
                for name in REMOVED_TOOLS
                if re.search(rf"\b{re.escape(name)}\s*\(", line)
            )
            for target in LINK.findall(line):
                if SCHEME.match(target) or target.startswith(("mailto:", "#")):
                    continue
                path_part = target.split("#", 1)[0]
                if path_part and not (path.parent / path_part).resolve().exists():
                    errors.append(f"{path.relative_to(ROOT)}:{line_number}: broken link {target}")

    if errors:
        print("\n".join(errors))
        return 1

    print(f"documentation passed: {len(live_tools)} tools and {len(maintained)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
