"""Progressive reads and single-operation mutations with native input round trips."""

from typing import Any, override

from fastmcp import Context  # ruff: ignore[typing-only-third-party-import] - FastMCP inspects runtime annotations.
from fastmcp.exceptions import ToolError
from fastmcp.experimental.transforms.code_mode import CodeMode, MontySandboxProvider
from fastmcp.tools import Tool, ToolResult
from fastmcp.tools.base import InputRequiredToolResult


class CommunicationsCodeMode(CodeMode):
    """Keep a confirmation round from replaying a multi-operation write script."""

    @override
    def _make_execute_tool(self) -> Tool:
        transform = self

        async def execute(
            ctx: Context,
            code: str | None = None,
            tool_name: str | None = None,
            arguments: dict[str, Any] | None = None,
        ) -> Any:
            """Use code for read-only workflows; use tool_name and arguments for one mutation.

            Single-operation execution preserves native confirmation requests without
            replaying earlier writes. Discover the operation schema with get_schema.
            """
            if bool(code) == bool(tool_name):
                raise ToolError("Provide either code or tool_name with arguments")
            catalog = await transform.get_tool_catalog(ctx)

            async def invoke(name: str, params: dict[str, Any]) -> ToolResult:
                tool = next((item for item in catalog if item.name == name), None)
                if tool is None:
                    raise ToolError(f"Unknown tool: {name}")
                if code and not (tool.annotations and tool.annotations.read_only_hint):
                    raise ToolError("Mutations require execute(tool_name=..., arguments=...)")
                return await ctx.fastmcp.call_tool(tool.name, params)

            if tool_name:
                result = await invoke(tool_name, arguments or {})
                if isinstance(result, InputRequiredToolResult):
                    return result.input_required
                return result

            count = 0

            async def call_tool(name: str, params: dict[str, Any]) -> Any:
                nonlocal count
                count += 1
                if transform.max_tool_calls is not None and count > transform.max_tool_calls:
                    raise ToolError("Tool call limit exceeded")
                result = await invoke(name, params)
                if isinstance(result, InputRequiredToolResult):
                    raise ToolError("Input requests require single-operation execution")
                if result.structured_content is not None:
                    return result.structured_content
                return "\n".join(getattr(item, "text", str(item)) for item in result.content)

            return await transform.sandbox_provider.run(
                code or "", external_functions={"call_tool": call_tool}
            )

        return Tool.from_function(fn=execute, name=self.execute_tool_name)


def communications_code_mode() -> CodeMode:
    """Expose discovery, schemas and bounded reads plus isolated mutations."""
    return CommunicationsCodeMode(
        sandbox_provider=MontySandboxProvider(
            limits={"max_duration_secs": 60, "max_memory": 50_000_000},
        ),
        max_tool_calls=25,
    )
