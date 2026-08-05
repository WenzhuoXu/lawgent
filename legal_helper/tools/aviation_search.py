"""Compact dispatcher over aviation-specific connectors.

Mirrors ``tools/legal_search.py``: a single ``@beta_tool`` entry point with
a ``source`` discriminator so sub-agents that already use
``legal_source_search`` can also reach the aviation-specific surface
without learning every individual connector name.
"""

import json
from typing import Literal, Optional

from anthropic import beta_tool

from ..connectors.caac_local import caac_local_fetch, caac_local_search
from ..connectors.easa_safety import easa_ad_fetch, easa_ad_search, easa_ear_index
from ..connectors.ethiopia import ecaa_fetch, ecaa_index, ethiopia_law_fetch, ethiopia_law_search
from ..connectors.faa_drs import drs_search
from ..rag.retrieve import retrieve

__all__ = ["aviation_source_search"]


@beta_tool
def aviation_source_search(
    source: Literal[
        "caac_local",
        "caac_local_fetch",
        "caac_hybrid",
        "faa_drs",
        "easa_ad",
        "easa_ad_fetch",
        "easa_ear",
        "ecaa_index",
        "ecaa_fetch",
        "ethiopia_law",
        "ethiopia_law_fetch",
    ],
    query: str = "",
    identifier: str = "",
    category: Optional[str] = None,
    ccar_part: str = "",
    title: str = "",
    validity: str = "",
    article: str = "",
    max_chars: int = 12000,
    doc_types: Optional[list[str]] = None,
    ad_type: Optional[str] = None,
    ad_number: str = "",
    per_page: int = 5,
) -> str:
    """Search one authoritative aviation regulatory source via a discriminator.

    CAAC results carry ``is_stub`` (markdown body is only a PDF attachment),
    ``body_chars``, ``flags`` (any of ``pdf_stub``, ``explicit_expired``,
    ``superseded``, ``future_effective``), and ``superseded_by``.
    ``caac_local_fetch`` additionally returns ``alternates`` (other revisions
    of the same title family) and a ``note`` when the picked entry is a stub.

    Args:
        source: Source to query:
            - ``caac_local``: deterministic local CAAC / CCAR corpus search
            - ``caac_local_fetch``: fetch one local CAAC / CCAR document.
              When multiple revisions share a title (e.g. ``中华人民共和国民
              用航空法（2021年版）`` vs ``（2026年7月1日起施行）``) the
              non-stub, non-expired, latest entry wins; losers are returned
              under ``alternates`` with their status.
            - ``caac_hybrid``: local CAAC search plus caac_ccar RAG hits
            - ``faa_drs``: FAA Dynamic Regulatory System (ADs, ACs, policy)
            - ``easa_ad``: EASA Safety Publications Tool search
            - ``easa_ad_fetch``: fetch a specific EASA AD by number
            - ``easa_ear``: EASA Easy Access Rules curated index
            - ``ecaa_index``: Ethiopian civil-aviation legal-framework index
              (regulator, Civil Aviation Proclamation 616/2008 + 1179/2020,
              ECAA re-establishment 273/2002, ECAR, ICAO/Cape Town/BASA layer)
            - ``ecaa_fetch``: fetch readable text from an official ECAA page
              (``ecaa.gov.et``); pass the page path in ``identifier``
              (default ``sectoral/regulation``)
            - ``ethiopia_law``: search the curated Ethiopian federal-law
              catalogue (civil aviation + foundational/commercial); pass the
              query or a proclamation number
            - ``ethiopia_law_fetch``: fetch one Ethiopian proclamation body;
              pass ``616/2008`` / ``1179/2020`` / ``273/2002`` (or any
              proclamation number) in ``identifier``
        query: Free-text query for the search-style sources.
        identifier: File path, title, or CCAR part for ``caac_local_fetch``.
        category: Optional CAAC category filter: ``laws`` or ``ccar``.
        ccar_part: Optional CCAR part filter (e.g. ``CCAR-121-R7``).
        title: Optional CAAC title substring filter.
        validity: Optional CAAC validity filter (e.g. ``有效``).
        article: Optional CAAC article label/number to extract — pass
            ``"第三十七条"`` or ``"37"`` when you intend to cite a specific
            article. Without this, ``caac_local`` / ``caac_local_fetch``
            return metadata + a header snippet only; with it they return
            the verbatim 第X条 body under ``article_text``. Citing a CCAR
            article number without first fetching the article body via
            ``article=`` is a cite-check failure mode.
        max_chars: Maximum characters for ``caac_local_fetch``.
        doc_types: ``faa_drs`` doc-type filter (``AD``, ``AC``, ``POLICY``,
            ``ORDER``, ``SAFO``, ``InFO``, ``MMEL``, ``TCDS``).
        ad_type: ``easa_ad`` filter (``AD``, ``PAD``, ``EAD``, ``SIB``,
            ``SD``, ``PSD``).
        ad_number: Required for ``easa_ad_fetch`` — e.g. ``2025-0123``.
        per_page: Results to return for search-style sources (1-20).
    """
    per_page = max(1, min(int(per_page), 20))
    if source == "caac_local":
        return caac_local_search.call(
            {
                "query": query,
                "category": category,
                "ccar_part": ccar_part,
                "title": title,
                "validity": validity,
                "article": article,
                "per_page": per_page,
            }
        )
    if source == "caac_local_fetch":
        return caac_local_fetch.call(
            {
                "identifier": identifier or ccar_part or title or query,
                "article": article,
                "max_chars": max_chars,
            }
        )
    if source == "caac_hybrid":
        local_raw = caac_local_search.call(
            {
                "query": query,
                "category": category,
                "ccar_part": ccar_part,
                "title": title,
                "validity": validity,
                "article": article,
                "per_page": min(per_page, 8),
            }
        )
        filters = {}
        if category:
            filters["corpus_category"] = category
        if ccar_part:
            filters["ccar_part"] = ccar_part
        if validity:
            filters["validity"] = validity
        try:
            rag_hits = [
                {
                    "score": h.score,
                    "text": h.text,
                    "source_path": h.metadata.get("source_path"),
                    "source_url": h.metadata.get("source_url"),
                    "title": h.metadata.get("title"),
                    "ccar_part": h.metadata.get("ccar_part"),
                    "validity": h.metadata.get("validity"),
                    "metadata": h.metadata,
                }
                for h in retrieve(
                    query=query or title or ccar_part,
                    collection="caac_ccar",
                    k=min(per_page, 8),
                    filters=filters or None,
                )
            ]
            rag_payload: dict = {"collection": "caac_ccar", "count": len(rag_hits), "results": rag_hits}
        except Exception as exc:  # noqa: BLE001
            rag_payload = {"collection": "caac_ccar", "error": str(exc), "results": []}
        return json.dumps(
            {
                "source": "caac_hybrid",
                "query": query,
                "local": json.loads(local_raw),
                "rag": rag_payload,
            },
            ensure_ascii=False,
        )
    if source == "faa_drs":
        return drs_search.call(
            {"query": query, "doc_types": doc_types, "per_page": per_page}
        )
    if source == "easa_ad":
        return easa_ad_search.call(
            {"query": query, "ad_type": ad_type, "per_page": per_page}
        )
    if source == "easa_ad_fetch":
        return easa_ad_fetch.call({"ad_number": ad_number})
    if source == "easa_ear":
        return easa_ear_index.call({})
    if source == "ecaa_index":
        return ecaa_index.call({})
    if source == "ecaa_fetch":
        return ecaa_fetch.call(
            {"page": identifier or query or "sectoral/regulation", "max_chars": max_chars}
        )
    if source == "ethiopia_law":
        return ethiopia_law_search.call({"query": query or identifier, "per_page": per_page})
    if source == "ethiopia_law_fetch":
        return ethiopia_law_fetch.call(
            {"proclamation": identifier or query, "max_chars": max_chars}
        )
    return json.dumps(
        {"error": f"Unsupported aviation source: {source}", "query": query}
    )
