"""In-process connector layer.

Each connector is a ``@beta_tool``-decorated function with a JSON-Schema
description that both the Anthropic and OpenAI providers can register as a
plain function tool. ``REGISTRY`` collects them with their jurisdiction +
domain-pack metadata so ``tools/connectors_tools.py`` can filter them per
invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .aviation import ccar_fetch, ccar_search, faa_title14_search
from .caac_local import caac_local_fetch, caac_local_search
from .easa_safety import easa_ad_fetch, easa_ad_search, easa_ear_index
from .ethiopia import ecaa_fetch, ecaa_index, ethiopia_law_fetch, ethiopia_law_search
from .eu import eurlex_fetch, eurlex_search
from .faa_drs import drs_fetch, drs_search
from .prc import flk_npc_fetch, flk_npc_search
from .us_courts import courtlistener_fetch, courtlistener_search
from .us_federal import (
    ecfr_fetch,
    ecfr_search,
    federal_register_fetch,
    federal_register_search,
    govinfo_fetch,
    govinfo_search,
)


@dataclass(frozen=True)
class ConnectorEntry:
    name: str
    fn: Any
    jurisdiction: tuple[str, ...]
    domain_packs: tuple[str, ...] = ()


REGISTRY: tuple[ConnectorEntry, ...] = (
    ConnectorEntry("ecfr_search", ecfr_search, ("US",)),
    ConnectorEntry("ecfr_fetch", ecfr_fetch, ("US",)),
    ConnectorEntry("federal_register_search", federal_register_search, ("US",)),
    ConnectorEntry("federal_register_fetch", federal_register_fetch, ("US",)),
    ConnectorEntry("govinfo_search", govinfo_search, ("US",)),
    ConnectorEntry("govinfo_fetch", govinfo_fetch, ("US",)),
    ConnectorEntry("courtlistener_search", courtlistener_search, ("US",)),
    ConnectorEntry("courtlistener_fetch", courtlistener_fetch, ("US",)),
    ConnectorEntry("eurlex_search", eurlex_search, ("EU",)),
    ConnectorEntry("eurlex_fetch", eurlex_fetch, ("EU",)),
    ConnectorEntry("flk_npc_search", flk_npc_search, ("CN",)),
    ConnectorEntry("flk_npc_fetch", flk_npc_fetch, ("CN",)),
    ConnectorEntry("ethiopia_law_search", ethiopia_law_search, ("ET",)),
    ConnectorEntry("ethiopia_law_fetch", ethiopia_law_fetch, ("ET",)),
    ConnectorEntry("faa_title14_search", faa_title14_search, ("US", "ICAO"), ("aviation",)),
    ConnectorEntry("caac_local_search", caac_local_search, ("CN", "ICAO"), ("aviation",)),
    ConnectorEntry("caac_local_fetch", caac_local_fetch, ("CN", "ICAO"), ("aviation",)),
    # ccar_search / ccar_fetch disabled — CAAC search endpoint moved to
    # api.so-gov.cn behind a JS-rendered form; current scraper returns 404.
    # Re-enable once a working scraper is wired (or replace with PKULaw).
    # ConnectorEntry("ccar_search", ccar_search, ("CN", "ICAO"), ("aviation",)),
    # ConnectorEntry("ccar_fetch", ccar_fetch, ("CN", "ICAO"), ("aviation",)),
    ConnectorEntry("drs_search", drs_search, ("US",), ("aviation",)),
    # drs_fetch disabled — drs.faa.gov is a pure SPA; both browse and
    # document-detail URLs return only the JS shell (~25 chars of text)
    # to a non-browser client. Agents should follow the html_url field
    # in drs_search results (federalregister.gov links are static HTML)
    # or use fetch_url_to_artifact on a known FAA PDF URL.
    # ConnectorEntry("drs_fetch", drs_fetch, ("US",), ("aviation",)),
    ConnectorEntry("easa_ad_search", easa_ad_search, ("EU",), ("aviation",)),
    ConnectorEntry("easa_ad_fetch", easa_ad_fetch, ("EU",), ("aviation",)),
    ConnectorEntry("easa_ear_index", easa_ear_index, ("EU",), ("aviation",)),
    ConnectorEntry("ecaa_index", ecaa_index, ("ET", "ICAO"), ("aviation",)),
    ConnectorEntry("ecaa_fetch", ecaa_fetch, ("ET", "ICAO"), ("aviation",)),
)


def filter_for(
    jurisdictions: Iterable[str],
    active_packs: Iterable[str],
) -> list[ConnectorEntry]:
    """Return the connectors visible under the given jurisdictions + packs."""
    juris = set(jurisdictions)
    packs = set(active_packs)
    visible: list[ConnectorEntry] = []
    for entry in REGISTRY:
        if entry.domain_packs and not (set(entry.domain_packs) & packs):
            continue
        if juris and not (set(entry.jurisdiction) & juris):
            continue
        visible.append(entry)
    return visible


__all__ = [
    "ConnectorEntry",
    "REGISTRY",
    "filter_for",
    "ccar_fetch",
    "ccar_search",
    "caac_local_fetch",
    "caac_local_search",
    "courtlistener_fetch",
    "courtlistener_search",
    "drs_fetch",
    "drs_search",
    "easa_ad_fetch",
    "easa_ad_search",
    "easa_ear_index",
    "ecaa_fetch",
    "ecaa_index",
    "ecfr_fetch",
    "ecfr_search",
    "ethiopia_law_fetch",
    "ethiopia_law_search",
    "eurlex_fetch",
    "eurlex_search",
    "faa_title14_search",
    "federal_register_fetch",
    "federal_register_search",
    "flk_npc_fetch",
    "flk_npc_search",
    "govinfo_fetch",
    "govinfo_search",
]
