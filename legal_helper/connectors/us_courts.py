"""US case law and dockets via CourtListener (Free Law Project)."""

import json
import os
import re
from typing import Any, Optional

import httpx
from anthropic import beta_tool

from ._body_fetch import fetch_body_text
from .base import USER_AGENT, ensure_keys_loaded, http_get, request_with_retry, truncate


@beta_tool
def courtlistener_search(
    query: str,
    court: Optional[str] = None,
    result_type: str = "o",
    per_page: int = 8,
) -> str:
    """Search CourtListener. Returns case-name + citation + cluster_id +
    URL — the ``snippet`` field is often empty. Call
    ``courtlistener_fetch(cluster_id)`` on a hit to get the opinion
    plain-text body.

    Covers federal Circuit / District / Supreme Court opinions, state
    appellate opinions, and RECAP-archived PACER dockets. Set
    ``COURTLISTENER_API_TOKEN`` in the environment for higher rate limits;
    anonymous read works for low-volume queries.

    Args:
        query: Free-text query, party name, citation, or quoted phrase.
        court: Optional court slug (``scotus``, ``ca9``, ``cafc``, ``nysd``…).
            Omit to search all jurisdictions.
        result_type: One of ``o`` (opinions, default), ``r`` (RECAP docket
            entries), ``d`` (RECAP dockets), ``oa`` (oral arguments).
        per_page: Results per page (1-20).
    """
    per_page = max(1, min(int(per_page), 20))
    params: dict[str, Any] = {
        "q": query,
        "type": result_type,
        "order_by": "score desc",
    }
    if court:
        params["court"] = court

    ensure_keys_loaded()
    headers: dict[str, str] = {}
    token = os.getenv("COURTLISTENER_API_TOKEN")
    if token:
        headers["Authorization"] = f"Token {token}"

    try:
        data = http_get(
            "https://www.courtlistener.com/api/rest/v4/search/",
            params,
            headers=headers,
        )
    except httpx.HTTPError as e:
        return json.dumps({"error": f"CourtListener request failed: {e!s}", "query": query})

    results = []
    for hit in (data.get("results") or [])[:per_page]:
        snippet = hit.get("snippet") or ""
        if isinstance(snippet, list):
            snippet = " ".join(s for s in snippet if isinstance(s, str))
        abs_url = hit.get("absolute_url") or ""
        if abs_url and abs_url.startswith("/"):
            abs_url = "https://www.courtlistener.com" + abs_url
        results.append(
            {
                "case_name": hit.get("caseName") or hit.get("case_name"),
                "court": hit.get("court") or hit.get("court_id"),
                "date_filed": hit.get("dateFiled") or hit.get("date_filed"),
                "docket_number": hit.get("docketNumber") or hit.get("docket_number"),
                "citation": hit.get("citation") or hit.get("citations"),
                "snippet": truncate(snippet, 600),
                "url": abs_url,
            }
        )
    return json.dumps(
        {
            "source": "CourtListener",
            "query": query,
            "court_filter": court or "all",
            "result_type": result_type,
            "count": len(results),
            "results": results,
            "authenticated": bool(token),
        },
        ensure_ascii=False,
    )


_CL_URL_RE = re.compile(r"/opinion/(\d+)/", re.IGNORECASE)


@beta_tool
def courtlistener_fetch(
    cluster_id: str = "",
    opinion_id: str = "",
    url: str = "",
    max_chars: int = 16000,
) -> str:
    """Return the verbatim plain-text body of one CourtListener opinion.

    Fetches the opinion record via the v4 API. Pass any one of
    ``cluster_id`` (preferred — from a ``courtlistener_search`` result),
    ``opinion_id`` (when known), or ``url`` (the search result's
    ``absolute_url``). Tries ``plain_text`` first, then HTML variants
    (``html``, ``html_with_citations``, ``html_lawbox``, ``html_columbia``,
    ``html_anon_2020``), then ``download_url`` if those are empty.

    Args:
        cluster_id: Opinion-cluster id from a search hit's ``cluster_id``.
        opinion_id: Direct opinion id (one per cluster, sometimes more).
        url: Search result's ``absolute_url`` — the cluster id is extracted
            from it.
        max_chars: Maximum body characters returned (default 16000).
    """
    ensure_keys_loaded()
    token = os.getenv("COURTLISTENER_API_TOKEN")
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Token {token}"
    cid = (cluster_id or "").strip()
    oid = (opinion_id or "").strip()
    if not cid and not oid and url:
        m = _CL_URL_RE.search(url)
        if m:
            cid = m.group(1)
    if not cid and not oid:
        return json.dumps(
            {"error": "pass cluster_id, opinion_id, or a /opinion/<id>/ url"},
            ensure_ascii=False,
        )

    opinions: list[dict[str, Any]] = []
    try:
        if oid:
            r = request_with_retry(
                "GET",
                f"https://www.courtlistener.com/api/rest/v4/opinions/{oid}/",
                headers=headers,
            )
            opinions = [r.json()]
        else:
            r = request_with_retry(
                "GET",
                "https://www.courtlistener.com/api/rest/v4/opinions/",
                params={"cluster": cid},
                headers=headers,
            )
            opinions = r.json().get("results") or []
    except (httpx.HTTPError, ValueError) as e:
        return json.dumps({"error": f"CourtListener fetch failed: {e!s}"}, ensure_ascii=False)

    if not opinions:
        return json.dumps(
            {"error": "no opinions found", "cluster_id": cid or None, "opinion_id": oid or None},
            ensure_ascii=False,
        )

    op = opinions[0]
    text_fields = (
        "plain_text",
        "html",
        "html_with_citations",
        "html_lawbox",
        "html_columbia",
        "html_anon_2020",
    )
    body_text = ""
    extractor = "none"
    body_source: Optional[str] = None
    for field in text_fields:
        v = op.get(field) or ""
        if not v:
            continue
        if field == "plain_text":
            body_text = v
        else:
            try:
                from bs4 import BeautifulSoup

                body_text = " ".join(BeautifulSoup(v, "lxml").get_text(" ").split())
            except Exception:
                body_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", v))
        extractor = field
        body_source = f"courtlistener:opinion/{op.get('id')}#{field}"
        break

    if not body_text and op.get("download_url"):
        result = fetch_body_text(op["download_url"], max_chars=max_chars)
        if result.get("body_text"):
            body_text = result["body_text"]
            extractor = result.get("extractor") or "download"
            body_source = result.get("url") or op.get("download_url")

    truncated = False
    if len(body_text) > max_chars:
        body_text = body_text[: max_chars - 1] + "…"
        truncated = True

    return json.dumps(
        {
            "source": "CourtListener",
            "opinion_id": op.get("id"),
            "cluster_id": op.get("cluster_id"),
            "absolute_url": (
                f"https://www.courtlistener.com{op.get('absolute_url')}"
                if (op.get("absolute_url") or "").startswith("/")
                else op.get("absolute_url")
            ),
            "type": op.get("type"),
            "author_str": op.get("author_str"),
            "per_curiam": op.get("per_curiam"),
            "page_count": op.get("page_count"),
            "body_text": body_text,
            "body_extractor": extractor,
            "body_truncated": truncated,
            "body_source_url": body_source,
        },
        ensure_ascii=False,
    )
