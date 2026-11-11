"""Ethiopian federal law: the curated catalogue and gazette-mirror helpers.

Ethiopia publishes no statute-search API. metaappz.com mirrors the Federal
Negarit Gazeta: a typed English transcript at
``/References/ethiopian_laws/federal/pr_{num}_{year}/en/txt`` for the laws
that have been re-typed, and the bilingual gazette scan at
``/pdf/ethiopian_laws/federal/{year}/pr_{num}_{year}.pdf``. Search matches
the hand-curated catalogue below (aviation-weighted, plus foundational laws);
any proclamation can still be fetched by number. Used by ``et.py``; a module
whose name starts with ``_`` is never loaded as a source.
"""

from __future__ import annotations

import re
from typing import Any, Optional

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


def search_catalogue(query: str, per_page: int = 8) -> list[dict[str, Any]]:
    """Catalogue entries matching a number, title words or topics, best first."""
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
    return [entry for _score, entry in scored[:per_page]]
