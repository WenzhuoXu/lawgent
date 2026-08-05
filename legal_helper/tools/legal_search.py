"""Compact dispatcher over connector-layer search functions.

Historical entry point. Internals now live in ``legal_helper/connectors/``.
Skills that prefer a single tool surface call ``legal_source_search`` with a
``source`` discriminator; skills with allow-listed connectors call the
underlying functions directly.
"""

import json
from typing import Literal, Optional

from anthropic import beta_tool

from ..connectors.eu import eurlex_search
from ..connectors.prc import flk_npc_search
from ..connectors.us_courts import courtlistener_search
from ..connectors.us_federal import ecfr_search, federal_register_search, govinfo_search

# Re-exports so callers that import from the old path keep working.
__all__ = [
    "courtlistener_search",
    "ecfr_search",
    "eurlex_search",
    "federal_register_search",
    "flk_npc_search",
    "govinfo_search",
    "legal_source_search",
]


@beta_tool
def legal_source_search(
    source: Literal["ecfr", "federal_register", "govinfo", "courtlistener", "eurlex", "flk_npc"],
    query: str,
    title: int = 0,
    agency: str = "",
    collections: Optional[list[str]] = None,
    court: Optional[str] = None,
    level: Optional[str] = None,
    document_type: Optional[str] = None,
    per_page: int = 5,
) -> str:
    """Search one authoritative legal source via a discriminator.

    Args:
        source: Source to query (``ecfr`` | ``federal_register`` | ``govinfo``
            | ``courtlistener`` | ``eurlex`` | ``flk_npc``).
        query: Natural-language query, citation, phrase, or document number.
        title: CFR title for eCFR searches (14 = FAA, 49 = transportation,
            0 = all). Ignored for non-eCFR sources.
        agency: Federal Register agency slug; empty for all. Ignored for
            non-FR sources.
        collections: Optional GovInfo collection codes (CFR, USCODE, FR, …).
        court: Optional CourtListener court slug (``scotus``, ``ca9``, …).
        level: Optional flk.npc.gov.cn level filter (``law``, ``regulation``,
            ``judicial_interpretation``, ``rule``).
        document_type: Optional EUR-Lex document type (``REG``, ``DIR``,
            ``DEC``, ``RUL``).
        per_page: Results to return (1-20).
    """
    per_page = max(1, min(int(per_page), 20))
    if source == "ecfr":
        return ecfr_search.call({"query": query, "title": title, "per_page": per_page})
    if source == "federal_register":
        return federal_register_search.call(
            {"query": query, "agency": agency, "per_page": per_page}
        )
    if source == "govinfo":
        return govinfo_search.call(
            {"query": query, "collections": collections, "page_size": per_page}
        )
    if source == "courtlistener":
        return courtlistener_search.call(
            {"query": query, "court": court, "per_page": per_page}
        )
    if source == "eurlex":
        return eurlex_search.call(
            {"query": query, "document_type": document_type, "per_page": per_page}
        )
    if source == "flk_npc":
        return flk_npc_search.call(
            {"query": query, "level": level, "per_page": per_page}
        )
    return json.dumps({"error": f"Unsupported legal source: {source}", "query": query})
