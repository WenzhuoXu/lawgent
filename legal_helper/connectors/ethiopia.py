"""Ethiopian Civil Aviation Authority (ECAA) sources for the aviation pack.

**ecaa.gov.et** is the official regulator site; its server-rendered HTML
carries the readable directorate / regulation text and document links
(``ecaa_fetch``). ``ecaa_index`` is a curated map of Ethiopia's civil-aviation
legal framework, since the site exposes no list endpoint. Ethiopian
proclamations themselves (616/2008, 1179/2020, …) are read through the
research layer: ``legal_fetch(ref="et.negarit:616/2008")``.
"""

from __future__ import annotations

import json
import re

import httpx
from anthropic import beta_tool

from .base import http_get_text, truncate

_METAAPPZ = "https://www.metaappz.com"
_ECAA = "https://www.ecaa.gov.et"


# --------------------------------------------------------------------------
# Curated civil-aviation legal-framework index (institutional anchor).
# --------------------------------------------------------------------------
_ECAA_FRAMEWORK_INDEX: list[dict[str, str]] = [
    {
        "instrument": "Civil Aviation Proclamation No. 616/2008",
        "role": "Primary civil-aviation statute (safety/security/economic oversight).",
        "fetch": "legal_fetch(ref='et.negarit:616/2008')",
        "url": f"{_METAAPPZ}/References/ethiopian_laws/federal/pr_616_2008/en/pdf",
    },
    {
        "instrument": "Civil Aviation (Amendment) Proclamation No. 1179/2020",
        "role": "Amends 616/2008 (definitions, sanctions, enforcement).",
        "fetch": "legal_fetch(ref='et.negarit:1179/2020')",
        "url": f"{_METAAPPZ}/References/ethiopian_laws/federal/pr_1179_2020/en/pdf",
    },
    {
        "instrument": "ECAA Re-establishment Proclamation No. 273/2002",
        "role": "Establishes the regulator (objectives, organs, powers).",
        "fetch": "legal_fetch(ref='et.negarit:273/2002')",
        "url": f"{_METAAPPZ}/References/ethiopian_laws/federal/pr_273_2002/en/pdf",
    },
    {
        "instrument": "Ethiopian Civil Aviation Regulations / Rules (ECAR — implementing rules)",
        "role": "Subsidiary technical rules (personnel licensing, operations, "
                "airworthiness, air navigation) issued under the proclamation. "
                "ECAA hosts these as directives; use ecaa_fetch on the regulation "
                "directorate pages and follow PDF links.",
        "fetch": "ecaa_fetch('sectoral/regulation')",
        "url": f"{_ECAA}/sectoral/regulation",
    },
    {
        "instrument": "ICAO Convention on International Civil Aviation (Chicago, 1944)",
        "role": "Ethiopia is an original contracting state (1947); Annexes 1-19 are "
                "implemented domestically through the ECAR. Use the icao_doc / "
                "aviation_treaties RAG collections for Annex / Doc text.",
        "fetch": "retrieve_legal(collection='aviation_treaties', query='Chicago Convention ...')",
        "url": "https://www.icao.int/publications/pages/doc7300.aspx",
    },
    {
        "instrument": "Cape Town Convention + Aircraft Protocol (IDERA)",
        "role": "Ethiopia is a contracting state; relevant to aircraft leasing/financing "
                "security and deregistration (IDERA) for an operator's fleet.",
        "fetch": "retrieve_legal(collection='aviation_treaties', query='Cape Town Aircraft Protocol IDERA')",
        "url": "https://www.unidroit.org/instruments/security-interests/cape-town-convention/",
    },
    {
        "instrument": "Bilateral Air Services Agreement (BASA) / traffic rights",
        "role": "Route entry, frequencies and traffic rights are governed by the BASA "
                "between Ethiopia and the partner state plus ECAA economic licensing. "
                "Confirm the operative BASA via web_search; not in the gazette mirror.",
        "fetch": "web_search('Ethiopia bilateral air services agreement <partner state>')",
        "url": f"{_ECAA}/sectoral/air-transport",
    },
]



@beta_tool
def ecaa_index() -> str:
    """Return the curated Ethiopian civil-aviation legal-framework index (ET).

    Anchors the regulator (ECAA) and the layered instruments a foreign airline
    needs before opening an Ethiopia route: the Civil Aviation Proclamation
    616/2008 and its 1179/2020 amendment, the ECAA re-establishment 273/2002,
    the implementing ECAR rules, Ethiopia's ICAO Chicago Convention membership,
    Cape Town / Aircraft Protocol (IDERA) status, and the bilateral
    air-services-agreement layer that governs route/traffic rights. Each entry
    carries a ``fetch`` hint and an official / mirror URL.
    """
    return json.dumps(
        {
            "source": "Ethiopia civil-aviation legal framework (curated index)",
            "regulator": {
                "name": "Ethiopian Civil Aviation Authority (ECAA)",
                "site": _ECAA,
                "ministry": "Ministry of Transport and Logistics",
            },
            "count": len(_ECAA_FRAMEWORK_INDEX),
            "results": _ECAA_FRAMEWORK_INDEX,
            "note": "Pinpoint cite format: 'Proclamation No. 616/2008, art. X' for the "
                    "statute; 'ECAR Part-N, §N.N' for implementing rules; "
                    "'Chicago Convention Annex N, Standard N.N' for ICAO SARPs.",
        },
        ensure_ascii=False,
    )


@beta_tool
def ecaa_fetch(page: str = "sectoral/regulation", max_chars: int = 12000) -> str:
    """Fetch readable text from an official ECAA page (ecaa.gov.et) (ET).

    The ECAA site server-renders its body text, so this strips the markup and
    returns the visible content plus any document/PDF links found on the page.

    Args:
        page: Path under ``ecaa.gov.et`` (no leading slash). Useful pages:
            ``sectoral/regulation`` (Aviation Regulation directorate),
            ``sectoral/air-transport`` (economic regulation / traffic rights),
            ``sectoral/navigation-services``, ``about/responsibilities``,
            ``services``. Default ``sectoral/regulation``.
        max_chars: Maximum body characters returned (default 12000).
    """
    page = (page or "").strip().lstrip("/")
    url = f"{_ECAA}/{page}" if page else _ECAA
    try:
        html = http_get_text(url)
    except httpx.HTTPError as e:
        return json.dumps({"error": f"ECAA fetch failed: {e!s}", "url": url}, ensure_ascii=False)

    pdf_links: list[str] = []
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ".pdf" in href.lower() or "/document" in href.lower():
                pdf_links.append(href if href.startswith("http") else f"{_ECAA}/{href.lstrip('/')}")
    except Exception:
        text = " ".join(re.sub(r"<[^>]+>", " ", html).split())

    return json.dumps(
        {
            "source": "Ethiopian Civil Aviation Authority (ecaa.gov.et)",
            "url": url,
            "text": truncate(text, max_chars),
            "document_links": sorted(set(pdf_links))[:20],
            "note": "Follow document_links (PDF) via fetch_url_to_artifact for full "
                    "directive/ECAR text; cite the ECAA page or directive title + section.",
        },
        ensure_ascii=False,
    )
