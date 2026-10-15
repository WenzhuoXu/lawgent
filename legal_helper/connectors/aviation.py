"""Aviation-specific connectors: FAA regulations (14 CFR) via eCFR Title 14.

This module is the Python implementation behind both the in-process function
tool and the ``mcp/servers/ccar_aviation/`` MCP shell. CAAC rule text comes
from the staged corpus (``caac_local``) and PKULaw: caac.gov.cn's search moved
behind a JS-rendered form, so there is no CAAC scraper here.
"""

from anthropic import beta_tool

from .us_federal import ecfr_search


@beta_tool
def faa_title14_search(query: str, per_page: int = 8) -> str:
    """Search FAA regulations (14 CFR) via eCFR Title 14."""
    return ecfr_search.call({"query": query, "title": 14, "per_page": per_page})
