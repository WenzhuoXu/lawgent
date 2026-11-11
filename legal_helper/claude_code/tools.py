"""The legal tools as an in-process MCP server for Claude Code."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from anthropic.lib.tools._beta_functions import BetaFunctionTool

from ..config import Settings
from ..providers.tool_exec import _guarded
from ..tool_budget import apply_result_budget
from ..tool_policy import is_concurrent_safe, is_mutating
from ..tools.multimodal import split_inline_images
from .runtime import SERVER_NAME

#: Tools on the author's surface that Claude Code's own tools or skills replace
#: here. The hosted searches are provider tools, not functions, and drop out by
#: type. `load_skill` / `read_skill_file` → the Skill tool and Read;
#: `run_workflow` → the Agent tool and its `legal-specialist`; `create_deck` and
#: `run_procedure` drive a skill in a sub-agent of their own, which here is the
#: `deck-builder` sub-agent or the skill run directly with Bash.
REPLACED_TOOLS = frozenset(
    {"load_skill", "read_skill_file", "run_workflow", "create_deck", "run_procedure"}
)


def legal_tools(settings: Settings) -> list[BetaFunctionTool]:
    """The author's tools that Claude Code has no equivalent for."""
    from ..workflow import author_tool_surface

    out: list[BetaFunctionTool] = []
    seen: set[str] = set()
    for tool in author_tool_surface(settings, legal=True, workflow=False):
        if not isinstance(tool, BetaFunctionTool):
            continue
        if tool.name in REPLACED_TOOLS or tool.name in seen:
            continue
        seen.add(tool.name)
        out.append(tool)
    return out


def _run(tool: BetaFunctionTool, args: dict[str, Any]) -> dict[str, Any]:
    """Call one tool and shape its result as MCP content.

    A render returns its pages as images, which go back as image blocks so the
    model sees them; text is cut to the tool's result budget (spilled to
    ``state/tool_results`` with a path to read on, like on the other
    providers). Renders share PDFium, so `_guarded` keeps them from overlapping.
    """
    raw = _guarded(tool.name, lambda: tool.call(args))
    text, images = split_inline_images(raw)
    if images:
        return {
            "content": [{"type": "text", "text": text}]
            + [
                {
                    "type": "image",
                    "data": img.get("data", ""),
                    "mimeType": img.get("media_type", "image/png"),
                }
                for img in images
            ]
        }
    return {"content": [{"type": "text", "text": apply_result_budget(tool.name, text).text}]}


def _handler(tool: BetaFunctionTool):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        # Tools block (HTTP, LibreOffice, the embedder); a worker thread keeps
        # the SDK's event loop free, and carries this turn's context with it.
        try:
            return await asyncio.to_thread(_run, tool, dict(args or {}))
        except Exception as exc:  # noqa: BLE001 — the model reads the error and recovers
            message = json.dumps({"error": f"{type(exc).__name__}: {exc}"[:2000]}, ensure_ascii=False)
            return {"content": [{"type": "text", "text": message}], "is_error": True}

    return handler


def legal_tool_server(settings: Settings) -> Any:
    """An SDK MCP server named ``legal`` over :func:`legal_tools`."""
    from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server
    from mcp.types import ToolAnnotations

    tools = []
    for tool in legal_tools(settings):
        # Read-only tools may run side by side; Claude Code serialises the rest.
        read_only = is_concurrent_safe(tool.name) and not is_mutating(tool.name)
        tools.append(
            SdkMcpTool(
                name=tool.name,
                description=tool.description or tool.name,
                input_schema=tool.input_schema,
                handler=_handler(tool),
                annotations=ToolAnnotations(readOnlyHint=read_only),
            )
        )
    return create_sdk_mcp_server(SERVER_NAME, "1.0.0", tools=tools)
