"""US federal legal sources: eCFR, Federal Register, GovInfo."""

import json
import os
import re
import time
from typing import Any, Optional

import httpx
from anthropic import beta_tool

from ._body_fetch import fetch_body_text
from .base import (
    BODY_TTL,
    SEARCH_TTL,
    TTLCache,
    USER_AGENT,
    ensure_keys_loaded,
    http_get,
    http_post_json,
    request_with_retry,
    truncate,
)


@beta_tool
def ecfr_search(query: str, title: int = 0, per_page: int = 8) -> str:
    """Search the eCFR. Returns hit metadata + an ellipsed snippet only — no
    full section body. Call ``ecfr_fetch(title, part, section)`` on a hit
    to get the verbatim § body.

    Common titles: 14 (aviation / FAA), 49 (transportation / TSA / NTSB /
    PHMSA), 21 (food & drug), 26 (tax), 29 (labor), 15 (BIS / EAR export
    controls).

    Each hit carries ``title``, ``chapter``, ``part``, ``section``,
    ``hierarchy_headings``, ``snippet`` and ``url``.

    Args:
        query: Natural-language query, regulation phrase, or section number.
            eCFR full-text search is conjunctive over every token, so prefer
            short focused queries (e.g. ``"122.49a electronic manifest"``)
            over long verbose ones — a 10-word query with one off-topic word
            commonly returns zero hits.
        title: CFR title number to restrict to. Default 0 (all titles).
        per_page: Number of results to return (1-20).
    """
    per_page = max(1, min(int(per_page), 20))
    params: dict[str, Any] = {"query": query, "per_page": per_page}
    if title and int(title) > 0:
        params["hierarchy[title]"] = int(title)
    try:
        data = http_get("https://www.ecfr.gov/api/search/v1/results", params)
    except httpx.HTTPError as e:
        return json.dumps({"error": f"eCFR request failed: {e!s}", "query": query})

    results = []
    for hit in (data.get("results") or [])[:per_page]:
        hierarchy = hit.get("hierarchy") or {}
        headings = hit.get("hierarchy_headings") or {}
        url = (
            f"https://www.ecfr.gov/current/title-{hierarchy.get('title')}"
            + (f"/chapter-{hierarchy.get('chapter')}" if hierarchy.get("chapter") else "")
            + (f"/part-{hierarchy.get('part')}" if hierarchy.get("part") else "")
            + (f"/section-{hierarchy.get('section')}" if hierarchy.get("section") else "")
        )
        results.append(
            {
                "title": hierarchy.get("title"),
                "chapter": hierarchy.get("chapter"),
                "part": hierarchy.get("part"),
                "section": hierarchy.get("section"),
                "hierarchy_headings": headings,
                "snippet": truncate(hit.get("full_text_excerpt") or hit.get("headings_text"), 600),
                "effective_on": hit.get("starts_on"),
                "url": url,
            }
        )
    return json.dumps(
        {
            "source": "eCFR",
            "query": query,
            "title_filter": title or "all",
            "count": len(results),
            "results": results,
        },
        ensure_ascii=False,
    )


