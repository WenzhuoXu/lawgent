"""In-process connectors of the domain packs.

Primary law of every jurisdiction is reached through the research layer
(``legal_helper/research``: ``legal_sources`` / ``legal_search`` /
``legal_fetch``). What remains here are the aviation pack's regulator
sources, which are not statute databases: the staged CAAC corpus, FAA DRS
(ADs / ACs), EASA safety publications and Easy Access Rules, and the
Ethiopian CAA. Each is a ``@beta_tool`` function; ``REGISTRY`` carries its
jurisdiction and pack so ``tools/connectors_tools.py`` can gate it.
``base.py`` is the shared HTTP plumbing (retries, TTL cache) the research
sources use too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .caac_local import caac_local_fetch, caac_local_search
from .easa_safety import easa_ad_fetch, easa_ad_search, easa_ear_index
from .ethiopia import ecaa_fetch, ecaa_index
from .faa_drs import drs_search


@dataclass(frozen=True)
class ConnectorEntry:
    name: str
    fn: Any
    jurisdiction: tuple[str, ...]
    domain_packs: tuple[str, ...] = ()


REGISTRY: tuple[ConnectorEntry, ...] = (
    ConnectorEntry("caac_local_search", caac_local_search, ("CN", "ICAO"), ("aviation",)),
    ConnectorEntry("caac_local_fetch", caac_local_fetch, ("CN", "ICAO"), ("aviation",)),
    ConnectorEntry("drs_search", drs_search, ("US",), ("aviation",)),
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
    "caac_local_fetch",
    "caac_local_search",
    "drs_search",
    "easa_ad_fetch",
    "easa_ad_search",
    "easa_ear_index",
    "ecaa_fetch",
    "ecaa_index",
]
