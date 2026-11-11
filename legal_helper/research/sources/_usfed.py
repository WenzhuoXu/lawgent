"""Request helpers for the US federal sources (eCFR, Federal Register, GovInfo).

Plain functions returning parsed rows; ``us.py`` turns them into hits. A
module whose name starts with ``_`` is never loaded as a source.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from ...connectors.base import (
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
from ..base import SourceUnavailable


def search_ecfr(query: str, *, title: int = 0, per_page: int = 10) -> list[dict[str, Any]]:
    """eCFR full-text search over the current CFR.

    ``date=current``: without it every superseded version of a section comes
    back too (§ 254.4 three times). The search is conjunctive over every
    token, so a long query with one off-topic word returns nothing.
    """
    params: dict[str, Any] = {"query": query, "per_page": max(1, min(per_page, 20)), "date": "current"}
    if title and int(title) > 0:
        params["hierarchy[title]"] = int(title)
    try:
        data = http_get("https://www.ecfr.gov/api/search/v1/results", params)
    except httpx.HTTPError as exc:
        raise SourceUnavailable(f"eCFR search failed: {exc!s}"[:300]) from exc
    rows = []
    for hit in data.get("results") or []:
        h = hit.get("hierarchy") or {}
        rows.append({
            "title": h.get("title"), "part": h.get("part"), "section": h.get("section"),
            "headings": hit.get("hierarchy_headings") or {},
            "snippet": truncate(hit.get("full_text_excerpt") or hit.get("headings_text"), 600),
            "effective_on": hit.get("starts_on"),
        })
    return rows


def search_federal_register(query: str, *, agency: str = "", per_page: int = 10) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "conditions[term]": query,
        "per_page": max(1, min(per_page, 20)),
        "order": "relevance",
        "fields[]": ["title", "abstract", "document_number", "publication_date", "type", "html_url", "citation"],
    }
    if agency:
        params["conditions[agencies][]"] = agency
    try:
        data = http_get("https://www.federalregister.gov/api/v1/documents.json", params)
    except httpx.HTTPError as exc:
        raise SourceUnavailable(f"Federal Register search failed: {exc!s}"[:300]) from exc
    return list(data.get("results") or [])


def search_govinfo(query: str, *, collections: Optional[list[str]] = None, page_size: int = 10) -> list[dict[str, Any]]:
    ensure_keys_loaded()
    key = os.getenv("GOVINFO_API_KEY")
    if not key:
        raise SourceUnavailable("GovInfo needs GOVINFO_API_KEY (free at https://api.data.gov/signup/).")
    q = query.strip()
    if collections:
        codes = " ".join(c.strip().upper() for c in collections if c.strip())
        if codes:
            q = f"({q}) AND collection:({codes})"
    payload = {
        "query": q,
        "pageSize": max(1, min(page_size, 20)),
        "offsetMark": "*",
        "sorts": [{"field": "relevancy", "sortOrder": "DESC"}],
        # historical=True ranked the 2007-2010 US Code editions first.
        "historical": False,
        "resultLevel": "default",
    }
    try:
        data = http_post_json("https://api.govinfo.gov/search", payload, headers={"X-Api-Key": key}, cache_ttl=SEARCH_TTL)
    except httpx.HTTPError as exc:
        raise SourceUnavailable(f"GovInfo search failed: {exc!s}"[:300]) from exc
    rows = []
    for hit in data.get("results") or []:
        pkg = hit.get("packageId")
        rows.append({
            "package_id": pkg, "granule_id": hit.get("granuleId"),
            "title": truncate(hit.get("title"), 400),
            "collection": hit.get("collectionCode") or hit.get("collection"),
            "date_issued": hit.get("dateIssued"),
            "snippet": truncate(hit.get("teaser") or hit.get("summary"), 600),
            "details_url": f"https://www.govinfo.gov/app/details/{pkg}" if pkg else None,
        })
    return rows


# eCFR titles are re-issued; cache the latest issue date for a day, never failures.
_ISSUE_DATE_CACHE = TTLCache(maxsize=64)


def ecfr_latest_issue_date(title: int) -> Optional[str]:
    key = str(int(title))
    cached = _ISSUE_DATE_CACHE.get(key)
    if cached is not TTLCache._MISS:
        return cached
    try:
        r = request_with_retry("GET", "https://www.ecfr.gov/api/versioner/v1/titles", headers={"User-Agent": USER_AGENT})
        for t in r.json().get("titles") or []:
            if int(t.get("number") or 0) == int(title):
                issue = t.get("latest_issue_date") or t.get("up_to_date_as_of")
                if issue:
                    _ISSUE_DATE_CACHE.set(key, issue, BODY_TTL)
                    return issue
    except (httpx.HTTPError, ValueError):
        return None
    return None


__all__ = ["search_ecfr", "search_federal_register", "search_govinfo", "ecfr_latest_issue_date"]