@beta_tool
def federal_register_search(
    query: str,
    agency: str = "",
    per_page: int = 8,
    document_types: Optional[list[str]] = None,
) -> str:
    """Search the Federal Register. Returns metadata + abstract only — no
    full rule body. Call ``federal_register_fetch(document_number)`` on a
    hit to get the verbatim rule text. (FAA Airworthiness Directives also
    surface through this search; ``drs_search`` is a thin filter wrapper.)

    Args:
        query: Free-text query (regulation topic, AD number, citation).
        agency: Federal Register agency slug. Empty string searches all.
            Common slugs: ``federal-aviation-administration``,
            ``transportation-department``, ``food-and-drug-administration``,
            ``securities-and-exchange-commission``,
            ``transportation-security-administration``,
            ``national-transportation-safety-board``,
            ``industry-and-security-bureau``.
        per_page: Results per page (1-20).
        document_types: Optional list filter; ``RULE``, ``PRORULE``,
            ``NOTICE``, ``PRESDOCU``.
    """
    per_page = max(1, min(int(per_page), 20))
    params: dict[str, Any] = {
        "conditions[term]": query,
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
        ],
    }
    if agency:
        params["conditions[agencies][]"] = agency
    if document_types:
        params["conditions[type][]"] = document_types

    try:
        data = http_get("https://www.federalregister.gov/api/v1/documents.json", params)
    except httpx.HTTPError as e:
        return json.dumps({"error": f"Federal Register request failed: {e!s}", "query": query})

    results = []
    for doc in (data.get("results") or [])[:per_page]:
        results.append(
            {
                "title": doc.get("title"),
                "type": doc.get("type"),
                "publication_date": doc.get("publication_date"),
                "document_number": doc.get("document_number"),
                "citation": doc.get("citation"),
                "agencies": [a.get("name") for a in (doc.get("agencies") or [])],
                "regulation_id_numbers": doc.get("regulation_id_numbers") or [],
                "abstract": truncate(doc.get("abstract"), 600),
                "html_url": doc.get("html_url"),
                "pdf_url": doc.get("pdf_url"),
            }
        )
    return json.dumps(
        {
            "source": "Federal Register",
            "query": query,
            "agency_filter": agency or "all",
            "total_pages": data.get("total_pages"),
            "count": len(results),
            "results": results,
        },
        ensure_ascii=False,
    )


@beta_tool
def govinfo_search(
    query: str,
    collections: Optional[list[str]] = None,
    page_size: int = 8,
) -> str:
    """Search GovInfo (US GPO). Returns granule metadata + txt_url / pdf_url
    only — no full body. Call ``govinfo_fetch(package_id, granule_id)`` on
    a hit to get the verbatim text.

    Complements eCFR and Federal Register by reaching historical CFR editions,
    the US Code, Public Laws, Statutes at Large, and Congressional
    bills / hearings / reports.

    Args:
        query: Natural-language query, statutory citation, or Public Law
            number.
        collections: Optional list of GovInfo collection codes to restrict
            to. Common codes: CFR (Code of Federal Regulations), USCODE
            (US Code), FR (Federal Register), PLAW (Public Laws), STATUTE
            (Statutes at Large), BILLS, CHRG (hearings), CRPT (reports),
            COMPS. Omit to search across all collections.
        page_size: Number of results to return (1-20).
    """
    page_size = max(1, min(int(page_size), 20))
    ensure_keys_loaded()
    api_key = os.getenv("GOVINFO_API_KEY")
    if not api_key:
        return json.dumps(
            {"error": "GOVINFO_API_KEY not set; cannot query GovInfo.", "query": query}
        )

    q = query.strip()
    if collections:
        codes = " ".join(c.strip().upper() for c in collections if c.strip())
        if codes:
            q = f"({q}) AND collection:({codes})"

    payload: dict[str, Any] = {
        "query": q,
        "pageSize": page_size,
        "offsetMark": "*",
        "sorts": [{"field": "relevancy", "sortOrder": "DESC"}],
        "historical": True,
        "resultLevel": "default",
    }
    headers = {"X-Api-Key": api_key}

    try:
        data = http_post_json(
            "https://api.govinfo.gov/search", payload, headers=headers, cache_ttl=SEARCH_TTL
        )
    except httpx.HTTPError as e:
        return json.dumps({"error": f"GovInfo request failed: {e!s}", "query": query})

    results = []
    for hit in (data.get("results") or [])[:page_size]:
        download = hit.get("download") or {}
        package_id = hit.get("packageId")
        details_url = f"https://www.govinfo.gov/app/details/{package_id}" if package_id else None
        results.append(
            {
                "title": truncate(hit.get("title"), 400),
                "collection": hit.get("collectionCode") or hit.get("collection"),
                "package_id": package_id,
                "granule_id": hit.get("granuleId"),
                "date_issued": hit.get("dateIssued"),
                "government_author": hit.get("governmentAuthor1"),
                "publisher": hit.get("publisher"),
                "snippet": truncate(hit.get("teaser") or hit.get("summary"), 600),
                "details_url": details_url,
                "pdf_url": download.get("pdfLink"),
                "txt_url": download.get("txtLink"),
            }
        )

    return json.dumps(
        {
            "source": "GovInfo (GPO)",
            "query": query,
            "collections_filter": collections or "all",
            "total_count": data.get("count"),
            "count": len(results),
            "results": results,
            "authenticated": True,
        },
        ensure_ascii=False,
    )


