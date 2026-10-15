"""One search tool over the connector layer's official sources.

``legal_source_search`` takes a ``source`` discriminator and calls the
connector in ``legal_helper/connectors/``; skills with allow-listed connectors
call those connectors directly.
"""

import json
from typing import Literal, Optional

from anthropic import beta_tool

from ..connectors.eu import eurlex_search
from ..connectors.prc import flk_npc_search
from ..connectors.us_courts import courtlistener_search
from ..connectors.us_federal import ecfr_search, federal_register_search, govinfo_search

__all__ = ["legal_source_search"]


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
    """Search one authoritative legal source: US (eCFR, Federal Register,
    GovInfo, CourtListener), EU (EUR-Lex) or PRC (国家法律法规数据库).

    Args:
        source: ecfr | federal_register | govinfo | courtlistener | eurlex | flk_npc.
        query: Query, citation, phrase or document number.
        title: eCFR title number; 0 = all.
        agency: Federal Register agency slug.
        collections: GovInfo collection codes (CFR, USCODE, FR).
        court: CourtListener court slug (scotus, ca9).
        level: flk_npc level: law | regulation | judicial_interpretation | rule.
        document_type: EUR-Lex type: REG | DIR | DEC | RUL.
        per_page: Results, 1-20.
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
