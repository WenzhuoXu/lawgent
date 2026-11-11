"""In-tree CAAC / CCAR MCP server.

Re-exports the aviation connector functions over stdio MCP so external
clients (Claude Code, Claude Desktop, etc.) can use them. Inside the
legal_helper agent runtime the connector functions are called directly via
``connectors/`` without going through this MCP path.

``search_ccar`` / ``fetch_ccar`` read the local CAAC / CCAR corpus staged
from caac.gov.cn (``caac_local_search`` / ``caac_local_fetch``): the live
caac.gov.cn search moved to api.so-gov.cn behind a JS-rendered form, so a
direct scraper 404s.

Run via:
    python -m legal_helper.mcp.servers.ccar_aviation
"""

from __future__ import annotations

import asyncio
import json
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from ....connectors.caac_local import caac_local_fetch, caac_local_search

_SEARCH_ARGS = ("query", "category", "ccar_part", "title", "validity", "article", "per_page")


def _identifier(arguments: dict) -> str:
    """``identifier``, or the page stem of a legacy caac.gov.cn ``url``.

    The corpus is keyed by source-page stem (``t20210315_206814``), not by
    the full URL.
    """
    ident = str(arguments.get("identifier") or "").strip()
    if ident:
        return ident
    url = str(arguments.get("url") or "").strip()
    if not url:
        return ""
    return PurePosixPath(urlsplit(url).path).stem or url


def dispatch(name: str, arguments: dict) -> str:
    """Route one MCP ``tools/call`` to its backing connector. Kept free of
    ``mcp``-package imports so it is unit-testable."""
    if name == "search_ccar":
        return caac_local_search.call({k: arguments[k] for k in _SEARCH_ARGS if k in arguments})
    if name == "fetch_ccar":
        ident = _identifier(arguments)
        if not ident:
            return json.dumps({"error": "fetch_ccar needs an identifier"}, ensure_ascii=False)
        args = {"identifier": ident}
        for k in ("article", "max_chars"):
            if k in arguments:
                args[k] = arguments[k]
        return caac_local_fetch.call(args)
    if name == "faa_title14":
        # 14 CFR through the research layer's eCFR source (current text only).
        from ....research.sources.us import EcfrSource

        hits = EcfrSource().search(
            arguments.get("query", ""),
            limit=int(arguments.get("per_page", 8) or 8),
            filters={"title": 14},
        )
        return json.dumps(
            {"source": "eCFR (14 CFR)", "count": len(hits), "results": [h.to_dict() for h in hits]},
            ensure_ascii=False,
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
                    "Search the local CAAC / CCAR corpus (laws, CCAR parts, "
                    "规范性文件) by keyword, title, document number or CCAR "
                    "part. Results carry validity, source URL, and stub / "
                    "superseded flags."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "category": {"type": "string", "enum": ["laws", "ccar"]},
                        "ccar_part": {"type": "string", "description": "e.g. CCAR-121"},
                        "title": {"type": "string"},
                        "validity": {"type": "string", "description": "e.g. 有效"},
                        "article": {"type": "string", "description": "e.g. 第十七条"},
                        "per_page": {"type": "integer", "default": 8},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="fetch_ccar",
                description=(
                    "Fetch one CAAC / CCAR document from the local corpus by "
                    "file path, source-page stem or caac.gov.cn URL, CCAR part, "
                    "or title; optionally one article."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "identifier": {"type": "string"},
                        "url": {"type": "string", "description": "caac.gov.cn page URL (alternative to identifier)"},
                        "article": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 12000},
                    },
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
