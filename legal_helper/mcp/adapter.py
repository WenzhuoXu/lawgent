"""Convert MCP ``tools/list`` entries into plain function-tool schemas."""

from __future__ import annotations

from typing import Any


_NAME_SEPARATOR = "__"


def qualify_mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Build a function-tool name from ``<server>__<tool>``.

    The Anthropic + OpenAI tool-name surfaces only accept
    ``[a-zA-Z0-9_-]`` so we use a double-underscore separator instead of a
    dot. Spaces become underscores; the result is safe to send on the wire
    and round-trip back to (server, tool) via ``split_mcp_tool_name``.
    """
    return f"{server_name}{_NAME_SEPARATOR}{tool_name}".replace(" ", "_")


def split_mcp_tool_name(qualified: str) -> tuple[str, str] | None:
    """Inverse of ``qualify_mcp_tool_name``. ``None`` when not namespaced."""
    if _NAME_SEPARATOR not in qualified:
        return None
    server, _, tool = qualified.partition(_NAME_SEPARATOR)
    if not server or not tool:
        return None
    return server, tool


def mcp_tool_to_function_schema(server_name: str, tool: dict[str, Any]) -> dict[str, Any]:
    """Translate an MCP tool definition into a Pydantic-friendly function spec
    that both the Anthropic and OpenAI providers accept after wrapping with
    their respective tool helpers.

    Args:
        server_name: The MCP server name (used to namespace the tool name
            so two servers can both expose ``search``).
        tool: The MCP ``Tool`` object: ``{name, description, inputSchema}``.
    """
    name = tool.get("name", "tool")
    qualified = qualify_mcp_tool_name(server_name, name)
    desc = tool.get("description") or f"MCP tool {qualified}"
    input_schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object", "properties": {}}
    if input_schema.get("type") != "object":
        input_schema = {"type": "object", "properties": {}, "items_origin": input_schema}
    return {
        "name": qualified,
        "description": desc,
        "input_schema": input_schema,
    }
