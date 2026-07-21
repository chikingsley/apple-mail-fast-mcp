"""Require every public connector method to be a tool or explicitly internal."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONNECTOR = ROOT / "src/apple_mail_fast_mcp/mail_connector.py"
SERVER = ROOT / "src/apple_mail_fast_mcp/server.py"

INTENTIONALLY_INTERNAL = {
    "auto_template_vars",
    "extract_draft_attachments",
    "find_message_by_message_id",
    "flag_message",
    "get_attachments",
    "get_draft_state",
    "get_message",
    "get_selected_messages",
    "mark_as_read",
    "move_messages",
    "set_rule_enabled",
}


def _decorator_name(node: ast.expr) -> str | None:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def main() -> int:
    """Validate connector and MCP tool parity."""
    connector_tree = ast.parse(CONNECTOR.read_text(), filename=str(CONNECTOR))
    server_tree = ast.parse(SERVER.read_text(), filename=str(SERVER))

    connector_class = next(
        node
        for node in connector_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AppleMailConnector"
    )
    connector_methods = {
        node.name
        for node in connector_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }
    tools = {
        node.name
        for node in server_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            _decorator_name(decorator) in {"_tool", "tool"} for decorator in node.decorator_list
        )
    }

    missing = connector_methods - tools
    unexpected = sorted(missing - INTENTIONALLY_INTERNAL)
    stale = sorted(INTENTIONALLY_INTERNAL - missing)
    if unexpected or stale:
        for name in unexpected:
            print(f"public connector method is neither a tool nor allowlisted: {name}")
        for name in stale:
            print(f"stale intentionally-internal method: {name}")
        return 1

    print(f"client/server parity passed: {len(tools)} tools, {len(missing)} internal methods")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
