"""Synchronous JSON-RPC client over MCP streamable-HTTP.

The async :class:`McpManager` keeps a long-lived pool that is the right fit
for the Claude Desktop / OpenAI MCP client use case. Inside our agent runtime
the model calls one tool at a time and the surrounding plumbing is sync, so a
small blocking client is simpler and avoids needing an event loop inside
``BetaFunctionTool.call``.

Used by ``tools/mcp_tools.py`` to fetch each MCP server's ``tools/list`` and
to dispatch ``tools/call`` invocations triggered by the model.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..connectors.base import request_with_retry
from .registry import McpServerSpec


_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_LIST_TIMEOUT = httpx.Timeout(15.0, connect=8.0)


def _parse_jsonrpc(resp: httpx.Response) -> dict[str, Any]:
    """Return the JSON-RPC envelope from a streamable-HTTP MCP response.

    Servers may answer with plain ``application/json`` or with SSE-framed
    ``text/event-stream`` (one or more ``data: {...}`` lines). Accept both.
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


def http_list_tools(spec: McpServerSpec) -> list[dict[str, Any]]:
    """Fetch the MCP server's ``tools/list``. Empty list on any failure.

    ``request_with_retry`` absorbs WSO2 gateway blips / 429s so a single
    transient failure at startup doesn't freeze the fallback manifest in
    for the whole process.
    """
    if not spec.url:
        return []
    try:
        resp = request_with_retry(
            "POST",
            spec.url,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **spec.headers,
            },
            json_body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            timeout=_LIST_TIMEOUT,
        )
        payload = _parse_jsonrpc(resp)
    except Exception:  # noqa: BLE001 — soft-fail per server
        return []
    return (payload.get("result") or {}).get("tools") or []


def http_call_tool(
    spec: McpServerSpec,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """Invoke ``tools/call`` on the MCP server. Returns a string suitable
    to hand back to the model.

    JSON-RPC errors are wrapped into a ``{"error": ...}`` JSON string so the
    model can still reason about them rather than the tool loop crashing.
    """
    if not spec.url:
        return json.dumps({"error": f"no url for http MCP: {spec.name}"})
    try:
        resp = request_with_retry(
            "POST",
            spec.url,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **spec.headers,
            },
            json_body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
            timeout=_DEFAULT_TIMEOUT,
        )
        payload = _parse_jsonrpc(resp)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"mcp call failed: {e!s}", "server": spec.name})
    if "result" in payload:
        result = payload["result"]
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list):
            texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("text")]
            if texts:
                return "\n".join(texts)
        return json.dumps(result, ensure_ascii=False)
    return json.dumps(payload, ensure_ascii=False)


__all__ = ["http_call_tool", "http_list_tools"]
