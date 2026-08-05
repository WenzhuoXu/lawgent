"""FAA Dynamic Regulatory System (DRS) connector.

DRS at https://drs.faa.gov is the FAA's consolidated regulatory-guidance
knowledge base (ADs, ACs, Policy Statements, Orders, SAFOs, InFOs,
CHG_BULL, MMELs). The site itself is a Single-Page App with no
publicly documented JSON API; deep-link URLs (``/browse/excelExternalWindow/{id}``)
are stable, but the search/listing endpoints are gated behind the SPA's
client-side JS.

The pragmatic path: Federal Register API is the authoritative published
record for FAA rulemaking (including all ADs and most policy notices), so
we route ``drs_search`` through it filtered to the FAA, then surface
DRS-style deep-links + a DRS browse URL so the agent can hand off to a
human (or to ``drs_fetch``) for the ones that require the SPA.

``drs_fetch`` does a single WebFetch-equivalent on a DRS document URL and
returns the plain-text body — works for the static document detail pages
even though search doesn't.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import quote_plus

import httpx
from anthropic import beta_tool

from .base import USER_AGENT, http_get, request_with_retry, truncate

# Federal Register document types that DRS surfaces.
# RULE = published rules including ADs; PRORULE = NPRMs; NOTICE = policy
# notices and most ACs that go through FR.
_DRS_FR_DOC_TYPE_MAP = {
    "AD": ["RULE", "PRORULE"],
    "AC": ["NOTICE"],
    "POLICY": ["NOTICE"],
    "ORDER": ["NOTICE"],
    "SAFO": ["NOTICE"],
    "InFO": ["NOTICE"],
    "CHG_BULL": ["NOTICE"],
    "MMEL": ["NOTICE"],
    "TCDS": ["NOTICE"],
}


def _drs_browse_url(query: str, doc_types: Optional[list[str]]) -> str:
    """Build the DRS SPA browse URL so the agent can hand off to a human."""
    base = "https://drs.faa.gov/browse"
    if doc_types:
        # The SPA URL convention: /browse/<DOCTYPE>/recent — we use the
        # first doc_type if present. Multi-type filter is a SPA-only UI.
        return f"{base}/{doc_types[0]}/recent?searchQuery={quote_plus(query)}"
    return f"{base}?searchQuery={quote_plus(query)}"


@beta_tool
def drs_search(
    query: str,
    doc_types: Optional[list[str]] = None,
    per_page: int = 8,
) -> str:
    """Search the FAA Dynamic Regulatory System (DRS) — ADs, ACs, policy,
    orders, SAFOs, MMELs.

    Routed through the Federal Register API filtered to the FAA, which is
    the authoritative publication channel for all FAA rules and most
    notices. Each result carries a Federal Register ``html_url`` (primary
    cite) plus a ``drs_browse_url`` deep-link for human follow-up in the
    DRS UI when the document predates online-only publication.

    Args:
        query: Free-text query (regulation topic, AD number like
            ``2025-12-09``, model/series like ``B737-MAX``, AC number).
        doc_types: Optional list of DRS document-type filters. Accepted
            values: ``AD`` (Airworthiness Directives), ``AC`` (Advisory
            Circulars), ``POLICY``, ``ORDER``, ``SAFO``, ``InFO``,
            ``CHG_BULL``, ``MMEL``, ``TCDS``. Omit for all types.
        per_page: Number of results to return (1-20).
    """
    per_page = max(1, min(int(per_page), 20))

    fr_types: list[str] = []
    if doc_types:
        for t in doc_types:
            fr_types.extend(_DRS_FR_DOC_TYPE_MAP.get(t, []))
        # de-dup preserving order
        seen: set[str] = set()
        fr_types = [x for x in fr_types if not (x in seen or seen.add(x))]

    params: dict[str, Any] = {
        "conditions[term]": query,
        "conditions[agencies][]": "federal-aviation-administration",
        "per_page": per_page,
        "order": "relevance",
        "fields[]": [
            "title",
            "abstract",
            "document_number",
            "publication_date",
            "type",
            "agencies",
            "html_url",
            "pdf_url",
            "citation",
            "regulation_id_numbers",
            "topics",
        ],
    }
    if fr_types:
        params["conditions[type][]"] = fr_types

    try:
        data = http_get("https://www.federalregister.gov/api/v1/documents.json", params)
    except httpx.HTTPError as e:
        return json.dumps(
            {"error": f"DRS (Federal Register backing) request failed: {e!s}", "query": query}
        )

    results: list[dict[str, Any]] = []
    for doc in (data.get("results") or [])[:per_page]:
        results.append(
            {
                "title": doc.get("title"),
                "doc_type_fr": doc.get("type"),
                "publication_date": doc.get("publication_date"),
                "document_number": doc.get("document_number"),
                "citation": doc.get("citation"),
                "regulation_id_numbers": doc.get("regulation_id_numbers") or [],
                "topics": doc.get("topics") or [],
                "abstract": truncate(doc.get("abstract"), 600),
                "html_url": doc.get("html_url"),
                "pdf_url": doc.get("pdf_url"),
            }
        )
    return json.dumps(
        {
            "source": "FAA DRS (via Federal Register)",
            "query": query,
            "doc_types_filter": doc_types or "all",
            "count": len(results),
            "results": results,
            "drs_browse_url": _drs_browse_url(query, doc_types),
            "note": (
                "Federal Register is the authoritative publication channel for "
                "FAA rules and most policy notices. For ACs / Orders / SAFOs not "
                "in FR, follow drs_browse_url or call drs_fetch with a known DRS URL."
            ),
        },
        ensure_ascii=False,
    )


@beta_tool
def drs_fetch(url: str) -> str:
    """Fetch a DRS document page and return its plain-text body.

    DRS document detail URLs are stable (e.g.
    ``https://drs.faa.gov/browse/excelExternalWindow/<DOC_ID>``). The DRS
    search/listing pages are SPA-only and not fetchable this way.

    Args:
        url: A drs.faa.gov document URL.
    """
    if "drs.faa.gov" not in url:
        return json.dumps(
            {"error": "drs_fetch only accepts drs.faa.gov URLs", "url": url}
        )
    try:
        resp = request_with_retry("GET", url, headers={"User-Agent": USER_AGENT})
        html = resp.text
    except httpx.HTTPError as e:
        return json.dumps({"error": f"DRS fetch failed: {e!s}", "url": url})

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return json.dumps(
            {"source": "FAA DRS", "url": url, "text": truncate(html, 8000)},
            ensure_ascii=False,
        )
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return json.dumps(
        {
            "source": "FAA DRS",
            "url": url,
            "text": truncate(text, 8000),
        },
        ensure_ascii=False,
    )
