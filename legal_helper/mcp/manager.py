"""MCP client lifecycle: spawn / connect / list tools / call tool / shutdown.

The manager keeps stdio MCP servers running for the lifetime of the agent
process and reuses the connections. HTTP MCPs are stateless per call.

Both transport paths surface the same ``list_tools()`` / ``call_tool()``
interface so ``tools/mcp_tools.py`` can register them as plain function
tools for either provider.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import httpx

from .registry import McpServerSpec


def _parse_jsonrpc_response(resp: httpx.Response) -> dict[str, Any]:
    """Return the JSON-RPC envelope from a streamable-HTTP MCP response.

    Servers may answer with plain ``application/json`` or with SSE-framed
    ``text/event-stream`` (one or more ``data: {...}`` lines). We accept both.
    """
    ctype = (resp.headers.get("content-type") or "").lower()
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if not chunk:
                    continue
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                    return obj
        return {"error": {"message": "no JSON-RPC payload in SSE stream"}}
    return resp.json()


@dataclass
class McpClient:
    spec: McpServerSpec
    session: Any = None  # mcp.ClientSession (stdio) or None (http)
    tools: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tools is None:
            self.tools = []


class McpManager:
    """Owns a pool of MCP clients keyed by server name."""

    def __init__(self, specs: list[McpServerSpec]):
        self.specs = specs
        self._clients: dict[str, McpClient] = {}
        self._exit_stack: AsyncExitStack | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> "McpManager":
        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()
        for spec in self.specs:
            try:
                client = await self._connect(spec)
                self._clients[spec.name] = client
            except Exception as e:  # noqa: BLE001 — fail soft per server
                self._clients[spec.name] = McpClient(spec=spec, tools=[{
                    "error": f"failed to connect to {spec.name}: {e!s}",
                }])
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.__aexit__(*exc)

    async def _connect(self, spec: McpServerSpec) -> McpClient:
        if spec.transport == "stdio":
            return await self._connect_stdio(spec)
        return await self._connect_http(spec)

    async def _connect_stdio(self, spec: McpServerSpec) -> McpClient:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            return McpClient(spec=spec, tools=[{"error": "mcp package not installed"}])

        command = spec.launch_command()
        if command is None:
            return McpClient(
                spec=spec,
                tools=[
                    {
                        "error": (
                            f"stdio command {spec.command!r} not found on PATH or in the "
                            f"conda env — install it in the harness env (see CLAUDE.md) "
                            f"or give an absolute path in .mcp.json"
                        )
                    }
                ],
            )
        params = StdioServerParameters(
            command=command,
            args=list(spec.args),
            env={**os.environ, **spec.env},
        )
        assert self._exit_stack is not None
        read, write = await self._exit_stack.enter_async_context(stdio_client(params))
        session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools_resp = await session.list_tools()
        tools = [t.model_dump() for t in tools_resp.tools]
        return McpClient(spec=spec, session=session, tools=tools)

    async def _connect_http(self, spec: McpServerSpec) -> McpClient:
        # Lightweight HTTP MCP support — issue a tools/list against the
        # configured URL. We do not use the official SDK's streamable client
        # here because it is overkill for what we need; the JSON-RPC payload
        # is simple enough.
        if not spec.url:
            return McpClient(spec=spec, tools=[{"error": "no url for http MCP"}])
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0)) as client:
                resp = await client.post(
                    spec.url,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        **spec.headers,
                    },
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                )
                resp.raise_for_status()
                payload = _parse_jsonrpc_response(resp)
            tools = (payload.get("result") or {}).get("tools", [])
            return McpClient(spec=spec, tools=tools)
        except Exception as e:  # noqa: BLE001
            return McpClient(spec=spec, tools=[{"error": f"http connect failed: {e!s}"}])

    def all_clients(self) -> dict[str, McpClient]:
        return dict(self._clients)

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        client = self._clients.get(server_name)
        if client is None:
            return json.dumps({"error": f"unknown MCP server: {server_name}"})
        if client.spec.transport == "stdio" and client.session is not None:
            try:
                result = await client.session.call_tool(tool_name, arguments=arguments)
                texts = []
                for c in result.content or []:
                    if hasattr(c, "text"):
                        texts.append(c.text)
                    else:
                        texts.append(str(c))
                return "\n".join(texts) if texts else json.dumps(result.model_dump())
            except Exception as e:  # noqa: BLE001
                return json.dumps({"error": f"mcp call failed: {e!s}"})
        if client.spec.transport == "http":
            return await self._call_http(client.spec, tool_name, arguments)
        return json.dumps({"error": f"server {server_name} has no active connection"})

    async def _call_http(
        self, spec: McpServerSpec, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                resp = await client.post(
                    spec.url or "",
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        **spec.headers,
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": arguments},
                    },
                )
                resp.raise_for_status()
                payload = _parse_jsonrpc_response(resp)
            if "result" in payload:
                return json.dumps(payload["result"], ensure_ascii=False)
            return json.dumps(payload, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"http call failed: {e!s}"})
