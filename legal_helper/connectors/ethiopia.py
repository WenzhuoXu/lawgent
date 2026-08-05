"""Ethiopia (ET) legal + civil-aviation sources.

Ethiopia publishes no public REST API over its statute book, so these
connectors stand on the two reachable public surfaces:

- **metaappz.com** mirrors the Federal Negarit Gazeta. Every federal
  proclamation lives at
  ``/References/ethiopian_laws/federal/pr_{num}_{year}/en/txt`` (a
  server-rendered English transcript, available for the subset of laws that
  have been re-typed) and the original bilingual (Amharic + English) gazette
  scan at ``/pdf/ethiopian_laws/federal/{year}/pr_{num}_{year}.pdf``.
  ``ethiopia_law_fetch`` tries the typed transcript first and falls back to
  extracting the PDF.
- **ecaa.gov.et** — the official Ethiopian Civil Aviation Authority site.
  Its server-rendered HTML carries the readable directorate / regulation
  text; ``ecaa_fetch`` strips the markup.

``ethiopia_law_search`` and ``ecaa_index`` resolve against a curated
catalogue of the core instruments because neither surface exposes a machine
index (metaappz search is a Google Custom Search widget; ECAA has no list
endpoint). The catalogue mirrors the shape of ``easa_ear_index`` — hand
maintained, each entry carrying an official / mirror URL and a pinpoint hint.

The company is opening an Addis Ababa route, so the catalogue is weighted
toward civil-aviation instruments; general foundational laws are included so
``ethiopia_law_fetch`` is useful beyond aviation too.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import httpx
from anthropic import beta_tool

from ._body_fetch import fetch_body_text
from .base import BODY_TTL, http_get_text, truncate

_METAAPPZ = "https://www.metaappz.com"
_ECAA = "https://www.ecaa.gov.et"

# Markers that distinguish a real typed proclamation transcript from the
# generic "Ethiopian Federal Law" catalogue shell metaappz serves when the
# /en/txt transcript does not exist for a given slug.
_LAW_BODY_TOKENS = ("WHEREAS", "hereby", "Short Title", "PROCLAMATION NO", "it is proclaimed")


# --------------------------------------------------------------------------
# Curated catalogue of core Ethiopian instruments (aviation-weighted).
# `slug` drives metaappz fetches; `pdf` is the verified direct gazette scan.
# --------------------------------------------------------------------------
_ETHIOPIA_LAW_CATALOG: list[dict[str, Any]] = [
    {
        "title": "Civil Aviation Proclamation No. 616/2008",
        "proclamation": "616/2008",
        "slug": "pr_616_2008",
        "category": "aviation",
        "topics": ["civil aviation", "ECAA powers", "air operator", "licensing",
                   "airworthiness", "air navigation", "safety oversight"],
        "summary": "Consolidating civil-aviation law that strengthens the ECAA's "
                   "regulatory, technical and supervisory mandate and aligns Ethiopia "
                   "with ICAO standards; the primary civil-aviation statute.",
        "pdf": f"{_METAAPPZ}/pdf/ethiopian_laws/federal/2008/pr_616_2008.pdf",
    },
    {
        "title": "Civil Aviation (Amendment) Proclamation No. 1179/2020",
        "proclamation": "1179/2020",
        "slug": "pr_1179_2020",
        "category": "aviation",
        "topics": ["civil aviation amendment", "penalties", "ECAA", "enforcement"],
        "summary": "Amends Proclamation 616/2008 — updates definitions, sanctions and "
                   "the ECAA's enforcement powers.",
        "pdf": f"{_METAAPPZ}/pdf/ethiopian_laws/federal/2020/pr_1179_2020.pdf",
    },
    {
        "title": "Ethiopian Civil Aviation Authority Re-establishment Proclamation No. 273/2002",
        "proclamation": "273/2002",
        "slug": "pr_273_2002",
        "category": "aviation",
        "topics": ["ECAA establishment", "authority", "objectives", "powers", "duties"],
        "summary": "Re-establishes the ECAA, defining its objectives, organs and "
                   "powers; institutional anchor for the regulator (partly superseded "
                   "by 616/2008 on regulatory functions).",
        "pdf": f"{_METAAPPZ}/pdf/ethiopian_laws/federal/2002/pr_273_2002.pdf",
    },
    {
        "title": "Federal Negarit Gazeta Establishment Proclamation No. 3/1995",
        "proclamation": "3/1995",
        "slug": "pr_3_1995",
        "category": "foundational",
        "topics": ["official gazette", "promulgation", "publication of laws"],
        "summary": "Establishes the Federal Negarit Gazeta as the official vehicle for "
                   "publishing all federal laws — the citation backbone for every "
                   "Ethiopian proclamation.",
        "pdf": f"{_METAAPPZ}/pdf/ethiopian_laws/federal/1995/pr_3_1995.pdf",
    },
    {
        "title": "Commercial Code of Ethiopia Proclamation No. 1243/2021",
        "proclamation": "1243/2021",
        "slug": "pr_1243_2021",
        "category": "commercial",
        "topics": ["commercial code", "business organisations", "carriage", "insurance",
                   "negotiable instruments", "bankruptcy"],
        "summary": "The revised Commercial Code — governs business organisations, "
                   "commercial contracts (including carriage), insurance and insolvency; "
                   "relevant to airline leasing, ground-handling and charter contracts.",
        "pdf": f"{_METAAPPZ}/pdf/ethiopian_laws/federal/2021/pr_1243_2021.pdf",
    },
    {
        "title": "Investment Proclamation No. 1180/2020",
        "proclamation": "1180/2020",
        "slug": "pr_1180_2020",
        "category": "commercial",
        "topics": ["foreign investment", "investment permit", "EIC", "incentives",
                   "repatriation"],
        "summary": "Governs domestic and foreign investment, permits and incentives "
                   "administered by the Ethiopian Investment Commission; relevant to "
                   "establishing route/station operations in Ethiopia.",
        "pdf": f"{_METAAPPZ}/pdf/ethiopian_laws/federal/2020/pr_1180_2020.pdf",
    },
]


# --------------------------------------------------------------------------
# Curated civil-aviation legal-framework index (institutional anchor).
# --------------------------------------------------------------------------
_ECAA_FRAMEWORK_INDEX: list[dict[str, str]] = [
    {
        "instrument": "Civil Aviation Proclamation No. 616/2008",
        "role": "Primary civil-aviation statute (safety/security/economic oversight).",
        "fetch": "ethiopia_law_fetch('616/2008')",
        "url": f"{_METAAPPZ}/References/ethiopian_laws/federal/pr_616_2008/en/pdf",
    },
    {
        "instrument": "Civil Aviation (Amendment) Proclamation No. 1179/2020",
        "role": "Amends 616/2008 (definitions, sanctions, enforcement).",
        "fetch": "ethiopia_law_fetch('1179/2020')",
        "url": f"{_METAAPPZ}/References/ethiopian_laws/federal/pr_1179_2020/en/pdf",
    },
    {
        "instrument": "ECAA Re-establishment Proclamation No. 273/2002",
        "role": "Establishes the regulator (objectives, organs, powers).",
        "fetch": "ethiopia_law_fetch('273/2002')",
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
                "security and deregistration (IDERA) for the new route's fleet.",
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


def _normalize_proclamation(raw: str) -> Optional[tuple[int, int, str]]:
    """Parse '616/2008', '616-2008', 'pr_616_2008', '616 of 2008' → (num, year, slug)."""
    raw = (raw or "").strip().lower().replace("proclamation", "").replace("no.", "").strip()
    m = re.search(r"pr[_\s]*(\d{1,5})[_\s]*(\d{4})", raw)
    if not m:
        m = re.search(r"(\d{1,5})\s*(?:/|-|\s+of\s+|\s+)\s*(\d{4})", raw)
    if not m:
        return None
    num, year = int(m.group(1)), int(m.group(2))
    if not (1900 < year < 2100):
        return None
    return num, year, f"pr_{num}_{year}"


def _looks_like_law_body(text: str) -> bool:
    return any(tok.lower() in text.lower() for tok in _LAW_BODY_TOKENS)


def _is_machine_readable_english(text: str) -> bool:
    """True only if extracted text is usable English law prose.

    The gazette PDFs are non-OCR'd bilingual (Amharic + English) scans, so PDF
    extraction frequently yields Ge'ez script plus ``(cid:NN)`` font-glyph
    mojibake with too little real English to cite. Returning that as a law body
    just pushes the model into hedging ("pinpoint unavailable") instead of
    falling back to a clean secondary source. This gate detects the unusable
    case so the caller can redirect to web_search.
    """
    sample = (text or "")[:4000]
    if not sample.strip():
        return False
    if "(cid:" in sample:  # PDF font-glyph mojibake — never citable
        return False
    ascii_letters = sum(1 for ch in sample if ("a" <= ch <= "z") or ("A" <= ch <= "Z"))
    non_space = sum(1 for ch in sample if not ch.isspace())
    if non_space == 0:
        return False
    # A clean English transcript runs well above 0.5 ASCII-letter density;
    # interleaved Ge'ez scans fall far below it.
    return (ascii_letters / non_space) >= 0.45 and _looks_like_law_body(sample)


def _extract_transcript(html: str) -> str:
    """Pull the typed law body out of a metaappz /en/txt page.

    The page chrome ends at a 'Text (English)' marker; the proclamation body
    follows it. Returns '' if no real transcript is present (the catalogue
    shell metaappz serves for slugs without a typed transcript).
    """
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
    except Exception:
        text = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    # Slice off the navigation/header chrome up to the download row.
    marker = "Text (English)"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
    if len(text) < 400 or not _looks_like_law_body(text):
        return ""
    return text


@beta_tool
def ethiopia_law_search(query: str, per_page: int = 8) -> str:
    """Search the curated catalogue of core Ethiopian federal laws (ET).

    Ethiopia has no public statute-search API, so this matches a hand-curated
    catalogue weighted toward civil-aviation instruments (Civil Aviation
    Proclamation 616/2008 and its 1179/2020 amendment, the ECAA
    re-establishment 273/2002) plus the foundational Negarit Gazeta,
    Commercial Code and Investment proclamations. Each hit carries the
    proclamation number, a ``fetch`` hint, topics and the gazette PDF URL.
    Call ``ethiopia_law_fetch`` on a hit to read the body text.

    For statutes not in the catalogue, pass the proclamation number directly
    to ``ethiopia_law_fetch`` (e.g. ``"1051/2017"``) — the mirror serves any
    federal proclamation by number.

    Args:
        query: Free-text query — a proclamation number (``616/2008``), an
            English keyword (``civil aviation``, ``investment``, ``commercial
            code``, ``air operator``), or a topic phrase.
        per_page: Maximum hits to return (1-15).
    """
    per_page = max(1, min(int(per_page), 15))
    q = (query or "").strip().lower()
    parsed = _normalize_proclamation(query)
    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in _ETHIOPIA_LAW_CATALOG:
        score = 0
        if parsed and entry["slug"] == parsed[2]:
            score += 100
        if q and q in entry["title"].lower():
            score += 40
        if q and q in entry["proclamation"]:
            score += 30
        for topic in entry["topics"]:
            if q and (q in topic or topic in q):
                score += 8
        for word in re.findall(r"[a-z]{3,}", q):
            if word in entry["title"].lower() or word in entry["summary"].lower():
                score += 3
            if any(word in t for t in entry["topics"]):
                score += 4
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        # No catalogue match. The catalogue is small and aviation-weighted, so
        # returning the aviation core here would misdirect unrelated queries
        # (e.g. consumer-protection, tax, labour, data). Tell the caller to use
        # web_search instead of handing back an off-topic default.
        return json.dumps(
            {
                "source": "Ethiopia federal law catalogue (Federal Negarit Gazeta mirror)",
                "query": query,
                "count": 0,
                "results": [],
                "next_step": "web_search",
                "note": "No instrument in the curated Ethiopian catalogue matches this query. "
                        "The catalogue is small and aviation-weighted. For this topic run "
                        "web_search for the governing Ethiopian proclamation and an "
                        "authoritative English source, then cite that with a pinpoint. If you "
                        "already know the proclamation number, pass it to ethiopia_law_fetch.",
            },
            ensure_ascii=False,
        )

    results = []
    for _score, entry in scored[:per_page]:
        results.append(
            {
                "title": entry["title"],
                "proclamation": entry["proclamation"],
                "category": entry["category"],
                "summary": entry["summary"],
                "topics": entry["topics"],
                "fetch": f"ethiopia_law_fetch('{entry['proclamation']}')",
                "gazette_pdf": entry["pdf"],
            }
        )
    return json.dumps(
        {
            "source": "Ethiopia federal law catalogue (Federal Negarit Gazeta mirror)",
            "query": query,
            "count": len(results),
            "results": results,
            "note": "Cite as 'Proclamation No. NNN/YYYY, art. X, Federal Negarit Gazeta'. "
                    "Pass any proclamation number to ethiopia_law_fetch to read the body.",
        },
        ensure_ascii=False,
    )


@beta_tool
def ethiopia_law_fetch(proclamation: str, max_chars: int = 16000) -> str:
    """Return the body text of one Ethiopian federal proclamation (ET).

    Resolves a proclamation reference to the Federal Negarit Gazeta mirror on
    metaappz, tries the typed English transcript first
    (``/pr_{num}_{year}/en/txt``), and falls back to extracting the original
    bilingual gazette PDF (``/pdf/.../pr_{num}_{year}.pdf``) when no typed
    transcript exists. The PDF is the Amharic + English scan, so extracted
    text may interleave Ge'ez script around the English clauses.

    Args:
        proclamation: Proclamation reference in any of these forms:
            ``616/2008``, ``616-2008``, ``pr_616_2008``, ``616 of 2008``.
        max_chars: Maximum body characters returned (default 16000).
    """
    parsed = _normalize_proclamation(proclamation)
    if not parsed:
        return json.dumps(
            {"error": "could not parse proclamation; use forms like '616/2008' or 'pr_616_2008'",
             "input": proclamation},
            ensure_ascii=False,
        )
    num, year, slug = parsed
    txt_url = f"{_METAAPPZ}/References/ethiopian_laws/federal/{slug}/en/txt"
    pdf_url = f"{_METAAPPZ}/pdf/ethiopian_laws/federal/{year}/{slug}.pdf"
    detail_url = f"{_METAAPPZ}/References/ethiopian_laws/federal/{slug}/en/pdf"

    # 1) typed English transcript — published gazettes never change, so the
    # 24 h body cache is safe.
    transcript = ""
    try:
        html = http_get_text(txt_url, cache_ttl=BODY_TTL)
        transcript = _extract_transcript(html)
    except httpx.HTTPError:
        transcript = ""

    if transcript:
        truncated = len(transcript) > max_chars
        body = transcript[: max_chars - 1] + "…" if truncated else transcript
        return json.dumps(
            {
                "source": "Federal Negarit Gazeta (metaappz typed transcript)",
                "proclamation": f"{num}/{year}",
                "body_text": body,
                "body_extractor": "html-transcript",
                "body_truncated": truncated,
                "url": txt_url,
                "pdf_url": pdf_url,
                "note": f"Cite as 'Proclamation No. {num}/{year}, art. X'. Verify the "
                        "article number against the gazette PDF where exact pinpoints matter.",
            },
            ensure_ascii=False,
        )

    # 2) gazette PDF fallback
    result = fetch_body_text(pdf_url, max_chars=max_chars)
    body = result.get("body_text") or ""
    if not body and result.get("error"):
        return json.dumps(
            {
                "error": f"no typed transcript and PDF fetch failed: {result['error']}",
                "proclamation": f"{num}/{year}",
                "detail_url": detail_url,
                "next_step": "web_search",
                "note": "No machine-readable text from this source. Run web_search for an "
                        "authoritative English secondary source and cite that with a pinpoint.",
            },
            ensure_ascii=False,
        )
    # Guard: the bilingual gazette scan often extracts as Ge'ez + (cid:NN)
    # mojibake with too little English to cite. Returning it just induces
    # hedged "pinpoint unavailable" output, so signal the caller to use
    # web_search instead of dumping unusable text.
    if not _is_machine_readable_english(body):
        return json.dumps(
            {
                "source": "Federal Negarit Gazeta (metaappz bilingual PDF scan)",
                "proclamation": f"{num}/{year}",
                "body_text": "",
                "transcript_available": False,
                "url": pdf_url,
                "detail_url": detail_url,
                "next_step": "web_search",
                "note": f"Proclamation No. {num}/{year} has no typed English transcript and the "
                        "gazette PDF is a non-OCR'd Amharic+English scan (not machine-readable). "
                        "Do NOT report 'pinpoint unavailable' from this. Instead run web_search "
                        "for an authoritative English source — the official regulator's English "
                        "pages, UNCTAD Investment Laws, ICAO, or a reputable international "
                        "law-firm briefing — and cite that with an article/section pinpoint.",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "source": "Federal Negarit Gazeta (metaappz bilingual PDF scan)",
            "proclamation": f"{num}/{year}",
            "body_text": body,
            "body_extractor": result.get("extractor", "pdf"),
            "body_truncated": bool(result.get("truncated")),
            "url": pdf_url,
            "detail_url": detail_url,
            "note": f"Cite as 'Proclamation No. {num}/{year}, art. X, Federal Negarit Gazeta'. "
                    "PDF is the Amharic+English scan; English clause text is interleaved with "
                    "Ge'ez — locate the English article heading for the pinpoint.",
        },
        ensure_ascii=False,
    )


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
