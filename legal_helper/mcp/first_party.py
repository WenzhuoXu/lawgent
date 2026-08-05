"""First-party MCP servers adopted as secondary lookup paths.

CourtListener (Free Law Project) and GovInfo (GPO) now ship official MCP
servers. Per the US-sources guidance the direct-API connectors in
``legal_helper/connectors/`` remain the PRIMARY US path — these MCPs are a
secondary surface (notably CourtListener's citation-verification tooling,
which back-stops eyecite in ``ground_answer``). PKULaw stays primary for
PRC lookups; nothing here changes that routing.

Endpoint facts (verified live, 2026-07):

- ``https://mcp.courtlistener.com/`` — streamable HTTP at the root path
  (``/mcp`` 404s). OAuth 2.1 protected resource; auth server
  ``https://www.courtlistener.com``, bearer header only. The plain
  ``COURTLISTENER_API_TOKEN`` REST token is **not** accepted — a bearer
  obtained via the OAuth flow must be supplied as
  ``COURTLISTENER_MCP_TOKEN``. Without it, tools still surface via the
  static manifest and calls return the server's structured auth error.
- ``https://api.govinfo.gov/mcp`` — GPO public preview. Plain JSON
  (non-SSE) streamable HTTP; authenticates with the same ``X-Api-Key``
  as the direct GovInfo connectors. ``tools/list`` returns
  ``searchGovInfo`` + ``describePackageOrGranule``.

Specs declared here are appended by ``registry.load_registry`` unless
``.mcp.json`` declares the same server name (repo config wins).
"""

from __future__ import annotations

import os

from ..connectors.base import ensure_keys_loaded
from .registry import McpServerSpec

COURTLISTENER_MCP_URL = "https://mcp.courtlistener.com/"
GOVINFO_MCP_URL = "https://api.govinfo.gov/mcp"


def first_party_specs() -> list[McpServerSpec]:
    """Build specs for the official first-party MCP servers.

    Auth headers are attached only when the corresponding secret is
    present, so an unset token never ships an empty ``Authorization`` /
    ``X-Api-Key`` header.
    """
    ensure_keys_loaded()
    specs: list[McpServerSpec] = []

    cl_headers: dict[str, str] = {}
    cl_bearer = os.getenv("COURTLISTENER_MCP_TOKEN")
    if cl_bearer:
        cl_headers["Authorization"] = f"Bearer {cl_bearer}"
    specs.append(
        McpServerSpec(
            name="courtlistener_mcp",
            transport="http",
            url=COURTLISTENER_MCP_URL,
            headers=cl_headers,
            jurisdictions=("US",),
        )
    )

    gi_headers: dict[str, str] = {}
    gi_key = os.getenv("GOVINFO_API_KEY")
    if gi_key:
        gi_headers["X-Api-Key"] = gi_key
    specs.append(
        McpServerSpec(
            name="govinfo_mcp",
            transport="http",
            url=GOVINFO_MCP_URL,
            headers=gi_headers,
            jurisdictions=("US",),
        )
    )
    return specs


__all__ = ["COURTLISTENER_MCP_URL", "GOVINFO_MCP_URL", "first_party_specs"]