# TTL cache (not lru_cache): eCFR titles get re-issued, so a process-lifetime
# cache would serve superseded regulation text as current forever. Successful
# resolutions are cached for 24 h with the resolution timestamp; failures are
# never cached.
_ISSUE_DATE_CACHE = TTLCache(maxsize=64)


def _ecfr_latest_issue_date(title: int) -> tuple[Optional[str], Optional[str]]:
    """Return ``(latest_issue_date, resolved_at_iso)`` for a CFR title."""
    key = str(int(title))
    cached = _ISSUE_DATE_CACHE.get(key)
    if cached is not TTLCache._MISS:
        return cached
    try:
        r = request_with_retry(
            "GET",
            "https://www.ecfr.gov/api/versioner/v1/titles",
            headers={"User-Agent": USER_AGENT},
        )
        for t in r.json().get("titles") or []:
            if int(t.get("number") or 0) == int(title):
                issue_date = t.get("latest_issue_date") or t.get("up_to_date_as_of")
                if issue_date:
                    resolved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    _ISSUE_DATE_CACHE.set(key, (issue_date, resolved_at), BODY_TTL)
                    return issue_date, resolved_at
    except (httpx.HTTPError, ValueError):
        return None, None
    return None, None


@beta_tool
def ecfr_fetch(
    title: int,
    section: str = "",
    part: str = "",
    chapter: str = "",
    date: str = "",
    max_chars: int = 16000,
) -> str:
    """Return the verbatim body of one eCFR section (or smaller subtree).

    Hits the eCFR versioner API and extracts plain text from the returned
    XML. When ``date`` is empty the latest issue date for the title is used.

    Args:
        title: CFR title number (e.g. 14, 49, 21).
        section: Section number (e.g. ``121.583``). Pass section OR part.
        part: Part number (e.g. ``121``). Used when section is omitted to
            return the entire part.
        chapter: Optional chapter filter (e.g. ``I``).
        date: ISO date (``YYYY-MM-DD``) for a historical version; empty
            means current.
        max_chars: Maximum body characters returned (default 16000).
    """
    if not section and not part:
        return json.dumps(
            {"error": "ecfr_fetch needs section= or part="}, ensure_ascii=False
        )
    issue_date_resolved_at: Optional[str] = None
    issue_date = date.strip()
    if not issue_date:
        issue_date, issue_date_resolved_at = _ecfr_latest_issue_date(int(title))
    if not issue_date:
        return json.dumps(
            {"error": f"could not resolve latest issue date for title {title}"},
            ensure_ascii=False,
        )
    params: dict[str, Any] = {}
    if section:
        params["section"] = section
    if part:
        params["part"] = part
    if chapter:
        params["chapter"] = chapter
    url = f"https://www.ecfr.gov/api/versioner/v1/full/{issue_date}/title-{int(title)}.xml"
    try:
        r = request_with_retry(
            "GET", url, params=params, headers={"User-Agent": USER_AGENT}
        )
        xml = r.text
    except httpx.HTTPError as e:
        return json.dumps({"error": f"eCFR fetch failed: {e!s}", "url": url})

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(xml, "lxml-xml")
        head = soup.find("HEAD")
        heading = head.get_text(strip=True) if head else ""
        body_text = " ".join(soup.get_text(separator=" ").split())
        extractor = "xml"
    except Exception:
        heading = ""
        body_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml)).strip()
        extractor = "regex"

    truncated = False
    if len(body_text) > max_chars:
        body_text = body_text[: max_chars - 1] + "…"
        truncated = True
    return json.dumps(
        {
            "source": "eCFR (versioner)",
            "title": int(title),
            "section": section or None,
            "part": part or None,
            "chapter": chapter or None,
            "issue_date": issue_date,
            "issue_date_resolved_at": issue_date_resolved_at,
            "heading": heading,
            "body_text": body_text,
            "body_extractor": extractor,
            "body_truncated": truncated,
            "url": url + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""),
        },
        ensure_ascii=False,
    )


