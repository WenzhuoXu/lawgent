"""Startup health probes for external MCP servers.

``pkulaw_health`` answers the one question that otherwise fails silently:
is ``PKULAW_API_TOKEN`` present and accepted by the WSO2 gateway? Without
it every ``pkulaw_*`` call degrades to the static manifest and the model
only discovers the problem mid-conversation. Callers (CLI startup, serve)
should log the returned dict once; the probe never raises.
"""

from __future__ import annotations

import os
from typing import Any

from ..connectors.base import ensure_keys_loaded
from .registry import load_registry
from .sync_client import http_list_tools

# Cheapest sub-service to probe — single tool, no search index behind it.
_PROBE_SERVER = "pkulaw_fatiao"


def pkulaw_health(probe_network: bool = True) -> dict[str, Any]:
    """Return ``{token_present, probed_server, reachable, tool_count, note}``.

    ``reachable`` is None when the network probe was skipped (no token, or
    ``probe_network=False``).
    """
    ensure_keys_loaded()
    token_present = bool(os.getenv("PKULAW_API_TOKEN"))
    out: dict[str, Any] = {
        "token_present": token_present,
        "probed_server": None,
        "reachable": None,
        "tool_count": 0,
    }
    if not token_present:
        out["note"] = (
            "PKULAW_API_TOKEN not set — pkulaw_* tools will surface from the "
            "static manifest but every call will fail at the WSO2 gateway. "
            "PKULaw is the primary PRC source; set the token in "
            "legal_helper/api_key or .env."
        )
        return out
    if not probe_network:
        return out
    spec = next(
        (s for s in load_registry() if s.name == _PROBE_SERVER and s.transport == "http"),
        None,
    )
    if spec is None:
        out["note"] = f"{_PROBE_SERVER} not found in the MCP registry"
        return out
    tools = http_list_tools(spec)
    out["probed_server"] = spec.name
    out["reachable"] = bool(tools)
    out["tool_count"] = len(tools)
    if not tools:
        out["note"] = (
            f"tools/list against {spec.name} returned nothing — token may be "
            "expired or the gateway is down; flk_npc_search remains the "
            "public fallback (search/status only, no body text)."
        )
    return out


__all__ = ["pkulaw_health"]
