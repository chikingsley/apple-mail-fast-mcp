"""Bounded progressive tool discovery without removing service capabilities."""

from fastmcp.experimental.transforms.code_mode import CodeMode, MontySandboxProvider


def communications_code_mode() -> CodeMode:
    """Expose discovery, schemas and sandboxed workflows with explicit limits."""
    return CodeMode(
        sandbox_provider=MontySandboxProvider(
            limits={"max_duration_secs": 60, "max_memory": 50_000_000},
        ),
        max_tool_calls=25,
    )
