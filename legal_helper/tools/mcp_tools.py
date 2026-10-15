"""Bridge external MCP tools into the function-tool list.

Each visible MCP server's ``tools/list`` is fetched once per process and
wrapped as ``BetaFunctionTool`` instances with namespaced names of the form
``<server>__<tool>``. Both the Anthropic and OpenAI providers register them
the same way as any in-tree connector, satisfying the provider-parity
contract.

Only HTTP MCPs are wired this way: the JSON-RPC body is small enough to send
synchronously per call. The stdio servers in ``.mcp.json`` serve external
clients; the harness reaches their sources through in-tree connectors.
"""

from __future__ import annotations

import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

from anthropic.lib.tools._beta_functions import BetaFunctionTool

from ..connectors.base import TTLCache, cache_key
from ..mcp import filter_mcps, load_registry
from ..mcp.adapter import mcp_tool_to_function_schema, qualify_mcp_tool_name
from ..mcp.known_tools import known_tools_for
from ..mcp.registry import McpServerSpec
from ..mcp.sync_client import http_call_tool, http_list_tools


# One lock per server, not one for all of them. A single lock serialised every
# `tools/list` probe: measured 2026-10-07, a cold discovery of the 11 HTTP
# servers took 9.4 s, the sum of the per-server probes (slowest 4.0 s), and
# every sub-agent that started meanwhile queued behind it. The per-server lock
# still stops two agents starting together from probing the same server twice:
# the second waits, then reads what the first cached.
_SPEC_LOCKS: dict[tuple[str, str, tuple[tuple[str, str], ...]], threading.Lock] = {}
_SPEC_LOCKS_GUARD = threading.Lock()
# Probes are network waits, so this bounds open sockets, not CPU.
_MAX_PARALLEL_PROBES = 16


def visible_mcp_specs(
    jurisdictions: Iterable[str],
    active_packs: Iterable[str],
) -> list[McpServerSpec]:
    return filter_mcps(load_registry(), jurisdictions, active_packs)


# A successful live probe is stable for a while; a fallback (no token /
# network blip / server down) must NOT be cached permanently — a short TTL
# lets the live probe recover without a process restart.
_TOOLS_LIST_CACHE = TTLCache(maxsize=64)
_TOOLS_LIST_LIVE_TTL = 60 * 60  # authoritative live schemas
_TOOLS_LIST_FALLBACK_TTL = 60  # manifest fallback — retry the live probe soon


def _cached_tools_list(spec_key: tuple[str, str, tuple[tuple[str, str], ...]]) -> tuple[dict[str, Any], ...]:
    """Lookup cache keyed by a hashable view of the spec (name, url, headers).

    Tries the live ``tools/list`` first so we get authoritative schemas. If
    the probe returns nothing (no token, network blip, server down), falls
    back to the static manifest in ``mcp/known_tools.py`` so primary
    sources (e.g. the 9 PKULaw sub-services) still surface to the model
    rather than silently vanishing. Live results are cached for an hour,
    fallbacks for a minute (so a recovered server is picked up quickly).
    """
    key = cache_key("mcp_tools_list", spec_key)
    cached = _TOOLS_LIST_CACHE.get(key)
    if cached is not TTLCache._MISS:
        return cached
    server_name, url, headers_tuple = spec_key
    spec = McpServerSpec(
        name=server_name,
        transport="http",
        url=url,
        headers=dict(headers_tuple),
    )
    live = http_list_tools(spec)
    if live:
        # Merge: live wins on tool name, manifest fills in any tool the live
        # response omits (e.g. legacy endpoints not yet exposed in tools/list).
        seen = {t.get("name") for t in live if isinstance(t, dict) and t.get("name")}
        merged = list(live)
        for entry in known_tools_for(server_name):
            if entry.get("name") not in seen:
                merged.append(entry)
        result = tuple(merged)
        _TOOLS_LIST_CACHE.set(key, result, _TOOLS_LIST_LIVE_TTL)
        return result
    # No live tools — fall back to the static manifest if we have one.
    result = tuple(known_tools_for(server_name))
    _TOOLS_LIST_CACHE.set(key, result, _TOOLS_LIST_FALLBACK_TTL)
    return result


# Tests reset the cache through this attribute.
_cached_tools_list.cache_clear = _TOOLS_LIST_CACHE.clear  # type: ignore[attr-defined]


def _spec_key(spec: McpServerSpec) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    headers = tuple(sorted((spec.headers or {}).items()))
    return (spec.name, spec.url or "", headers)


def _tools_list_for(spec_key: tuple[str, str, tuple[tuple[str, str], ...]]) -> tuple[dict[str, Any], ...]:
    """One server's tool list, probing it at most once at a time.

    A failure stays with its server: the probe already soft-fails to the static
    manifest, and anything that escapes it yields an empty list here rather
    than costing the other servers their tools.
    """
    with _SPEC_LOCKS_GUARD:
        lock = _SPEC_LOCKS.setdefault(spec_key, threading.Lock())
    try:
        with lock:
            return _cached_tools_list(spec_key)
    except Exception:  # noqa: BLE001 — isolate one server's failure
        return ()


def _build_dispatcher(spec: McpServerSpec, tool_name: str):
    """Return a ``**kwargs`` closure suitable for ``BetaFunctionTool(func=...)``."""

    def _run(**kwargs: Any) -> str:
        return http_call_tool(spec, tool_name, kwargs)

    _run.__name__ = qualify_mcp_tool_name(spec.name, tool_name)
    return _run


def _wrap_as_function_tool(spec: McpServerSpec, tool: dict[str, Any]) -> BetaFunctionTool | None:
    schema = mcp_tool_to_function_schema(spec.name, tool)
    try:
        return BetaFunctionTool(
            _build_dispatcher(spec, tool.get("name", "tool")),
            name=schema["name"],
            description=schema["description"],
            input_schema=schema["input_schema"],
        )
    except Exception:  # noqa: BLE001 — surface failures via empty list rather than crashing
        return None


def visible_mcp_function_tools(
    jurisdictions: Iterable[str],
    active_packs: Iterable[str],
) -> list[BetaFunctionTool]:
    """Return ``BetaFunctionTool`` wrappers for every tool on every visible
    HTTP MCP server. Empty list if no MCP is visible / reachable.
    """
    # HTTP only: a stdio server would need a subprocess and an event loop
    # behind a synchronous ``BetaFunctionTool.call``.
    specs = [s for s in visible_mcp_specs(jurisdictions, active_packs) if s.transport == "http"]
    keys = [_spec_key(spec) for spec in specs]
    if len(keys) > 1:
        with ThreadPoolExecutor(
            max_workers=min(len(keys), _MAX_PARALLEL_PROBES), thread_name_prefix="mcp-list"
        ) as pool:
            futures = [pool.submit(contextvars.copy_context().run, _tools_list_for, key) for key in keys]
        lists = [f.result() for f in futures]
    else:
        lists = [_tools_list_for(key) for key in keys]
    # Assembled in registry order whatever order the probes finished in: the
    # tool list is part of every request's cached prefix, so it must not vary.
    out: list[BetaFunctionTool] = []
    for spec, tools in zip(specs, lists):
        for t in tools:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            wrapped = _wrap_as_function_tool(spec, t)
            if wrapped is not None:
                out.append(wrapped)
    return out


__all__ = ["visible_mcp_function_tools", "visible_mcp_specs"]