@beta_tool
def federal_register_fetch(document_number: str, max_chars: int = 16000) -> str:
    """Return the verbatim body of one Federal Register document.

    Calls the FR API for the document, follows ``raw_text_url``
    (or ``body_html_url`` as fallback) and extracts plain text.

    Args:
        document_number: The FR ``document_number`` (e.g. ``2026-07473``)
            from a ``federal_register_search`` / ``drs_search`` hit.
        max_chars: Maximum body characters returned (default 16000).
    """
    document_number = document_number.strip()
    if not document_number:
        return json.dumps({"error": "document_number is required"}, ensure_ascii=False)
    meta_url = f"https://www.federalregister.gov/api/v1/documents/{document_number}.json"
    try:
        r = request_with_retry("GET", meta_url, headers={"User-Agent": USER_AGENT})
        meta = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return json.dumps({"error": f"Federal Register meta fetch failed: {e!s}", "url": meta_url})

    body_text = ""
    extractor = "none"
    truncated = False
    body_source: Optional[str] = None
    for kind, url in (("raw", meta.get("raw_text_url")), ("html", meta.get("body_html_url"))):
        if not url:
            continue
        result = fetch_body_text(url, max_chars=max_chars)
        if result.get("body_text"):
            body_text = result["body_text"]
            extractor = result.get("extractor") or kind
            truncated = bool(result.get("truncated"))
            body_source = result.get("url") or url
            break

    return json.dumps(
        {
            "source": "Federal Register",
            "document_number": document_number,
            "title": meta.get("title"),
            "type": meta.get("type"),
            "publication_date": meta.get("publication_date"),
            "citation": meta.get("citation"),
            "agencies": [a.get("name") for a in (meta.get("agencies") or [])],
            "html_url": meta.get("html_url"),
            "pdf_url": meta.get("pdf_url"),
            "body_text": body_text,
            "body_extractor": extractor,
            "body_truncated": truncated,
            "body_source_url": body_source,
        },
        ensure_ascii=False,
    )


@beta_tool
def govinfo_fetch(
    package_id: str,
    granule_id: str = "",
    max_chars: int = 16000,
) -> str:
    """Return the verbatim body of one GovInfo package or granule.

    Tries ``htm`` then ``xml`` content endpoints. Use ``granule_id`` to
    target a specific section of a multi-section package (e.g. a single
    CFR section inside a title volume).

    Args:
        package_id: Package id from a ``govinfo_search`` hit
            (e.g. ``CFR-2025-title14-vol3``).
        granule_id: Optional granule id (e.g.
            ``CFR-2025-title14-vol3-sec121-583``). When empty, fetches the
            package's primary content.
        max_chars: Maximum body characters returned (default 16000).
    """
    ensure_keys_loaded()
    key = os.getenv("GOVINFO_API_KEY")
    if not key:
        return json.dumps({"error": "GOVINFO_API_KEY not set"}, ensure_ascii=False)
    package_id = package_id.strip()
    granule_id = granule_id.strip()
    if not package_id:
        return json.dumps({"error": "package_id is required"}, ensure_ascii=False)

    base = f"https://api.govinfo.gov/packages/{package_id}"
    if granule_id:
        base = f"{base}/granules/{granule_id}"

    body_text = ""
    extractor = "none"
    truncated = False
    body_source: Optional[str] = None
    for fmt in ("htm", "xml"):
        url = f"{base}/{fmt}?api_key={key}"
        result = fetch_body_text(url, max_chars=max_chars)
        if result.get("body_text"):
            body_text = result["body_text"]
            extractor = result.get("extractor") or fmt
            truncated = bool(result.get("truncated"))
            body_source = (result.get("url") or url).split("?")[0]
            break

    return json.dumps(
        {
            "source": "GovInfo (GPO)",
            "package_id": package_id,
            "granule_id": granule_id or None,
            "body_text": body_text,
            "body_extractor": extractor,
            "body_truncated": truncated,
            "body_source_url": body_source,
            "details_url": f"https://www.govinfo.gov/app/details/{package_id}",
        },
        ensure_ascii=False,
    )
