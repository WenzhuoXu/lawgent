"""Aviation-specific connectors: FAA eCFR Title 14 + CAAC CCAR via WebFetch.

This module is the canonical Python implementation for both the in-process
function tools and the ``mcp/servers/ccar_aviation/`` MCP shell.

The CAAC site (caac.gov.cn) has no documented API; we fetch HTML/PDF and
parse with BeautifulSoup + pdfminer. Rate-limited and cached.
"""

import json
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from anthropic import beta_tool

from .base import USER_AGENT, cache_key, request_with_retry, truncate
from .us_federal import ecfr_search

_CCAR_CACHE = Path.home() / ".legal_helper" / "cache" / "ccar"
_CCAR_CACHE.mkdir(parents=True, exist_ok=True)
_RATE_LIMIT_DELAY = 1.0  # seconds between caac.gov.cn requests


@beta_tool
def faa_title14_search(query: str, per_page: int = 8) -> str:
    """Search FAA regulations (14 CFR) via eCFR Title 14."""
    return ecfr_search.call({"query": query, "title": 14, "per_page": per_page})


def _caac_get(path: str, params: Optional[dict[str, Any]] = None) -> str:
    time.sleep(_RATE_LIMIT_DELAY)
    url = f"http://www.caac.gov.cn{path}" if path.startswith("/") else path
    resp = request_with_retry(
        "GET", url, params=params, headers={"User-Agent": USER_AGENT}
    )
    return resp.text


def _strip_html(html: str, limit: int = 1200) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return truncate(html, limit)
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return truncate(text, limit)


@beta_tool
def ccar_search(query: str, per_page: int = 8) -> str:
    """Search CAAC publications for CCAR parts, airworthiness directives, 通告.

    Backend: WebFetch against caac.gov.cn + amos.caac.gov.cn search portal.
    Rate-limited (1 req/sec) and cached on disk. Use when working in PRC
    aviation context (民航局规章, 适航指令).

    Args:
        query: Chinese or English query (e.g. "CCAR-145", "适航指令 B737",
            "运输类飞机适航标准").
        per_page: Number of results to return (1-20).
    """
    per_page = max(1, min(int(per_page), 20))
    # sha256, not hash(): hash() is salted per process, so its keys never
    # survive a restart and can collide across runs.
    cache_path = _CCAR_CACHE / f"search_{cache_key(query, per_page)}.json"
    if cache_path.is_file() and time.time() - cache_path.stat().st_mtime < 86400:
        return cache_path.read_text(encoding="utf-8")

    try:
        html = _caac_get("/XXGK/XXGK/index_140.html", params={"keywords": query})
    except httpx.HTTPError as e:
        return json.dumps({"error": f"CAAC search failed: {e!s}", "query": query})

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return json.dumps({"error": "beautifulsoup4 required for CAAC parsing"})
    soup = BeautifulSoup(html, "lxml")
    out: list[dict[str, Any]] = []
    for li in soup.select(".zclist li, ul.list li")[:per_page]:
        anchor = li.find("a")
        if not anchor:
            continue
        href = anchor.get("href") or ""
        if href and not href.startswith("http"):
            href = f"http://www.caac.gov.cn{href}"
        date = ""
        span = li.find("span")
        if span:
            date = span.get_text(strip=True)
        out.append(
            {
                "title": anchor.get_text(strip=True),
                "url": href,
                "publish_date": date,
                "snippet": truncate(li.get_text(separator=" ", strip=True), 400),
            }
        )

    payload = json.dumps(
        {
            "source": "CAAC (caac.gov.cn)",
            "query": query,
            "count": len(out),
            "results": out,
        },
        ensure_ascii=False,
    )
    cache_path.write_text(payload, encoding="utf-8")
    return payload


@beta_tool
def ccar_fetch(url: str) -> str:
    """Fetch a CAAC publication page and return its plain-text body.

    Args:
        url: Absolute URL on caac.gov.cn (or relative path starting with ``/``).
    """
    cache_path = _CCAR_CACHE / f"fetch_{cache_key(url)}.json"
    if cache_path.is_file() and time.time() - cache_path.stat().st_mtime < 86400:
        return cache_path.read_text(encoding="utf-8")

    try:
        html = _caac_get(url)
    except httpx.HTTPError as e:
        return json.dumps({"error": f"CAAC fetch failed: {e!s}", "url": url})

    payload = json.dumps(
        {
            "source": "CAAC (caac.gov.cn)",
            "url": url,
            "text": _strip_html(html, limit=8000),
        },
        ensure_ascii=False,
    )
    cache_path.write_text(payload, encoding="utf-8")
    return payload
