"""EASA Safety Publications + Easy Access Rules connectors.

EASA does not (yet) expose a REST API for ADs or Easy Access Rules. The
public surfaces we use are:

- ``ad.easa.europa.eu`` — the Safety Publications Tool. AD detail pages
  have stable URLs (``/ad/{AD_NUMBER}``). Search is a JS form, so we use
  the public search URL with a keyword param and parse the result list
  HTML; if EASA changes the markup we fall back to a search_url handoff.
- ``easa.europa.eu/.../easy-access-rules-xml-export`` — Easy Access Rules
  are published as static PDF + XML per regulation. There is no index API;
  ``easa_ear_index`` returns a curated, hand-maintained index of the five
  regulations most relevant to commercial air transport operators.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import httpx
from anthropic import beta_tool

from .base import USER_AGENT, request_with_retry, truncate

_EASA_AD_BASE = "https://ad.easa.europa.eu"
_EASA_AD_SEARCH = f"{_EASA_AD_BASE}/search/"
# The SP Tool advanced-search endpoint is slow when scanning across all
# AD classes — bump the default 15s timeout.
_EASA_SEARCH_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Curated index of EASA Easy Access Rules most relevant to a commercial
# airline operator. Update by hand when revisions ship; the EASA
# document-library page for each regulation always carries the latest
# PDF + XML on the same URL — there is no published machine index.
_EASA_EAR_INDEX: list[dict[str, str]] = [
    {
        "regulation": "965/2012",
        "title": "Air Operations",
        "parts": "CAT (Commercial Air Transport — Aeroplanes/Helicopters), ORO, SPA, NCC, NCO, SPO",
        "document_library_url": "https://www.easa.europa.eu/en/document-library/easy-access-rules/easy-access-rules-air-operations-regulation-eu-no-9652012",
    },
    {
        "regulation": "1321/2014",
        "title": "Continuing Airworthiness",
        "parts": "M (Continuing Airworthiness), CAMO, 145 (Maintenance Organisation Approvals), 66 (Certifying Staff), 147 (Training)",
        "document_library_url": "https://www.easa.europa.eu/en/document-library/easy-access-rules/easy-access-rules-continuing-airworthiness-regulation-eu-no",
    },
    {
        "regulation": "748/2012",
        "title": "Initial Airworthiness and Environmental Protection",
        "parts": "21 (Certification of Aircraft, Parts, Appliances; Design/Production Organisation Approvals)",
        "document_library_url": "https://www.easa.europa.eu/en/document-library/easy-access-rules/easy-access-rules-initial-airworthiness-and-environmental",
    },
    {
        "regulation": "1178/2011",
        "title": "Aircrew (Flight Crew Licensing & Medical)",
        "parts": "FCL (Flight Crew Licensing), MED (Medical), CC (Cabin Crew), ARA / ORA (Authorities / Organisations Requirements for Aircrew)",
        "document_library_url": "https://www.easa.europa.eu/en/document-library/easy-access-rules/easy-access-rules-aircrew-regulation-eu-no-11782011",
    },
    {
        "regulation": "923/2012",
        "title": "Standardised European Rules of the Air (SERA)",
        "parts": "SERA — implements ICAO Annex 2 + Annex 11 PANS-ATM",
        "document_library_url": "https://www.easa.europa.eu/en/document-library/easy-access-rules/easy-access-rules-standardised-european-rules-air-sera",
    },
]


_DEFAULT_AD_CLASSES = ("AD", "EAD", "PAD", "SIB", "PSD", "SD")


def _post_search(form: dict[str, Any]) -> str:
    """POST to the EASA SP Tool advanced-search endpoint and return HTML.

    httpx auto-sets Content-Type when ``data=`` is a dict, so we hand it a
    dict where ``fi_adclass[]`` is a list (urlencoded as repeated key).
    """
    resp = request_with_retry(
        "POST",
        _EASA_AD_SEARCH,
        data=form,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        timeout=_EASA_SEARCH_TIMEOUT,
    )
    return resp.text


@beta_tool
def easa_ad_search(
    query: str,
    ad_type: Optional[str] = None,
    per_page: int = 8,
) -> str:
    """Search the EASA Safety Publications Tool for ADs, SIBs, SDs.

    POSTs to ``ad.easa.europa.eu/search/`` (the SP Tool advanced-search
    endpoint) with ``fi_keyword`` and an optional ``fi_adclass[]`` filter,
    then parses the result table. AD detail URLs are
    ``https://ad.easa.europa.eu/ad/{AD_NUMBER}``.

    Args:
        query: Free-text query — aircraft model, manufacturer, AD number
            (e.g. ``2025-0123``), or descriptive phrase (``CFM56 fan
            blade``).
        ad_type: Optional filter — ``AD`` (final Airworthiness Directive),
            ``PAD`` (Proposed AD), ``EAD`` (Emergency AD), ``SIB`` (Safety
            Information Bulletin), ``SD`` (Safety Directive), ``PSD``
            (Proposed Safety Directive). Omit for all.
        per_page: Number of results to return (1-20).
    """
    per_page = max(1, min(int(per_page), 20))
    classes = [ad_type.upper()] if ad_type else list(_DEFAULT_AD_CLASSES)
    form: dict[str, Any] = {
        "fi_action": "advanced",
        "fi_tree": "",
        "fi_keyword": query,
        "fi_adclass[]": classes,
        "fi_date_start": "",
        "fi_date_end": "",
        "ps_src_tree": "",
        "fi_notification": "N",
        "is_default": "N",
    }

    try:
        html = _post_search(form)
    except httpx.HTTPError as e:
        return json.dumps(
            {
                "error": f"EASA SP Tool request failed: {e!s}",
                "query": query,
                "search_url": _EASA_AD_SEARCH,
            }
        )

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return json.dumps(
            {
                "error": "beautifulsoup4 required for EASA result parsing",
                "search_url": _EASA_AD_SEARCH,
            }
        )
    soup = BeautifulSoup(html, "lxml")

    # Result rows are <tr onmouseover="...showStatus('.../ad/<ID>')..."> with
    # 7 cells: [AD number, blank, issue date, title, manufacturer/aircraft,
    # effective date, file sizes]. AD-number anchor points to the detail URL.
    results: list[dict[str, Any]] = []
    for row in soup.find_all("tr", onmouseover=True):
        anchor = row.find("a", href=lambda h: h and "/ad/" in h)
        if not anchor:
            continue
        href = anchor.get("href") or ""
        ad_number = href.rsplit("/", 1)[-1]
        cells = [c.get_text(separator=" ", strip=True) for c in row.find_all("td")]
        # Cells layout: [AD#, "", issue_date, title, model, effective_date, sizes]
        title = cells[3] if len(cells) > 3 else anchor.get_text(strip=True)
        results.append(
            {
                "ad_number": ad_number,
                "title": truncate(title, 400),
                "issue_date": cells[2] if len(cells) > 2 else "",
                "effective_date": cells[5] if len(cells) > 5 else "",
                "applicability": cells[4] if len(cells) > 4 else "",
                "url": href if href.startswith("http") else f"{_EASA_AD_BASE}{href}",
            }
        )
        if len(results) >= per_page:
            break

    return json.dumps(
        {
            "source": "EASA Safety Publications Tool",
            "query": query,
            "ad_type_filter": ad_type or "all",
            "count": len(results),
            "results": results,
            "search_url": _EASA_AD_SEARCH,
            "note": (
                "AD/SIB cite format: 'EASA AD YYYY-NNNN' (final), "
                "'EASA EAD YYYY-NNNN-E' (emergency), 'EASA SIB YYYY-NN'. "
                "Pinpoint to a section by article inside the AD body."
            ),
        },
        ensure_ascii=False,
    )


@beta_tool
def easa_ad_fetch(ad_number: str) -> str:
    """Fetch the EASA AD / SIB / SD detail page text.

    Args:
        ad_number: Numeric portion only (e.g. ``2025-0123``, ``2026-0095-E``,
            ``2024-15`` for SIBs). Do NOT include the ``EASA AD`` prefix.
    """
    ad_number = ad_number.strip()
    if not re.match(r"^[A-Z0-9\-]+$", ad_number):
        return json.dumps({"error": "ad_number must look like 2025-0123 or 2026-0095-E"})

    url = f"{_EASA_AD_BASE}/ad/{ad_number}"
    try:
        resp = request_with_retry("GET", url, headers={"User-Agent": USER_AGENT})
        html = resp.text
    except httpx.HTTPError as e:
        return json.dumps({"error": f"EASA AD fetch failed: {e!s}", "ad_number": ad_number})

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return json.dumps(
            {"source": "EASA AD", "ad_number": ad_number, "url": url, "text": truncate(html, 8000)},
            ensure_ascii=False,
        )
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())

    pdf_url: Optional[str] = None
    for a in soup.select("a[href*='/blob/']"):
        href = a.get("href") or ""
        if href.lower().endswith(".pdf"):
            pdf_url = href if href.startswith("http") else f"{_EASA_AD_BASE}{href}"
            break

    return json.dumps(
        {
            "source": "EASA AD",
            "ad_number": ad_number,
            "url": url,
            "pdf_url": pdf_url,
            "text": truncate(text, 8000),
        },
        ensure_ascii=False,
    )


@beta_tool
def easa_ear_index() -> str:
    """Return a curated index of EASA Easy Access Rules most relevant to a
    commercial airline.

    Five core regulations are indexed: Air Operations (965/2012),
    Continuing Airworthiness (1321/2014), Initial Airworthiness
    (748/2012), Aircrew (1178/2011), SERA (923/2012). Each entry carries
    the EASA document-library URL where the latest PDF + XML revision
    can be downloaded.

    For pinpoint lookup within a regulation, download the XML once via
    ``python -m legal_helper.rag.ingest --collection easa_ear --source easa_ear_xml``
    then query ``retrieve_legal`` with collection ``easa_ear``.
    """
    return json.dumps(
        {
            "source": "EASA Easy Access Rules (curated index)",
            "count": len(_EASA_EAR_INDEX),
            "results": _EASA_EAR_INDEX,
            "note": (
                "Pinpoint cite format: '{regulation} Part-{PART} {item}', e.g. "
                "'Reg (EU) 965/2012 Part-CAT CAT.OP.MPA.105'. "
                "For full text retrieval use the RAG collection easa_ear."
            ),
        },
        ensure_ascii=False,
    )
