"""In-tree CAAC / CCAR MCP server.

Re-exports ``connectors/aviation.py`` functions over stdio MCP so external
clients (Claude Desktop, etc.) can use them. Inside the legal_helper agent
runtime the connector functions are called directly via ``connectors/``
without going through this MCP path.

Run via:
    python -m legal_helper.mcp.servers.ccar_aviation
"""

from __future__ import annotations

import asyncio
import json

from ....connectors import aviation

# Mirrors the disabled entries in connectors/__init__.py: the caac.gov.cn
# scraper behind ccar_search / ccar_fetch 404s (search moved to
# api.so-gov.cn behind a JS-rendered form). Returning a structured notice
# beats invoking a knowingly-broken endpoint and handing the model a
# confusing scrape error.
_CCAR_DISABLED_NOTICE = {
    "error": "ccar endpoint disabled",
    "detail": (
        "The CAAC search endpoint moved to api.so-gov.cn behind a "
        "JS-rendered form; the direct scraper returns 404. Use the "
        "pkulaw_* MCP services for CCAR / CAAC rule text, or "
        "caac_local_search over the staged corpus."
    ),
}


def dispatch(name: str, arguments: dict) -> str:
    """Route one MCP ``tools/call`` to its backing connector. Kept free of
    ``mcp``-package imports so it is unit-testable."""
    if name in ("search_ccar", "fetch_ccar"):
        return json.dumps({**_CCAR_DISABLED_NOTICE, "tool": name}, ensure_ascii=False)
    if name == "faa_title14":
        return aviation.faa_title14_search.call(
            {"query": arguments.get("query", ""), "per_page": arguments.get("per_page", 8)}
        )
    return json.dumps({"error": f"unknown tool: {name}"})


async def _amain() -> None:
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import TextContent, Tool
    except ImportError as e:
        raise SystemExit(f"mcp package not installed: {e!s}")

    server = Server("ccar_aviation")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name="search_ccar",
                description=(
                    "Search CAAC / CCAR publications (parts, ADs, 通告). "
                    "Currently disabled — returns a structured notice "
                    "pointing at the pkulaw_* services."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "per_page": {"type": "integer", "default": 8},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="fetch_ccar",
                description=(
                    "Fetch the plain-text body of a CAAC publication URL. "
                    "Currently disabled — returns a structured notice."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            ),
            Tool(
                name="faa_title14",
                description="Search FAA 14 CFR (Title 14) via eCFR.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "per_page": {"type": "integer", "default": 8},
                    },
                    "required": ["query"],
                },
            ),
        ]

    @server.call_tool()
    async def _call(name: str, arguments: dict) -> list[TextContent]:
        return [TextContent(type="text", text=dispatch(name, arguments))]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
